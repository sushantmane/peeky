"""Fixed dependency-free Cargo binaries debugged through LLDB DAP.

Scope: one explicit edition-2021 binary with immutable declared integration-test
files, built offline/locked into a fresh workspace with a controlled environment
and explicit real (non-rustup-proxy) cargo/rustc executables. Symbols come only
from application compile units (see rust_symbols). Root integer locals kept in
memory are the only mutable values; qualified Option<i32> values are shown by active variant and other
data-carrying enums are opaque. Prepared builds also compile the declared Cargo
integration test; verification runs that hashed test executable directly (never
Cargo, so nothing can be rebuilt or substituted) and still requires the
independent checker. Trusted code; not a sandbox.
"""

import hashlib
import itertools
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from peeky.adapters import PythonAdapter
from peeky.agent import BUILD_RETAINED
from peeky.build import (
    ArtifactSpec,
    BuildCommandError,
    BuildRecipe,
    Command,
    _digest,
    dependency_paths,
    prepare,
    real_tool,
)
from peeky.compiled_verification import CompiledVerification
from peeky.names import display_function
from peeky.native import (
    NativeSession,
    _metadata,
    _path_name,
    _writable_integer,
    function_scopes,
    module_identity,
)
from peeky.rust_ir import ir_locals
from peeky.rust_lexer import without_comments
from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs
from peeky.rust_mir import OwnedLocals, frame_shape, ir_functions, load_bodies, stop_block
from peeky.rust_symbols import (
    OWNED_CONTAINERS,
    RUST_METADATA,
    RUST_SCALARS,
    STRUCT_PREFIX,
    UNIQUE_SCALARS,
    UNVERIFIED,
    _spelled,
    active_variant,
    application_symbols,
    composite_container,
    container_view,
    lldb_byte,
    lldb_char,
    lldb_float_debug,
    struct_name,
    struct_view,
    without_numbered_structs,
)

DEPS = "target/debug/deps"
MIR_DIR = "target/peeky-mir"
_COLLAPSE = re.compile(r"\bcollapse_debuginfo\b")


# Cargo runs this for workspace crates only (RUSTC_WORKSPACE_WRAPPER). For the debugged
# binary crate alone it adds the MIR the owned-local proof reads (rust_mir): no MIR
# optimizations, so moves stay moves and blocks keep their names in the IR. Unstable
# flags are allowed for that one crate, and legacy mangling keeps the frame hashes
# Peeky's IR facts are keyed by. Dependencies and tests compile unchanged. Python, not a
# shell: dash drops variables like CARGO_BIN_EXE_<bin-with-dashes> that tests need.
MIR_WRAPPER = """import os
import sys

args = sys.argv[1:]
crate = binary = None
for previous, arg in zip(args, args[1:]):
    if previous == "--crate-name":
        crate = arg
    elif previous == "--crate-type" and arg == "bin":
        binary = True
wanted = os.environ.get("PEEKY_MIR_CRATE")
if wanted and crate == wanted and binary:
    os.environ["RUSTC_BOOTSTRAP"] = crate
    args += [
        "-Zmir-opt-level=0",
        "-Zdump-mir=all & PreCodegen",
        "-Zdump-mir-dir=target/peeky-mir",
        "-Zmir-include-spans=yes",
        "-Zunstable-options",
        "-Csymbol-mangling-version=legacy",
    ]
os.execv(args[0], args)
"""


def mir_wrapper(directory):
    """Write the wrapper into ``directory``; returns (wrapper, its exact text).

    Its interpreter is this Python through a short symlink beside it, so the shebang
    has no spaces or length limits, run isolated (-I) from PYTHON* variables.
    """
    directory = Path(directory)
    interpreter = directory / "python"
    interpreter.symlink_to(Path(sys.executable).resolve())
    text = f"#!{interpreter} -I\n{MIR_WRAPPER}"
    wrapper = directory / "rustc-mir"
    wrapper.write_text(text)
    wrapper.chmod(0o700)
    return wrapper, text


TEST_TIMEOUT_S = 30
_CARGO_STDERR = re.compile(
    r" *Compiling [a-z0-9_-]{1,64} v[0-9.]{1,64} \(.{1,4096}\)"
    r"|error: could not compile `[a-z0-9_-]{1,64}` \(bin \"[a-z0-9_-]{1,64}\"\) "
    r"due to [0-9]{1,6} previous errors?(?:; [0-9]{1,6} warnings? emitted)?"
    r"|warning: `[a-z0-9_-]{1,64}` \(bin \"[a-z0-9_-]{1,64}\"\) generated [0-9]{1,6} warnings?"
)
SYMBOL_BUDGET_S = 10
_SUMMARY = re.compile(
    r"test result: (ok|FAILED)\. ([0-9]{1,6}) passed; ([0-9]{1,6}) failed; ([0-9]{1,6}) ignored; "
    r"([0-9]{1,6}) measured; ([0-9]{1,6}) filtered out; finished in [0-9]{1,6}\.[0-9]{1,3}s"
)


def _remove_owned(parent, error=None):
    """Remove the owned build parent; record failure on an active error, else raise."""
    shutil.rmtree(parent, ignore_errors=True)
    if Path(parent).exists():
        message = f"Rust build directory was not removed: {parent}"
        if error is None:
            raise RuntimeError(message)
        error.add_note(message)


def _absolute_tool(path, label):
    if not isinstance(path, str) or not path or "\x00" in path or not Path(path).is_absolute():
        raise ValueError(f"Rust {label} must be an absolute executable path")
    resolved = real_tool(path)
    if not resolved.is_file():
        raise ValueError(f"Rust {label} must be an executable file")
    return str(resolved)


def _toolchain(cargo, rustc):
    """Require real sysroot binaries; hashing a rustup proxy does not identify rustc."""
    cargo, rustc = _absolute_tool(cargo, "cargo"), _absolute_tool(rustc, "rustc")
    cargo_path, rustc_path = Path(cargo), Path(rustc)
    if (
        cargo_path.name != "cargo"
        or rustc_path.name != "rustc"
        or cargo_path.parent != rustc_path.parent
        or not (rustc_path.parent.parent / "lib/rustlib").is_dir()
    ):
        raise ValueError("Rust cargo/rustc must be the same real toolchain sysroot binaries")
    return cargo, rustc


def _executable(names, crate, label):
    found = {
        name
        for name in names
        if re.fullmatch(re.escape(f"{DEPS}/{crate}-") + r"[0-9a-f]{16}", name)
    }
    if len(found) != 1:
        raise ValueError(f"Expected exactly one Cargo {label} executable")
    return found.pop()


def cargo_artifacts(names, crate, test_crate):
    """Select the hashed application/test executables and symbols from fresh deps files;
    the test is None when the crate has no integration test (``test_crate`` None)."""
    if crate == test_crate:
        raise ValueError("Cargo application and test crates must have distinct names")
    binary = _executable(names, crate, "application")
    test = None if test_crate is None else _executable(names, test_crate, "integration-test")
    executables = (binary,) if test is None else (binary, test)
    allowed = {name + suffix for name in executables for suffix in ("", ".d", ".ll")}
    # rustc keeps its codegen-unit objects when it also emits LLVM IR.
    units = re.compile(
        "(?:" + "|".join(map(re.escape, executables)) + r")\.[A-Za-z0-9_]+\.[0-9a-f]{16}"
        r"-cgu\.[0-9]{1,4}\.rcgu\.o"
    )
    bundles = tuple(name + ".dSYM/" for name in executables)
    for name in names:
        if name in allowed or units.fullmatch(name):
            continue
        if sys.platform == "darwin" and name.startswith(bundles):
            continue
        raise ValueError("Unexpected Cargo build artifact")
    if sys.platform == "darwin":
        symbols = f"{bundles[0]}Contents/Resources/DWARF/{Path(binary).name}"
        if symbols not in names:
            raise ValueError("Missing Cargo packed debug symbols")
        return binary, symbols, test
    return binary, binary, test


def compiler_diagnostic(result, sources):
    """Retry only when every rustc error has its primary span in a declared source.

    Uses Cargo's JSON messages from stdout and scans all of them before deciding.
    Span-less errors (linking, driver), errors elsewhere, internal compiler errors,
    malformed messages or spans and possibly truncated output are not retryable.
    """
    stdout, stderr = result.get("stdout", ""), result.get("stderr", "")
    if (
        result.get("exit_code") != 101
        or result.get("timed_out")
        or result.get("output_limit_exceeded")
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or len(stdout.encode()) >= BUILD_RETAINED
        or len(stderr.encode()) >= BUILD_RETAINED
        or not stdout.endswith("\n")
    ):
        return False
    # Cargo's stderr for an ordinary compile failure is only progress and its footer.
    if not all(
        _CARGO_STDERR.fullmatch(line) for line in stderr.splitlines() if line.strip()
    ) or not any(line.startswith("error: could not compile") for line in stderr.splitlines()):
        return False
    lines = stdout.splitlines()
    try:
        final = json.loads(lines[-1])
    except (IndexError, ValueError, RecursionError):
        return False
    if final != {"reason": "build-finished", "success": False}:
        return False
    errors = 0
    for line in lines[:-1]:
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return False
        if not isinstance(message, dict) or message.get("reason") == "build-finished":
            return False
        if message.get("reason") != "compiler-message":
            continue
        diagnostic = message.get("message")
        if not isinstance(diagnostic, dict) or diagnostic.get("level") not in (
            "error",
            "warning",
            "note",
            "help",
            "failure-note",
        ):
            return False
        if diagnostic["level"] != "error":
            continue
        text, spans = diagnostic.get("message", ""), diagnostic.get("spans")
        if (
            not isinstance(text, str)
            or "internal compiler error" in text.lower()
            or not isinstance(spans, list)
            or not all(isinstance(span, dict) for span in spans)
        ):
            return False
        primary = [span for span in spans if span.get("is_primary") is True]
        if not primary or any(span.get("file_name") not in sources for span in primary):
            return False
        errors += 1
    return errors > 0


def libtest_summary(result):
    """Interpret one bounded libtest run; anything but an explicit clean pass fails."""
    lines = [line.strip() for line in result.get("stdout", "").splitlines() if line.strip()]
    summaries = [match for line in lines if (match := _SUMMARY.fullmatch(line))]
    # The harness prints its summary last; an earlier or extra summary is forged.
    if summaries and not _SUMMARY.fullmatch(lines[-1]):
        summaries = []
    summary = None
    if len(summaries) == 1:
        status, passed, failed, ignored, measured, filtered = summaries[0].groups()
        summary = {
            "status": status,
            "passed": int(passed),
            "failed": int(failed),
            "ignored": int(ignored),
            "measured": int(measured),
            "filtered_out": int(filtered),
        }
    clean = (
        summary is not None
        and result.get("exit_code") == 0
        and not result.get("timed_out")
        and not result.get("output_limit_exceeded")
        and summary["status"] == "ok"
        and summary["passed"] > 0
        and summary["failed"] == 0
        and summary["ignored"] == 0
        and summary["measured"] == 0
        and summary["filtered_out"] == 0
    )
    failures = (
        _libtest_failures(result.get("stdout", ""), summary["failed"])
        if summary and not clean
        else []
    )
    return {"summary": summary, "passed": clean, **({"failures": failures} if failures else {})}


def _libtest_failures(stdout, limit):
    """Names of failed tests and the start of each one's captured output, for people;
    never part of the pass/fail decision.

    Names come from libtest's final `failures:` list, printed after every test has
    finished (result lines can be interleaved with a program's own output); never more
    than the summary's failed count. Each message is the test's captured output, from
    its line-anchored `---- name stdout ----` block before that list.
    """
    details, header, listing = stdout.rpartition("\nfailures:\n")
    if not header:
        return []
    names = [
        line.strip()
        for line in listing.split("\n\n", 1)[0].splitlines()
        if line.startswith("    ") and line.strip()
    ][: min(limit, 10)]
    failures = []
    for name in names:
        marker = f"\n---- {name} stdout ----\n"
        index = details.rfind(marker)
        message = ""
        if index >= 0:
            message = details[index + len(marker) :].split("\n---- ", 1)[0]
            message = re.sub(r"\n?note: run with `RUST_BACKTRACE=1`.*", "", message).strip()
        failures.append({"name": name[:200], "message": message[:500]})
    return failures


@dataclass(frozen=True)
class RustTarget:
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    cargo: str
    rustc: str
    adapter: str
    dwarfdump: str
    linker: str
    dsymutil: str | None = None
    sysroot: str | None = None
    arguments: tuple[str, ...] = ()
    rust_version: str = "1.96.0"
    # Opt-in: optimized code can report locals as unavailable or show stale values.
    opt_level: str = "0"

    def __post_init__(self):
        if self.opt_level not in ("0", "1", "2"):
            raise ValueError("Rust opt_level must be 0, 1 or 2")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("Rust sources must be a nonempty immutable tuple")
        # A crate without an integration test is verified by its checker alone.
        if not isinstance(self.tests, tuple):
            raise ValueError("Rust tests must be an immutable tuple")
        for name in (*self.sources, *self.tests):
            _path_name(name, ".rs")
        self.validate_arguments(self.arguments)
        if not isinstance(self.rust_version, str) or not re.fullmatch(
            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", self.rust_version
        ):
            raise ValueError("Invalid pinned Rust version")
        _toolchain(self.cargo, self.rustc)
        for label in ("adapter", "dwarfdump", "linker"):
            _absolute_tool(getattr(self, label), label)
        if self.dsymutil is not None:
            _absolute_tool(self.dsymutil, "dsymutil")
        self._dsymutil()

    accepts_arguments = True

    @staticmethod
    def validate_arguments(arguments):
        if (
            not isinstance(arguments, tuple)
            or len(arguments) > 100
            or any(
                not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192 for arg in arguments
            )
        ):
            raise ValueError("Invalid Rust application arguments")

    language = "rust"

    @property
    def verification_inputs(self):
        return ("Cargo.toml", "Cargo.lock", *self.sources, *self.tests)

    @property
    def verification_sources(self):
        return self.sources

    @property
    def rustflags(self):
        flags = ["-C", "dwarf-version=4", "-C", f"linker={_absolute_tool(self.linker, 'linker')}"]
        # The LLVM IR shows which locals live in memory (mutation must reach the
        # program, not a debug-only copy); it is written beside each executable.
        flags += ["--emit=link,llvm-ir"]
        if sys.platform == "linux":
            flags += ["-C", "link-arg=-Wl,--build-id=sha1"]
        return flags

    @property
    def target_identity(self):
        return {
            "language": "rust",
            "sources": sorted(self.sources),
            "tests": sorted(self.tests),
            "arguments": list(self.arguments),
            "rust_version": self.rust_version,
            "rustflags": self.rustflags,
            "profile": self.profile,
            "mir_wrapper_sha256": hashlib.sha256(MIR_WRAPPER.encode()).hexdigest(),
        }

    @property
    def profile(self):
        return {
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_DEV_OPT_LEVEL": self.opt_level,
            "CARGO_PROFILE_DEV_DEBUG": "2",
            "CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "packed" if sys.platform == "darwin" else "off",
            "CARGO_PROFILE_DEV_STRIP": "none",
        }

    def validate_breakpoints(self, points):
        PythonAdapter().validate_breakpoints(points)
        if any(set(point) != {"line"} for point in points):
            raise ValueError("Rust targets support unconditional line breakpoints only")

    def _dsymutil(self):
        if sys.platform != "darwin":
            return None
        if self.dsymutil is None or Path(self.dsymutil).name != "dsymutil":
            raise ValueError("macOS Rust targets require an explicit tool named dsymutil")
        return _absolute_tool(self.dsymutil, "dsymutil")

    def _environment(self, rustc):
        # rustc runs `dsymutil` from PATH for packed macOS symbols. Put the declared
        # tool's directory first and require PATH resolution to select exactly it.
        tools = [str(Path(rustc).parent)]
        if dsymutil := self._dsymutil():
            tools.insert(0, str(Path(dsymutil).parent))
        env = {
            "PATH": ":".join((*dict.fromkeys(tools), "/usr/bin", "/bin")),
            "CARGO_HOME": ".peeky-cargo-home",
            "CARGO_TARGET_DIR": "target",
            "RUSTC": rustc,
            "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(self.rustflags),
            "CARGO_TERM_COLOR": "never",
            "LC_ALL": "C",
            **self.profile,
        }
        if self.sysroot is not None:
            sdk = Path(self.sysroot).resolve(strict=True)
            if not sdk.is_dir():
                raise ValueError("Rust sysroot must be a directory")
            env["SDKROOT"] = str(sdk)
        if dsymutil:
            selected = shutil.which("dsymutil", path=env["PATH"])
            if not selected or str(Path(selected).resolve()) != dsymutil:
                raise ValueError("PATH does not select the declared dsymutil")
        return env

    supports_deadline = True
    # Candidate probes stage an edit into a fresh build (the worker's launch `edit`).
    supports_edits = True

    def prepare(self, program, root, *, edit=None, deadline=None):
        root = Path(root).resolve(strict=True)
        relative = Path(program).resolve(strict=True).relative_to(root).as_posix()
        if relative not in self.sources:
            raise ValueError("Program is not a declared Rust application source")
        if edit is not None and edit.path not in self.sources:
            raise ValueError("Rust candidate edits require a declared application source")
        cargo_inputs = validate_cargo_inputs(root, sources=self.sources, tests=self.tests)
        cargo, rustc = _toolchain(self.cargo, self.rustc)
        adapter, dwarfdump, linker = (
            _absolute_tool(self.adapter, "adapter"),
            _absolute_tool(self.dwarfdump, "dwarfdump"),
            _absolute_tool(self.linker, "linker"),
        )
        build = Command(
            (
                cargo,
                "build",
                "--offline",
                "--locked",
                "--message-format=json-diagnostic-short",
                "--bin",
                cargo_inputs.binary,
            ),
            timeout_s=120,
        )
        # Compiles only the frozen test target; the application is already fresh.
        tests = (
            Command(
                (cargo, "test", "--no-run", "--offline", "--locked", "--test", cargo_inputs.test),
                timeout_s=120,
            )
            if cargo_inputs.test
            else None
        )
        probes = [
            Command((rustc, "-vV")),
            Command((cargo, "--version")),
            Command((adapter, "--help")),
            Command((dwarfdump, "--version")),
            Command((linker, "--version")),
        ]
        # Resolved once: every later PATH lookup must still select exactly these bytes.
        dsymutil = self._dsymutil()
        if sys.platform == "darwin":
            probes.append(Command((dsymutil, "--version")))
        elif sys.platform != "linux":
            raise ValueError("Rust target supports Linux and macOS hosts only")
        environment = self._environment(rustc)
        uplift = f"target/debug/{cargo_inputs.binary}"
        crate = cargo_inputs.binary.replace("-", "_")
        artifacts = (
            ArtifactSpec(DEPS, "directory", "cargo-deps"),
            ArtifactSpec(uplift, role="cargo-test-target"),
        )
        wrapper = wrapper_text = None

        def validate_workspace(workspace, env):
            # Before every probe and Cargo command: revalidate the exact staged bytes
            # Cargo will read (not the earlier preflight), ancestor configuration and
            # the helper rustc will select from PATH.
            staged = validate_cargo_inputs(workspace, sources=self.sources, tests=self.tests)
            if staged != cargo_inputs:
                raise RuntimeError("Staged Cargo inputs differ from validated preflight")
            assert_cargo_ancestry_clear(workspace)
            if wrapper is not None and (
                env.get("RUSTC_WORKSPACE_WRAPPER") != str(wrapper)
                or wrapper.is_symlink()
                or wrapper.read_text() != wrapper_text
                or Path(wrapper.parent / "python").resolve() != Path(sys.executable).resolve()
            ):
                raise RuntimeError("The Rust MIR wrapper changed during preparation")
            if dsymutil is not None:
                selected = shutil.which("dsymutil", path=env.get("PATH"))
                if (
                    not selected
                    or str(Path(selected).resolve()) != dsymutil
                    # The declared path itself must still name the tool PATH runs.
                    or str(real_tool(self.dsymutil)) != dsymutil
                ):
                    raise RuntimeError("Cargo dsymutil PATH resolution changed during preparation")

        def before_command(workspace, results, command):
            # Probe output must prove the pinned versions before any Cargo command runs.
            versions = {probe.argv[0]: probe.stdout for probe in results}
            if not versions[rustc].startswith(f"rustc {self.rust_version} ") or not versions[
                cargo
            ].startswith(f"cargo {self.rust_version} "):
                raise ValueError("Rust toolchain does not match the pinned version")

        # Cargo also reads ancestor configuration; own a checked parent directory.
        parent = Path(tempfile.mkdtemp(prefix="peeky-rust-")).resolve()
        try:
            assert_cargo_ancestry_clear(parent)
            # Outside the workspace, in Peeky's own directory; checked before every
            # command (validate_workspace). Optimized builds have it too: their bindings'
            # types decide what a Box is (#111), though nothing there is ever proven.
            wrapper, wrapper_text = mir_wrapper(parent)
            environment = {
                **environment,
                "RUSTC_WORKSPACE_WRAPPER": str(wrapper),
                "PEEKY_MIR_CRATE": crate,
            }
            artifacts += (ArtifactSpec(MIR_DIR, "directory", "rust-mir"),)
            recipe = BuildRecipe(
                "rust",
                cargo_inputs.inputs,
                (build, tests) if tests else (build,),
                artifacts,
                tuple(probes),
                tuple(sorted(environment.items())),
            )
            try:
                prepared = prepare(
                    recipe,
                    root,
                    parent=parent,
                    edit=edit,
                    deadline=deadline,
                    before_command=before_command,
                    validate_workspace=validate_workspace,
                )
            except BuildCommandError as exc:
                if exc.command != build.argv or not compiler_diagnostic(exc.result, self.sources):
                    raise RuntimeError("Rust toolchain or Cargo infrastructure failed") from exc
                raise
        except BaseException as exc:
            _remove_owned(parent, exc)
            raise
        try:
            if deadline is not None and deadline - time.monotonic() <= 0:
                raise TimeoutError("Rust preparation exceeded its overall deadline")
            names = [
                item.path
                for item in prepared.manifest.artifacts
                if item.path != uplift and not item.path.startswith(MIR_DIR + "/")
            ]
            binary, symbols, test_binary = (
                None if name is None else prepared.workspace / name
                for name in cargo_artifacts(
                    names, crate, cargo_inputs.test and cargo_inputs.test.replace("-", "_")
                )
            )
            if _digest(binary)[0] != _digest(prepared.workspace / uplift)[0]:
                raise ValueError("Cargo test target differs from the debugged executable")
            _require_declared_dependencies(
                tuple(path for path in (binary, test_binary) if path is not None),
                prepared.workspace,
                cargo_inputs.inputs,
            )
            declarations = {}
            memory, parameters = ir_locals(binary.with_name(binary.name + ".ll"), declarations)
            owned = None
            if wrapper is not None:
                owned = owned_locals(
                    prepared.workspace,
                    [item.path for item in prepared.manifest.artifacts],
                    crate,
                    binary.with_name(binary.name + ".ll"),
                    self.sources,
                )
            env = dict(prepared.manifest.effective_environment)
            lines, layouts, units, blocks = application_symbols(
                dwarfdump,
                symbols,
                prepared.workspace,
                env,
                entrypoint=cargo_inputs.entrypoint,
                crate=crate,
                declared=self.sources,
                budget=SYMBOL_BUDGET_S
                if deadline is None
                else min(SYMBOL_BUDGET_S, deadline - time.monotonic()),
            )
            rust = (*self.sources, *self.tests)
            texts = {
                # Every file the build may read (_require_declared_dependencies).
                name: (prepared.workspace / name).read_bytes().decode("utf-8", errors="replace")
                for name in cargo_inputs.inputs
            }
            layouts = without_numbered_structs(
                layouts,
                [texts[name] for name in rust],
                [text for name, text in texts.items() if name not in rust],
            )
            identity = module_identity(
                dwarfdump, binary, symbols, prepared.workspace, env, deadline=deadline
            )
            session = RustSession(
                self,
                prepared,
                adapter,
                lines,
                identity,
                binary,
                symbols,
                layouts,
                parent,
                memory,
                parameters,
                owned,
            )
            session.test_binary = test_binary
            session.test_target = prepared.workspace / uplift
            session.units = tuple(units)
            session.code_blocks = blocks
            # Each local's IR declarations, to say why a name is not in memory (#138).
            session.declarations_ir = declarations
            session.assert_current()
            return session
        except BaseException as exc:
            try:
                prepared.cleanup()
            except Exception as cleanup:
                exc.add_note(f"Rust workspace cleanup failed: {cleanup}")
            finally:
                _remove_owned(parent, exc)
            raise


def owned_locals(workspace, artifacts, crate, ir, sources):
    """The owned-local proofs (rust_mir) for this build, from its hashed MIR dumps."""
    mir = [name for name in artifacts if name.startswith(MIR_DIR + "/")]
    pattern = re.compile(
        re.escape(f"{MIR_DIR}/{crate}.") + r".+\.PreCodegen\.(?:before|after)\.mir"
    )
    if not mir or not all(pattern.fullmatch(name) for name in mir):
        raise ValueError("Unexpected Rust MIR artifact")
    # Any mention outside comments (`#[collapse_debuginfo]`, inside `cfg_attr`); a
    # string naming it only counts too much.
    collapsed = any(
        _COLLAPSE.search(text) and _COLLAPSE.search(without_comments(text))
        for name in sources
        for text in [(Path(workspace) / name).read_text(errors="replace")]
    )
    return OwnedLocals(
        load_bodies(Path(workspace) / MIR_DIR, crate),
        ir_functions(ir, sources),
        sources,
        precise=not collapsed,
        # Bytes, so lines split on `\n` alone as rustc counts them (no universal newlines).
        texts={
            name: (Path(workspace) / name).read_bytes().decode("utf-8", errors="replace")
            for name in sources
        },
    )


def _require_declared_dependencies(executables, workspace, inputs):
    """Every file the compiler read (rustc's dependency files) must be declared.

    `#[path = "/elsewhere"] mod x;`, `include!` and `include_str!` would otherwise
    compile bytes that no provenance check covers.
    """
    root = Path(workspace).resolve()
    for executable in executables:
        depfile = executable.with_name(executable.name + ".d")
        if not depfile.is_file():
            raise ValueError(f"Cargo wrote no dependency file for {executable.name}")
        for dependency in dependency_paths(depfile.read_text(), root):
            try:
                relative = dependency.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                relative = None
            if relative not in inputs:
                raise ValueError(
                    f"The Rust build read {dependency}, which is not a declared source; "
                    "declare it as a source or test inside the crate, or remove the "
                    "#[path]/include! reference"
                )


# LLDB's names for Rust integers (i16/u16, i32/u32, i64/u64, isize/usize) on LP64.
RUST_INTEGERS = frozenset(
    {"short", "unsigned short", "int", "unsigned int", "long", "unsigned long"}
)


_HASH_IN = re.compile(r"17(h[0-9a-f]{16})E")

# The MIR's names for the integers `set` changes (as RUST_INTEGERS are LLDB's).
_SETTABLE_RUST = frozenset({"i16", "i32", "i64", "u16", "u32", "u64", "isize", "usize"})


def _state_held(body, name):
    """True if every binding of ``name`` in the MIR ``body`` of an async body is kept in
    its state: a local the coroutine layout saves across an `.await` (``saved``), or a
    field of the state (a parameter's, `debug n => ((*_36).0: i32)`). The poll reaches
    it through a pointer at every stop."""
    if body is None or body.coroutine is None:
        return False
    state = re.compile(rf"\(+\*_{body.coroutine}\)")
    bindings = body.bindings.get(name, ())
    return bool(bindings) and all(
        binding[0] in body.saved
        if binding[0] is not None
        else bool(state.match(body.places.get((name, *binding[1:3], binding[4]), "")))
        for binding in bindings
    )


def _plain(name):
    """A local's name without where LLDB says it is declared (`s @ main.rs:81` is `s`,
    when several of that name are in scope); the IR's memory proof covers every local
    of a name."""
    return str(name).partition(" @ ")[0]


# LLDB's names for a pointer to a Rust u8 (`unsigned char`), i8 (`char`, `signed char`)
# or char (`char32_t`), whose value it follows with a C-string reading.
_CHAR_POINTER = re.compile(
    r"(?:const )?(?:unsigned char|signed char|char|char8_t|char16_t|char32_t)(?: const)? \*"
)
_ADDRESS = re.compile(r"(0x[0-9a-fA-F]+)(?:\s|$)")


def _frame_hash(frame):
    # lldb-dap marks an optimized frame's name ` [opt]` (macOS).
    name = re.sub(r"\s*\[opt\]$", "", str((frame or {}).get("name") or ""))
    return re.search(r"::(h[0-9a-f]{16})$", name)


def _same_lent_rows(copies, variable, siblings, read):
    """True if every row shown under the variable's name holds the same bytes, where
    the MIR says they are an async body's parameter and its copies (``copies``: bytes,
    rows; see OwnedLocals.lent_copies, #116): then each shows what the caller lent."""
    if not copies:
        return False
    width, count = copies
    rows = [row for row in siblings if row.get("name") == variable.get("name")]
    if len(rows) != count or not any(row is variable for row in rows):
        return False
    shown = [str(row.get("value") or "") for row in rows]
    if width == 8 and all(re.fullmatch(r"0x[0-9a-fA-F]{1,16}", value) for value in shown):
        # A thin reference shows the pointer it holds, all of its bytes: so lldb-dap 18,
        # which cannot read memory, can be told too.
        return len({int(value, 16) for value in shown}) == 1
    try:
        held = {bytes(read(row.get("memoryReference"), width)) for row in rows}
    except Exception:
        return False
    return len(held) == 1


def _holds_data_pointers(kind):
    """Whether LLDB's type is a data pointer, or an array of them (`Pt *[2]`, which a
    `[Box<Pt>; 2]` is too): what a Box may be. A fn pointer (`long (*)(long)`, `long
    (*[2])(long)`) points at code, never at a Box's allocation."""
    levels = list(itertools.dropwhile(lambda level: level == "array", _constructors(kind)))
    return levels[:1] == ["pointer"] and levels[1:2] != ["function"]


def _constructors(kind):
    """What LLDB's C-style type is, outermost first, one of "pointer", "array" or
    "function" per declarator level: `long (*[2])(long)` is an array of pointers to
    functions, ["array", "pointer", "function"]; a plain type is []."""
    text = _without_generics(kind).rstrip()
    start = _declarator_start(text)
    if start is None:
        # No `(*…)` group: trailing stars and array suffixes (`Pt *[2]`, `Pt *const`).
        tail = re.search(r"((?:\*\s*(?:const\b\s*)?)*)((?:\[\d*\]\s*)*)$", text)
        return ["array"] * tail[2].count("[") + ["pointer"] * tail[1].count("*")
    return _declarator(text[start:])


def _declarator(text):
    """A C abstract declarator's levels: `*`s, then an optional `(…)` group holding an
    inner declarator, then suffixes (`[N]`, `(parameters)`). The inner declarator's
    levels come first, then the suffixes', then the stars'."""
    stars = re.match(r"(?:\s|\*|const\b|volatile\b|restrict\b)*", text)
    rest = text[stars.end() :]
    inner = []
    if re.match(r"\(\s*\*", rest):
        group = _group_at(rest, 0)
        if group is None:
            return []
        inner, rest = _declarator(group), rest[len(group) + 2 :]
    suffixes = []
    while rest := rest.lstrip():
        if rest[0] == "[" and "]" in rest:
            suffixes.append("array")
            rest = rest[rest.index("]") + 1 :]
        elif rest[0] == "(" and (group := _group_at(rest, 0)) is not None:
            suffixes.append("function")
            rest = rest[len(group) + 2 :]
        else:
            break
    return inner + suffixes + ["pointer"] * stars[0].count("*")


def _without_generics(kind):
    """LLDB's type name without generic arguments (`Vec<Pt *>` is `Vec`)."""
    # `->` would close a generic's `<` (`W<(u8, fn() -> u8)>`).
    text = str(kind or "").replace("->", " ")
    while (stripped := re.sub(r"<[^<>]*>", "", text)) != text:
        text = stripped
    return text


def _is_wide_box(kind):
    """Whether LLDB's type is a wide Box (`alloc::boxed::Box<str>`, which LLDB names
    itself) or an array of them; not a fn pointer returning one (`alloc::boxed::Box<dyn
    Shape> (*[2])()`) or an array of references to one (`alloc::boxed::Box<str> *[2]`),
    which LLDB's C-style names also begin with it."""
    return bool(
        re.fullmatch(r"alloc::boxed::Box\s*(?:\[\d+\]\s*)*", _without_generics(kind).strip())
    )


def _declarator_start(text):
    """Where the declarator's `(*…)` group opens: the last one outside any parentheses or
    brackets with a base type before it. A parameter list's `(*)` (`unsigned char
    (*[2])(unsigned char (*)[4])`) never counts, nor does a tuple base that starts like
    one (`(*const u8, i64) (*)[2]`). A `(` right after `)` opens a parameter list, which
    can start like one too (`void (**)(*const [u8])`)."""
    depth, last = 0, None
    for at, char in enumerate(text):
        if char in "([":
            if (
                depth == 0
                and char == "("
                and text[at + 1 :].lstrip().startswith("*")
                and text[:at].strip()
                and not text[:at].endswith(")")
            ):
                last = at
            depth += 1
        elif char in ")]":
            depth -= 1
    return last


def _group_at(text, at):
    """The contents of the parenthesized group that opens at ``at``."""
    level = 0
    for end in range(at, len(text)):
        level += {"(": 1, ")": -1}.get(text[end], 0)
        if level == 0:
            return text[at + 1 : end]
    return None


# MIR types that do not own what they point at (`for<'a> fn(&'a T)` has a binder).
_NON_OWNING = re.compile(r"&|\*const |\*mut |(?:for<[^<>]*> )?(?:unsafe )?(?:extern \S+ )?fn\(")
_PRIMITIVES = frozenset(
    {
        *(f"{sign}{bits}" for sign in "iu" for bits in ("8", "16", "32", "64", "128", "size")),
        *("f16", "f32", "f64", "f128", "bool", "char", "()", "!"),
    }
)


def _may_own(kind, generics=()):
    """Whether a value of MIR type ``kind`` may own memory it reaches through a pointer
    (a Box, a Vec's or String's buffer, an Rc's allocation): anything but a primitive,
    a reference, a raw or fn pointer, or a tuple or array made only of those. A struct's
    or enum's name says nothing of its fields, and None (a type not written out) may be
    anything (#108). In a generic body (``generics``: its type parameters, None if
    unknown) a primitive's name may be a type parameter's (`fn keep<i64>(h: i64)`)."""
    if kind is None:
        return True
    kind = kind.strip()
    if _NON_OWNING.match(kind):
        return False
    if kind in _PRIMITIVES and generics is not None and kind not in generics:
        return False
    if kind.startswith("[") and kind.endswith("]"):
        # `[T; N]` or `[T]`: the element's type.
        return _may_own(_top_level_parts(kind[1:-1], ";")[0], generics)
    if kind.startswith("(") and kind.endswith(")"):
        parts = [part for part in _top_level_parts(kind[1:-1], ",") if part.strip()]
        return any(_may_own(part, generics) for part in parts)
    return True


def _top_level_parts(text, separator):
    """``text`` split at each ``separator`` outside brackets of any kind."""
    # `->` would close a bracket (`(fn(i64) -> i64, Pt)`).
    masked = text.replace("->", "  ")
    parts, depth, start = [], 0, 0
    for at, char in enumerate(masked):
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:at])
            start = at + 1
    return [*parts, text[start:]]


def _unproven(variable):
    """``variable`` shown only as its pointer (#101, #108)."""
    # Only the address: LLDB's summary of a `char *` (`Box<u8>`) is read through the
    # pointer too.
    address = re.match(r"0x[0-9a-fA-F]+", str(variable.get("value") or ""))
    return {
        "name": variable.get("name"),
        "type": variable.get("type"),
        "value": address[0] if address else "<not shown: may point at freed memory>",
        "variablesReference": 0,
        "rust_view": "unproven",
    }


class RustSession(NativeSession):
    def __init__(
        self,
        target,
        prepared,
        adapter,
        lines,
        identity,
        binary,
        symbols,
        layouts,
        parent,
        memory=None,
        parameters=None,
        owned=None,
    ):
        self._layouts_data = json.dumps(layouts, sort_keys=True)
        # Proofs that an owned Vec/String local is initialized at a stop (rust_mir).
        self.owned = owned
        self.parent = parent
        # {(function hash, local): True if every declaration is a real memory slot}.
        self.memory = dict(memory or {})
        # {(function hash, parameter): {"type", "line"}}: reference parameters whose
        # contents are live past "line" (rust_ir); only these are read from memory.
        self.parameters = dict(parameters or {})
        super().__init__(target, prepared, adapter, lines, identity, binary, symbols)
        self.manifest_id = hashlib.sha256(
            json.dumps(
                {"native": self.manifest_id, "enum_layouts": layouts}, sort_keys=True
            ).encode()
        ).hexdigest()

    @property
    def layouts(self):
        return json.loads(self._layouts_data)

    units = ()
    # {legacy hash: the function's CodeBlocks} (rust_symbols.code_blocks; #115).
    code_blocks = {}

    def function_scopes(self):
        """Functions of the application units only, as `prepare` read them: the whole
        binary's debug info is mostly the standard library (megabytes)."""
        dwarfdump = _absolute_tool(self.target.dwarfdump, "dwarfdump")
        env = dict(self.prepared.manifest.effective_environment)
        workspace, symbols = self.prepared.workspace, str(self.symbols)
        infos, tables = [], []
        for unit in self.units:
            tables.append(
                _metadata(
                    dwarfdump, [f"--debug-line=0x{unit.stmt_list:08x}", symbols], workspace, env
                )
            )
            infos.append(
                _metadata(
                    dwarfdump,
                    [f"--debug-info=0x{unit.die:08x}", "--show-children", symbols],
                    workspace,
                    env,
                    # A unit holds every std generic the crate instantiates (`sort_by_key`,
                    # iterators): megabytes for a small crate, as `prepare` allows.
                    max_bytes=RUST_METADATA,
                )
            )
        if not infos:
            raise ValueError("No application units were recorded for this build")
        return function_scopes(
            "\n".join(infos),
            "\n".join(tables),
            workspace,
            tuple(self.lines),
            hidden=self._macro_binding(),
            never=self._register_names,
        )

    def _register_names(self, linkages, names):
        """For `describe` (#138): of a function's integer locals (``names``: {name:
        types}), those no stop can change, and why: `register` where the build's IR keeps
        one in a register, `async state` where an async body keeps it across an `.await`
        in its state (see never_settable)."""
        if self._optimized() or not self.memory:
            return {}
        hashes = {found[1] for linkage in linkages if (found := _HASH_IN.search(linkage or ""))}
        if not hashes:
            return {}
        proofs = getattr(self, "owned", None)
        bodies = [proofs.body(hashed) if proofs is not None else None for hashed in hashes]
        return {
            name: ("async state" if all(_state_held(body, name) for body in bodies) else "register")
            for name, kinds in names.items()
            if kinds
            and set(kinds) <= _SETTABLE_RUST
            and all(self.memory.get((hashed, name)) is not True for hashed in hashes)
        }

    def _macro_binding(self):
        """For `describe`, which bindings the DWARF declares are a macro's own (sixth
        evaluation, finding 12): `println!`'s `args` (`core::fmt::rt::Argument[1]`),
        which rustc places on the line invoking the macro. Told per function by its own
        MIR body (the IR function its linkage name is), which places each binding where
        its code is: a name the body binds in another crate's file (std's `macros.rs`),
        declared where the body places no binding of that name in the program's
        sources, while every binding of the name the body places there has a
        declaration of its own (one moved to the invoking line, by a
        `#[collapse_debuginfo(yes)]` macro of the crate's, keeps them all). Shown when
        unsure: a function with no body matched, and anything without the MIR (an
        optimized build)."""
        proofs = getattr(self, "owned", None)
        if proofs is None:
            return None
        declared = set(self.lines)

        def hidden(linkages, positions):
            bodies = []
            for linkage in linkages:
                found = re.search(r"17h([0-9a-f]{16})E", linkage or "")
                if found and (body := proofs.body("h" + found[1])) is not None:
                    bodies.append(body)
            if not bodies:
                return None
            own, foreign = {}, set()
            for body in bodies:
                for name, bindings in body.bindings.items():
                    for _, file, line, _, _ in bindings:
                        if file in declared:
                            own.setdefault(name, set()).add((file, line))
                        else:
                            foreign.add(name)

            def left_out(source, name, line):
                places = own.get(name, set())
                if name not in foreign or source is None or (source, line) in places:
                    return False
                at = {(where, number) for where, each, number in positions if each == name}
                return places <= at

            return left_out

        return hidden

    def launch_args(self, program, root, args=None):
        return {
            **super().launch_args(program, root, args),
            # Never execute Python scripts embedded beside symbol files.
            "initCommands": ["settings set target.load-script-from-symbol-file false"],
        }

    # The rules behind a [settable] mark (mid_statement, _root_holds, validate_member)
    # read the stopped frame alone, never the frames below it: marks are kept by it.
    marks_by_frame = True

    def _optimized(self):
        return getattr(getattr(self, "target", None), "opt_level", "0") != "0"

    def settable(self, variable, frame=None):
        """A Rust integer local the program keeps in memory (see validate_mutation_target)."""
        if self._optimized():
            return False
        if variable.get("type") not in RUST_INTEGERS or not _writable_integer(variable):
            return False
        hashed = _frame_hash(frame)
        return bool(hashed) and self.memory.get((hashed[1], _plain(variable.get("name")))) is True

    def validate_variable(self, variable, value):
        if (kind := variable.get("type")) not in RUST_INTEGERS:
            raise ValueError(
                f"Only writable integer locals (i16 to i64, u16 to u64, isize, usize) support "
                f"Rust mutation: {variable.get('name')!r} has debugger type {kind!r}"
            )
        super().validate_variable(variable, value)

    def validate_mutation_target(self, variable, frame):
        """Only a local the program keeps in memory can be changed with effect."""
        name = variable.get("name")
        if self._optimized():
            # Which locals stay in memory is read from an unoptimized build's IR.
            raise ValueError(
                f"Rust mutation needs an unoptimized build (opt-level 0): {name!r} may be "
                "kept in a register there, and changing what the debugger shows would not "
                "affect the program"
            )
        if reason := self.not_yet_declared(variable, frame):
            raise ValueError(
                f"{name!r} has no value here: {reason}. The program assigns it before any "
                "read, so a change made here would have no effect"
            )
        hashed = _frame_hash(frame)
        proofs = getattr(self, "owned", None)
        if (
            hashed
            and self.memory.get((hashed[1], _plain(name))) is not True
            and _state_held(proofs.body(hashed[1]) if proofs is not None else None, _plain(name))
        ):
            where = display_function((frame or {}).get("name")) or "this frame"
            raise ValueError(
                f"Rust mutation requires a local the program keeps in memory; {name!r} in "
                f"{where} is held in the async body's state across an `.await` (a field of "
                "the future, which the poll reaches through a pointer Peeky never writes), "
                "at every stop of the body. Change what it is computed from before it is "
                "assigned, or copy it into a local of the poll's own after the `.await` with "
                "a candidate edit, in the same probe"
            )
        if hashed and self.memory.get((hashed[1], _plain(name))) is not True:
            place = frame or {}
            why = self._not_in_memory(
                hashed[1], name, place.get("line"), (place.get("source") or {}).get("path")
            )
            if why[0] != "own":
                # Another binding of the name, or an inlined copy, is what the proof lacks.
                where = display_function(place.get("name")) or "this frame"
                rule, advice = self._register_text(where, _plain(name), why)
                raise ValueError(
                    f"Rust mutation requires a local the program keeps in memory; {rule}. "
                    + advice.replace(", with this `--set`", ", with `--set` in the same probe")
                )
        if not hashed or self.memory.get((hashed[1], _plain(name))) is not True:
            where = display_function((frame or {}).get("name")) or "this frame"
            raise ValueError(
                f"Rust mutation requires a local the program keeps in memory; {name!r} in "
                f"{where} is kept in a register and the debugger only shows a copy of it, "
                "so changing it would not affect the program. Locals the code borrows or "
                "reassigns live in memory: a candidate edit can add one "
                "(`std::hint::black_box(&mut x);` after its `let`, with `--set x=…` in the same "
                "probe); otherwise edit the source and run verify"
            )

    def never_settable_row(self, variable, frame, member=False):
        """`register` where no stop of this build can change the row (#138): the build's
        IR keeps its local (for ``member``, the local holding the field or element) only
        in a register in the frame's function, so the debugger shows a copy. A root row
        only when it is an integer, which nothing else would rule out. Else None."""
        if self._optimized():
            return None
        kind = str(variable.get("type") or "")
        if not member and (kind not in RUST_INTEGERS or not _writable_integer(variable)):
            return None
        if member and (kind.endswith("*") or kind.startswith("&")):
            # A reference's fields are refused for that (never written through).
            return None
        hashed = _frame_hash(frame)
        if not hashed or not self.memory:
            return None
        plain = _plain(variable.get("name"))
        if self.memory.get((hashed[1], plain)) is True:
            return None
        proofs = getattr(self, "owned", None)
        if _state_held(proofs.body(hashed[1]) if proofs is not None else None, plain):
            return "async state"
        place = frame or {}
        kind, lines = self._not_in_memory(
            hashed[1],
            variable.get("name"),
            place.get("line"),
            (place.get("source") or {}).get("path"),
        )
        if kind == "inlined":
            return "inlined"
        if kind in ("other", "some"):
            return f"another binding in a register (line {', '.join(map(str, lines))})"
        return "register"

    def _not_in_memory(self, hashed, name, line=None, path=None):
        """Why the IR proves no memory for ``name`` (as LLDB names it: `x @ main.rs:5`
        too) in the function ``hashed``, a proof that covers every binding of its name
        there (rust_ir): ("own", None) where the binding asked for (the one in scope at
        ``line`` of ``path``) is itself kept in a register, or the IR does not say;
        ("other", [lines]) where it has a slot but other bindings of its name, declared
        on those lines, are kept in a register; ("inlined", None) where every binding
        without a slot is in a copy of the function inlined where it is called."""
        plain, _, at = str(name).partition(" @ ")
        found = (getattr(self, "declarations_ir", None) or {}).get((hashed, plain), [])
        unproven = [(number, kind) for number, kind in found if kind != "slot"]
        if not unproven:
            return ("own", None)
        if all(kind == "inlined" for _, kind in unproven):
            return ("inlined", None)
        asked = at.rpartition(":")[2]
        asked = int(asked) if asked.isdigit() else self._binding_line(hashed, plain, line, path)
        copies = sorted({number for number, kind in unproven if kind == "copy" and number})
        own = [kind for number, kind in found if asked is not None and number == asked]
        if own and all(kind == "slot" for kind in own) and copies:
            return ("other", copies)
        if (
            asked is None
            and copies
            and any(kind == "slot" for _, kind in found)
            and len({number for number, _ in found}) > 1
        ):
            # Which binding is asked for is not told (several in scope): name the ones
            # kept in a register.
            return ("some", copies)
        return ("own", None)

    def _binding_line(self, hashed, name, line, path):
        """The line the binding of ``name`` in scope at ``line`` of ``path`` is declared
        on, in the MIR body of ``hashed``; None where that is not one binding."""
        proofs = getattr(self, "owned", None)
        body = proofs.body(hashed) if proofs is not None else None
        if body is None:
            return None
        bindings = body.bindings.get(name, ())
        if len(bindings) == 1:
            return bindings[0][2]
        if line is None:
            return None
        local = proofs._local(body, name, line, path)
        lines = {binding[2] for binding in bindings if local is not None and binding[0] == local}
        return lines.pop() if len(lines) == 1 else None

    @staticmethod
    def _register_text(function, name, why, parameter=False):
        """The rule and its way around for a local the IR proves no memory for (see
        _not_in_memory), in ``function``: (the rule, the advice)."""
        kind, lines = why
        if kind == "inlined":
            return (
                f"in {function}, `{name}` is a local of a function inlined where it is "
                "called (`#[inline(always)]`): Peeky proves no slot for a local of an "
                "inlined copy, so a change would not reach the program",
                "Inlining prevents the change: drop `#[inline(always)]` with a candidate "
                "edit, or change the value where it comes from, in the caller",
            )
        if kind in ("other", "some"):
            listed = ", ".join(map(str, lines))
            some = "line" if len(lines) == 1 else "lines"
            return (
                (
                    f"in {function}, `{name}` has a slot of its own, but another binding of "
                    f"`{name}` (declared on {some} {listed}) is kept in a register"
                    if kind == "other"
                    else f"in {function}, the binding of `{name}` declared on {some} {listed} "
                    "is kept in a register (the debugger only shows a copy of it)"
                )
                + ", and Peeky proves a name in memory only when every binding of it is",
                f"A candidate edit keeps that one in memory too, in the same probe "
                f"(COOKBOOK 6.6): `std::hint::black_box(&mut {name});` after the `let` on "
                f"{some} {listed} (declared `let mut`), or a new name for one of them",
            )
        what = "parameter" if parameter else "local"
        keep = (
            f"`mut {name}` in the signature and `std::hint::black_box(&mut {name});` as "
            "the function's first statement"
            if parameter
            else f"`std::hint::black_box(&mut {name});` after its `let` (declared `let mut`)"
        )
        return (
            f"in {function}, `{name}` is a {what} kept in a register (the debugger only "
            "shows a copy of it, so a change would not affect the program)",
            f"A candidate edit keeps it in memory, in the same probe (COOKBOOK 6.6): {keep}, "
            "by `--old`/`--new`, with this `--set`",
        )

    def _line_functions(self, source, line):
        """[(hash, IrFunction)] of every function that can stop at ``line`` of
        ``source``: one with code of its own there, one inlined there (`copies`), or one
        whose call there LLVM inlined. Empty when the IR was not read."""
        proofs = getattr(self, "owned", None)
        found = []
        for hashed, function in sorted((proofs.functions if proofs else {}).items()):
            if function.file != source:
                continue
            own = any(line in lines for lines in function.blocks.values())
            if own or line in function.copies or line in function.inlined:
                found.append((hashed, function))
        return found

    def never_settable(self, program, lines, names):
        """Why a probe's `--set` of one of ``names`` can change nothing at any stop of
        ``lines`` of ``program`` in this build (#138): the refusal, as (code, message)
        where no such local exists there, else as its message; or None. Only what the
        build fixes counts, and only where it fails in every function that can stop there (a
        closure, a generic's instances, an inlined copy): the IR keeping the local in a
        register (or no such local), its MIR type for a root, and for a field or element
        the debug information's types on the way (#105). What depends on the stop (a
        statement under way, an initialization) is judged there."""
        from peeky.fields import Unsettable
        from peeky.probe import parse_selector

        proofs = getattr(self, "owned", None)
        if not self.memory or proofs is None or not getattr(proofs, "precise", True):
            # The IR was not read (too large): nothing is known to be in a register. A
            # crate's `#[collapse_debuginfo]` macro can put another file's function on
            # the line: which functions stop there is not known.
            return None
        try:
            source = Path(program).resolve().relative_to(self.prepared.source_root).as_posix()
        except (OSError, ValueError):
            return None
        stops = set(self.lines.get(source, ()))
        for name in names:
            try:
                root, *steps = parse_selector(name)
            except ValueError:
                continue
            for line in lines:
                functions = self._line_functions(source, line)
                if line not in stops or not functions:
                    # The debugger moves such a breakpoint; where it stops is not known.
                    continue
                failures = []
                for hashed, function in functions:
                    try:
                        why = self._never_in(hashed, root, steps, source, line)
                    except Unsettable:
                        why = None
                    if why is None:
                        break
                    failures.append((self._function_name(function), why))
                else:
                    return self._never_message(name, root, steps, source, line, failures)
        return None

    @staticmethod
    def _function_name(function):
        return "::".join((*function.ancestors, function.name))

    def _never_in(self, hashed, root, steps, source, line):
        """Why ``root`` (and the path ``steps`` into it) can never change in the function
        ``hashed``: (kind, detail), or None. Raises fields.Unsettable when the debug
        information Peeky needs could not be read (nothing is ruled out then)."""
        from peeky.fields import Unsettable, debugger_steps, settable_member

        plain = _plain(root)
        proofs = getattr(self, "owned", None)
        body = proofs.body(hashed) if proofs is not None else None
        kept = self.memory.get((hashed, plain))
        if kept is None and body is not None and plain not in body.bindings:
            return ("absent", f"has no local `{plain}`")
        # Its type first, as `set` judges it.
        if not steps:
            declared = proofs.declared_type(hashed, root, line, source) if proofs else None
            if declared is not None and declared not in _SETTABLE_RUST:
                return (
                    "type",
                    f"`{root}` is a {declared}; only integer locals (i16 to i64, u16 to u64, "
                    "isize, usize) can be changed",
                )
        else:
            functions, graph = self._field_types()
            types = functions.get(hashed)
            if types is None:
                return ("type", "Peeky cannot match it to the build's debug information")
            try:
                kind, framed = self._declared_type(types, root)
                # As the stop's walk reads the selector (`sp[-1]`, `sp[01]` are `sp[1]`).
                named = debugger_steps(graph, kind, root, steps)
                if named is None:
                    return None
                settable_member(graph, kind, root, named)
                if not framed:
                    raise Unsettable(
                        f"`{root}` is not kept in this frame's own memory (a static, or a "
                        "value the debug information places in registers)"
                    )
            except Unsettable as exc:
                return ("type", str(exc))
        if kept is True:
            return None
        if _state_held(body, plain):
            return ("state", plain)
        if kept is None and body is None:
            return ("unproven", plain)
        parameter = body is not None and any(
            binding[0] in body.arguments for binding in body.bindings.get(plain, ())
        )
        return ("register", (parameter, self._not_in_memory(hashed, root, line, source)))

    def _never_message(self, name, root, steps, source, line, failures):
        """The refusal for a `--set` that ``failures`` [(function, (kind, detail))] rule
        out in every function at ``line``, naming each rule and what to do instead."""
        plain = _plain(root)
        parts, registers, states = [], set(), False  # registers: the advice for each
        for function, (kind, detail) in failures:
            if kind == "register":
                parameter, why = detail
                rule, advice = self._register_text(function, plain, why, parameter)
                parts.append(rule)
                registers.add(advice)
            elif kind == "state":
                states = True
                parts.append(
                    f"in {function}, `{plain}` is held in the async body's state across an "
                    "`.await` (a field of the future, which the poll reaches through a pointer "
                    "Peeky never writes) at every stop of the body"
                )
            elif kind == "absent":
                parts.append(f"{function} {detail}")
            elif kind == "unproven":
                registers.add(self._register_text(function, plain, ("own", None))[1])
                parts.append(
                    f"in {function}, the build's IR gives `{plain}` no memory slot of its own "
                    "(a register, or no such local), so a change would not affect the program"
                )
            else:
                parts.append(f"in {function}, {detail}")
        text = (
            f"`{name}` cannot be changed at {source}:{line}, at any stop of this build: "
            + "; ".join(dict.fromkeys(parts))
            + ". Refused before the program runs: nothing ran"
        )
        if all(kind == "absent" for _, (kind, _) in failures):
            # No function that can stop there has such a local, as `set` would find.
            listed = self._settable_names(source, line)
            return (
                "NOT_FOUND",
                text + self._settable_here(listed),
            )
        for advice in sorted(registers):
            text += f". {advice}"
        if states:
            text += (
                ". Change what it is computed from before it is assigned, or copy it into a "
                "local of the poll's own after the `.await` with a candidate edit, in the "
                f"same probe (`let mut {plain}2 = {plain}; std::hint::black_box(&mut "
                f"{plain}2);` on a line before the stop's, then use and set `{plain}2`)"
            )
        listed = self._settable_names(source, line)
        return text + self._settable_here(listed)

    def _settable_names(self, source, line):
        """The integer locals the build keeps in memory, bound in scopes of ``line``:
        those `set` may change there once assigned (the stop decides the rest), as
        selectors name them: `z @ main.rs:10` where several of the name are in scope,
        as LLDB shows them."""
        proofs = getattr(self, "owned", None)
        found = set()
        for hashed, _ in self._line_functions(source, line):
            body = proofs.body(hashed) if proofs is not None else None
            visible = body.visible_scopes(line, source) if body is not None else None
            if visible is None:
                continue
            for name, bindings in body.bindings.items():
                if self.memory.get((hashed, name)) is not True:
                    continue
                shown = [binding for binding in bindings if binding[4] in visible]
                for binding in shown:
                    if binding[0] is None or body.types.get(binding[0]) not in _SETTABLE_RUST:
                        continue
                    where = f"{Path(binding[1]).name}:{binding[2]}"
                    found.add(name if len(shown) == 1 else f"{name} @ {where}")
        return sorted(found)[:20]

    @staticmethod
    def _settable_here(listed):
        """The end of a refusal before the run: what may be set at its line instead."""
        if not listed:
            return "; settable here: no root locals (fields where rows mark them [settable])"
        return (
            f"; settable here: {', '.join(listed)} (root locals; fields too where rows mark "
            "them [settable])"
        )

    def _stop_block(self, hashed, frame):
        """#115: the lexical block the frame's pc is in (``stop_block``), once per function
        and pc: a frame's every row asks."""
        pc = self._unslid(frame)
        found = self.__dict__.setdefault("_stop_blocks", {})
        if (hashed, pc) not in found:
            if len(found) >= 1024:
                found.clear()
            found[(hashed, pc)] = stop_block(self.code_blocks.get(hashed), pc)
        return found[(hashed, pc)]

    def not_yet_declared(
        self, variable, frame, innermost=True, stack=None, deeper=None, siblings=None, stop=None
    ):
        """#132: why a top-level local of ``frame`` (an unoptimized build's) holds no value
        of its own at the stop, or None (see OwnedLocals.unassigned): one a poll resuming
        an async body after an `.await` never assigned, since the future keeps no copy of
        it (a parameter's copy, a local not used after it), or one some path to the stop
        has not assigned yet. Its slot holds what an earlier call, poll or pass left.
        ``siblings``: the frame's rows, which tell apart rows lldb-dap shows under one
        name; ``stop``: the stop's reason (a line breakpoint's places are fewer)."""
        proofs, hashed = getattr(self, "owned", None), _frame_hash(frame)
        name = variable.get("name")
        if proofs is None or not hashed or self._optimized() or not isinstance(name, str):
            return None
        found = proofs.unassigned(
            hashed[1],
            name,
            (frame or {}).get("line"),
            ((frame or {}).get("source") or {}).get("path"),
            breakpoint=innermost and stop == "breakpoint",
            column=(frame or {}).get("column"),
            caller=not innermost,
            # #115: the lexical block the innermost frame's pc is in, which tells a
            # line's stops in a macro's own blocks apart.
            block=self._stop_block(hashed[1], frame) if innermost else None,
            shown=[row.get("name") for row in siblings or ()],
        )
        if found is None:
            return None
        count, why = found
        rows = [row for row in (siblings or [variable]) if row.get("name") == name]
        index = next((at for at, row in enumerate(rows) if row is variable), None)
        if len(rows) != count or index not in why:
            return None
        if why[index] == "passed":
            return (
                "the value the caller passed, which the future keeps: the body may have "
                "reassigned the parameter since"
                + (f", in its own copy (the other `{name}` row)" if count == 2 else "")
            )
        if why[index] == "unassigned":
            return (
                "not assigned on every path to here (a `let` without a value, or a loop's "
                "next pass before its `let`): its slot may hold what an earlier call or pass "
                "left; read it once it is assigned"
            )
        if count == 2:
            return (
                "this poll's copy of the parameter, never assigned by a poll resuming after an "
                "`.await` (the future keeps none: the body does not use it after), so it may "
                f"hold leftovers; the future's field, the other `{name}` row"
                + (" (withheld too)," if 0 in why else ",")
                + " holds the value passed in"
            )
        return (
            "never assigned by a poll resuming after an `.await` (the future keeps no copy: "
            "the body does not use it after), so it may hold leftovers"
        )

    def mid_statement(self, variable, frame, stop=None):
        """Why a change to the root local ``variable`` of the stopped ``frame`` may not
        reach a statement under way at the stop, or None (#105). Allowed only where the
        stop is before its line's first code (``stop``, its reason: a line breakpoint's,
        or a step's that ends at the line's lowest address) and from there no temporary
        the build's MIR shows may hold a value read from it is used again: a statement
        that starts on an earlier line (`let arr = [s.a,` / `k,` / `s.b];` at its last
        line) has already read it. A step that ends elsewhere (back from a call, as after
        `step --out`, or onto a later part of a statement spanning lines), any other
        stop, and anything the MIR does not explain are refused."""
        hashed, proofs, place = _frame_hash(frame), getattr(self, "owned", None), frame or {}
        line = place.get("line")
        if stop == "step" and not self._line_start(frame):
            return (
                f"the stop is part-way through line {line}, past its first instruction (a "
                "step back from a call, as after `step --out`, or onto a later part of a "
                "statement spanning lines)"
            )
        if stop not in ("breakpoint", "step"):
            return (
                f"the stop ({stop or 'unknown'}) is not a line breakpoint's or a step's, so "
                "Peeky cannot tell where it is in its statement"
            )
        verdict = None
        if hashed and proofs is not None and not self._optimized():
            verdict = proofs.copied_in_progress(
                hashed[1],
                variable.get("name"),
                line,
                (place.get("source") or {}).get("path"),
                breakpoint=True,
                column=place.get("column"),
                block=self._stop_block(hashed[1], frame),
            )
        if verdict is False:
            return None
        if verdict:
            return (
                "a statement under way at the stop has already read it into a temporary "
                "(`let t = s.a` / `+ f();`, at its second line)"
            )
        return (
            "Peeky cannot tell from the build's MIR whether a statement under way at the "
            "stop has read it"
        )

    def _line_start(self, frame):
        """Whether the frame's pc is the lowest address of its line's code in its
        function, where a step onto the line stops (as a line breakpoint does). False
        where it is not, or that is not known."""
        hashed, pc, place = _frame_hash(frame), self._unslid(frame), self._place(frame)
        blocks = self.code_blocks.get(hashed[1]) if hashed else None
        if not blocks or pc is None or place is None:
            return False
        ranges = blocks[0].ranges
        addresses = [
            address
            for address, name, number in self._address_rows()
            if (name, number) == place and any(low <= address < high for low, high in ranges)
        ]
        return bool(addresses) and pc == min(addresses)

    def _address_rows(self):
        """The application units' line-table rows, (address, source, line); empty if
        they cannot be read. Read once, on the first change after a step."""
        if "_rows" not in self.__dict__:
            from peeky.native import _line_rows

            rows = []
            try:
                dwarfdump = _absolute_tool(self.target.dwarfdump, "dwarfdump")
                env = dict(self.prepared.manifest.effective_environment)
                for unit in self.units:
                    table = _metadata(
                        dwarfdump,
                        [f"--debug-line=0x{unit.stmt_list:08x}", str(self.symbols)],
                        self.prepared.workspace,
                        env,
                        max_bytes=RUST_METADATA,
                    )
                    rows += _line_rows(
                        table, self.prepared.workspace, tuple(self.lines), RUST_METADATA
                    )
                self.assert_current()
            except (OSError, RuntimeError, ValueError):
                rows = []
            self._rows = sorted(rows)
        return self._rows

    def _root_type(self, frame, root):
        """(type offset, whether the frame keeps it in its own memory, type graph) of the
        root local of a field or element change (#105), from the application units'
        debug information."""
        from peeky.fields import Unsettable

        hashed = _frame_hash(frame)
        functions, graph = self._field_types()
        types = functions.get(hashed[1]) if hashed else None
        if types is None:
            raise Unsettable("Peeky cannot match this frame to the build's debug information")
        return (*self._declared_type(types, root.get("name")), graph)

    def _root_holds(self, frame, root, stack=None, deeper=None):
        """Raise fields.Unsettable unless the build's IR keeps the root local in memory
        and its MIR proves it initialized at the stop (not moved, dropped or yet to be
        assigned), in an unoptimized build."""
        from peeky.fields import Unsettable

        name = root.get("name")
        if self._optimized():
            raise Unsettable(
                "Rust field and element changes need an unoptimized build (opt-level 0)"
            )
        hashed = _frame_hash(frame)
        if not hashed or self.memory.get((hashed[1], _plain(name))) is not True:
            raise Unsettable(
                f"the build's IR does not keep `{name}` in memory here (the debugger may "
                "show a copy), so a change would not reach the program"
            )
        proofs = getattr(self, "owned", None)
        place = frame or {}
        if (
            proofs is None
            or not proofs.proven(
                hashed[1], name, place.get("line"), (place.get("source") or {}).get("path")
            )
            or self.aggregate_taint(root, frame, innermost=True)
        ):
            raise Unsettable(
                f"the build's MIR does not prove `{name}` initialized here: it may be moved, "
                "dropped or not yet assigned, so a change could be lost or overwritten"
            )

    def _field_types(self):
        """({function hash: fields.local_types}, fields.type_graph) of the application
        units, read once, on the first field or element change."""
        from peeky.fields import Unsettable

        cached = self.__dict__.get("_field_cache")
        if cached is None:
            try:
                cached = self._read_field_types()
            except (OSError, RuntimeError, ValueError) as exc:
                cached = exc
            self._field_cache = cached
        if isinstance(cached, Exception):
            raise Unsettable(
                f"Peeky could not read the build's debug information ({str(cached)[:200]})"
            )
        return cached

    def _read_field_types(self):
        from peeky.fields import RUST_LIBRARIES, local_types, type_graph
        from peeky.native import _chain, _code_ranges, _first, _parse_dies, _text

        dwarfdump = _absolute_tool(self.target.dwarfdump, "dwarfdump")
        env = dict(self.prepared.manifest.effective_environment)
        dies = {}
        for unit in self.units:
            dies.update(
                _parse_dies(
                    _metadata(
                        dwarfdump,
                        [f"--debug-info=0x{unit.die:08x}", "--show-children", str(self.symbols)],
                        self.prepared.workspace,
                        env,
                        max_bytes=RUST_METADATA,
                    )
                )
            )
        self.assert_current()
        functions, kinds = {}, set()
        for offset, die in dies.items():
            if die["tag"] != "DW_TAG_subprogram" or not _code_ranges(die):
                continue
            chain = _chain(dies, offset)
            linkage = _text(_first(dies, chain, "DW_AT_linkage_name")) or ""
            hashed = re.search(r"17(h[0-9a-f]{16})E$", linkage)
            if not hashed:
                continue
            types = local_types(dies, [offset])
            # One hash, two bodies that disagree (never expected): neither is trusted.
            functions[hashed[1]] = types if functions.get(hashed[1], types) == types else None
            kinds.update(kind for each in (types or {}).values() for kind, _, _ in each)
        return functions, type_graph(dies, kinds, RUST_LIBRARIES)

    def view_variable(
        self,
        variable,
        fetch,
        read=None,
        *,
        frame=None,
        top=False,
        innermost=False,
        stop=None,
        siblings=None,
        taint=None,
    ):
        """Qualified views: containers read from memory, active enum variants; None
        leaves ordinary values unchanged.

        A container is read only for a top-level variable of ``frame`` that is either a
        proven reference parameter (rust_ir), once the frame is past the parameter's
        declaration lines, where the entry block has stored it, or an owned `Vec<T>`
        or `String` local the build's MIR proves initialized at the frame's line
        (rust_mir). A `&mut` parameter and an owned local are read only in the
        innermost frame: from an outer frame, a callee (`retain`, `sort_by` calling back
        into your code) may be midway through changing it.

        ``taint`` (see aggregate_taint) says the value is inside a local whose pointers
        may reach freed memory: a pointer there shows only its address.
        """
        kind = variable.get("type")
        if taint and not top and (_holds_data_pointers(kind) or _is_wide_box(kind)):
            # #108: a Box field (or a Vec's, String's or Rc's pointer, a reference) of a
            # moved, dropped or unproven local; its own fields hold no pointer.
            return _unproven(variable)
        parameter = None
        hashed = _frame_hash(frame) if top else None
        proof = self.parameters.get((hashed[1], variable.get("name"))) if hashed else None
        line = (frame or {}).get("line")
        if (
            proof
            and self.target.opt_level == "0"
            and type(line) is int
            and line > proof["line"]
            and (innermost or not proof["type"].startswith("&mut "))
        ):
            parameter = proof["type"]
        proofs = getattr(self, "owned", None)
        # An optimized build's line table cannot place a proof (#13): nothing is proven.
        unoptimized = self.target.opt_level == "0"
        shown_path = ((frame or {}).get("source") or {}).get("path")
        verdict = []

        def block():
            # #115: the DWARF lexical block the innermost frame's pc is in, which tells
            # apart a line's stops in a macro's own blocks (`println!`'s `args`).
            if not innermost or not hashed:
                return None
            return self._stop_block(hashed[1], frame)

        def reference_valid():
            # #110, once per variable: True, False or None (see OwnedLocals).
            if not verdict:
                verdict.append(
                    proofs.reference_valid(
                        hashed[1],
                        variable.get("name"),
                        line,
                        shown_path,
                        # A caller frame is on its call's line, anywhere on it (a step's
                        # rule): more places can only make the verdict stricter.
                        breakpoint=innermost and stop == "breakpoint",
                        column=(frame or {}).get("column"),
                        block=block(),
                    )
                )
            return verdict[0]

        if (
            parameter
            and innermost
            and unoptimized
            and hashed
            and proofs is not None
            and reference_valid() is False
        ):
            # A reference parameter whose target the frame dropped (`*v = new` between
            # its drop and its store) is not read as a container either.
            parameter = None
        owned = bool(
            hashed
            and innermost
            and unoptimized
            and proofs is not None
            and (
                variable.get("type") in OWNED_CONTAINERS
                # A Vec of tuples or structs (`Vec<(char, usize)>`); the MIR's declared
                # type selects its layout.
                or composite_container(variable.get("type"))
            )
            and proofs.proven(
                hashed[1],
                variable.get("name"),
                line,
                ((frame or {}).get("source") or {}).get("path"),
            )
        )
        layouts = self.layouts
        container = container_view(
            variable,
            read,
            layouts,
            optimized=self.target.opt_level != "0",
            parameter=parameter,
            owned=owned,
            declared=proofs.declared_type(hashed[1], variable.get("name")) if owned else None,
        )
        if container is not None:
            return container
        if hashed and unoptimized and proofs is not None:
            # #106: a tuple or struct of scalars, previewed under the same proofs.
            declared = (
                proofs.declared_type(hashed[1], variable.get("name"), line, shown_path)
                if innermost
                else None
            )
            preview = struct_view(
                variable,
                read,
                layouts,
                parameter=parameter,
                owned=bool(
                    struct_name(declared, kind, layouts)
                    and proofs.proven(hashed[1], variable.get("name"), line, shown_path)
                ),
                declared=declared,
            )
            if preview is not None:
                return preview
        if top and proofs is not None:
            types = self._binding_types(variable, frame)
            # An array is judged by its elements (`[&Pt; 2]` is plain, `[Box<Pt>; 2]` owns).
            plain = bool(types) and all(_NON_OWNING.match((t or "").lstrip("[")) for t in types)
            # LLDB names a wide Box (`Box<str>`, `Box<[T]>`, `Box<dyn Trait>`) itself, even
            # where the MIR says only `T`; a reference to one (`&Box<str>`) is not owning.
            wide = _is_wide_box(kind)
            # A Box among a name's bindings counts only for a value that could be one (a
            # data pointer or an array of them, or a wide Box): another binding of the name
            # in a sibling block, arm or loop (`v: i64`, `a: [i64; 3]`, a fn pointer, a
            # tuple holding a raw pointer) is not.
            could_be_box = kind is None or wide or _holds_data_pointers(kind)
            owning = (
                could_be_box
                and any(
                    re.match(r"\[*(?:std::boxed::|alloc::boxed::)?Box<", t or "") for t in types
                )
            ) or (wide and not plain)
            # What LLDB shows as a data pointer, or an array of them (`Pt *`, `unsigned char
            # (*)[8]`, `long (**)()`, `Pt *[2]`), is read through only when the MIR says
            # every binding is a reference, a raw pointer or a fn pointer (or an array of
            # them): a generic `T`, `Self` or `impl Trait` may be a Box.
            pointer = _holds_data_pointers(kind)
            provable = innermost and unoptimized and hashed
            gated = (owning or (pointer and not plain)) and not (
                provable and proofs.proven(hashed[1], variable.get("name"), line, shown_path)
            )
            # Judged in every frame (`--frame N` reads callers): only proofs that make a
            # value readable (an owned container, a Box) are the innermost frame's.
            if not gated and unoptimized and hashed:
                # #110: a reference (or raw pointer, `&str`, a tuple holding one) outlives
                # what it points into in scope, though not in use: it reads while the code
                # still uses it ahead. A line breakpoint stops only where the line starts in
                # each lexical block, at the frame's column (`stop`); a step or a pause can
                # stop anywhere in it. What cannot be told (a site or local not understood)
                # reads as before.
                gated = reference_valid() is False
                if gated and siblings:
                    gated = not _same_lent_rows(
                        proofs.lent_copies(
                            hashed[1],
                            variable.get("name"),
                            line,
                            shown_path,
                            breakpoint=stop == "breakpoint",
                            column=(frame or {}).get("column"),
                            block=block(),
                        ),
                        variable,
                        siblings,
                        read,
                    )
            if gated:
                # #101: a moved or dropped Box still holds its pointer, and LLDB would
                # read the freed allocation as its fields. The pointer is shown; what it
                # points at is not.
                return _unproven(variable)
        view = active_variant(variable, fetch, layouts)
        if view and view["rust_view"] == "active-variant" and self.target.opt_level != "0":
            # Optimized code may track an enum's tag and payload separately and LLDB
            # zero-fills a missing piece without reporting it, so a decoded variant is
            # not trustworthy there.
            return {**view, "value": UNVERIFIED, "rust_view": "opaque"}
        return view

    def view_writable(self, view):
        """Whether a field of the value ``view`` shows is the value's own storage, laid
        out as the build's DWARF says (#105): a #106 tuple or struct preview, read with a
        `composite` struct layout. A container's contents (a Vec's, a String's, a Box's)
        or an enum's variant are not: they stay unchangeable. The change itself is still
        judged by validate_member, from the debugger's own variable."""
        layout = self.layouts.get(str(view.get("struct_layout")))
        return (
            view.get("rust_view") == "memory"
            and str(view.get("struct_layout")).startswith(STRUCT_PREFIX)
            and isinstance(layout, dict)
            and layout.get("kind") == "composite"
        )

    def _binding_types(self, variable, frame):
        """The MIR types of the bindings the frame's top-level ``variable`` can be (see
        OwnedLocals.binding_types): the one declared, if the stop tells which."""
        proofs, hashed = self.owned, _frame_hash(frame)
        line = (frame or {}).get("line")
        # An unmangled function (`#[no_mangle]`) has no hash: its bindings come from
        # the bodies on the stop line, and nothing there is ever proven.
        function = hashed[1] if hashed else None
        source = ((frame or {}).get("source") or {}).get("path")
        declared = (
            proofs.declared_type(function, variable.get("name"), line, source) if hashed else None
        )
        if declared is not None:
            return [declared]
        return proofs.binding_types(
            function,
            variable.get("name"),
            source,
            line,
            # The frame's own shape: a `fn` nested in a closure or async body
            # (`af::{{closure}}::tw`) is not a closure itself.
            closure=frame_shape((frame or {}).get("name"))[1] > 0,
            frame=(frame or {}).get("name"),
        )

    def aggregate_taint(self, variable, frame, *, innermost=False):
        """#108: how much of a top-level local LLDB shows as a struct, tuple, array or
        enum (a value with children, not a pointer) reads, for one whose MIR type may own
        memory through a pointer inside it (a Box field, a Vec's or String's buffer, an
        Rc; any struct, as the MIR names no fields). None: all of it, where the build's
        MIR proves it initialized at the stop in the innermost frame of an unoptimized
        build. "pointers": its own fields, not what its pointers reach, in a caller frame
        proven the same way (a callee may be midway through replacing a Box it lent).
        "stale": its fields marked as the bytes it left behind, and not what its
        pointers reach, where it may be moved, dropped, partly moved or not yet
        assigned, or nothing is proven (an optimized build, a frame the MIR cannot
        match). The worker passes the result to ``view_variable`` for everything inside
        it."""
        proofs, kind = getattr(self, "owned", None), variable.get("type")
        if proofs is None or _holds_data_pointers(kind) or _is_wide_box(kind):
            # A pointer at the top is the Box and reference gates' (view_variable).
            return None
        if not re.fullmatch(r"[^\W\d]\w*(?: @ .+)?", str(variable.get("name"))):
            # LLDB's own values, not a local the code moves: an async body's `<null>`
            # (its `Pin<&mut>` self), the `(Return Value)` a step out shows.
            return None
        types = self._binding_types(variable, frame)
        hashed = _frame_hash(frame)
        # An unmangled function (`#[no_mangle]`) is never generic.
        generics = proofs.type_parameters(hashed[1]) if hashed else ()
        if types and not any(_may_own(each, generics) for each in types):
            # Primitives, references and raw pointers only (`(i64, &Pt)`): a move leaves
            # their bytes as they were, and what a reference reaches is #110's.
            return None
        proven = bool(
            self.target.opt_level == "0"
            and hashed
            and proofs.proven(
                hashed[1],
                variable.get("name"),
                (frame or {}).get("line"),
                ((frame or {}).get("source") or {}).get("path"),
            )
        )
        if proven:
            return None if innermost else "pointers"
        return "stale"

    def display_type(self, variable, frame, top=True):
        """A value's Rust type where LLDB shows a C-style name: a root local's primitive
        from the build's MIR (`usize`, not `unsigned long`), any other's when only one
        primitive renders as that name (`double` is `f64`); and a reference parameter's
        from the build's IR (`&mut limiter::Window`, not `limiter::Window *`), which
        declares each instantiation's own types, never a type parameter."""
        kind = variable.get("type")
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        proof = self.parameters.get((hashed[1], variable.get("name"))) if top and hashed else None
        if proof and (match := re.fullmatch(r"&(?:mut )?(.+)", proof["type"])):
            # The parameter's only declaration (ir_locals): LLDB's name must spell it.
            if _spelled(f"{match[1]} *", kind):
                return proof["type"]
        if top and hashed and proofs is not None:
            declared = proofs.declared_type(hashed[1], variable.get("name"))
            if kind in RUST_SCALARS.get(declared, ()):
                return declared
        return UNIQUE_SCALARS.get(kind)

    def display_value(self, variable):
        """A scalar as Rust's `{:?}` shows it: `f64`/`f32` (`10.0`, not LLDB's `10`),
        `u8`/`i8` as integers (`65`, not LLDB's `'A'`) and `char` as `'b'` (not LLDB's
        `U+0062 U+0062 U'b'`). A pointer to one (`&u8`, `&i8`, `&char`, a raw pointer)
        shows only its address: LLDB reads it as a C string (`0x… "\\xc8\\xfd…"`,
        `U"zq…"`), whose text past the one value it points at is unrelated memory."""
        value, kind = variable.get("value"), variable.get("type")
        if _CHAR_POINTER.fullmatch(str(kind)) and (address := _ADDRESS.match(str(value))):
            return address[1]
        return lldb_float_debug(value, kind) or lldb_byte(value, kind) or lldb_char(value, kind)

    def validate_children(self, variables):
        # `$` cannot start a Rust identifier; such fields are debugger-synthesized storage.
        if any(
            not isinstance(variable.get("name"), str) or variable["name"].startswith("$")
            for variable in variables
        ):
            raise ValueError("Raw Rust enum storage is not exposed; its layout is not qualified")

    def run_tests(self, deadline=None):
        """Execute the prepared, hashed integration-test binary; Cargo is not invoked.
        None when the crate has no integration test: the checker alone decides."""
        from peeky.agent import execute

        if self.test_binary is None:
            return None
        self.assert_current()
        binary = _digest(self.binary)[0]
        if _digest(self.test_target)[0] != binary:
            raise RuntimeError("Cargo test target differs from the debugged executable")
        command = [str(self.test_binary), "--test-threads=1", "--color", "never"]
        timeout, limited = TEST_TIMEOUT_S, False
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("verify ran past its --timeout before the Cargo tests")
            timeout, limited = min(timeout, remaining), remaining < timeout
        # Any output beyond the retained capture is an explicit failure, never truncation.
        result = execute(
            command,
            self.prepared.workspace,
            timeout,
            env=self.environment,
            output_limit=16000,
        )
        self.assert_current()
        if _digest(self.test_target)[0] != binary:
            raise RuntimeError("Cargo test target changed during test execution")
        if limited and result.get("timed_out"):
            raise TimeoutError("verify ran past its --timeout while the Cargo tests ran")
        return {
            **result,
            **libtest_summary(result),
            "stage": "cargo-test",
            "command": command,
            "executable_sha256": _digest(self.test_binary)[0],
        }

    def close(self):
        try:
            super().close()
        finally:
            _remove_owned(self.parent)


class RustVerification(CompiledVerification):
    """Independent checker plus the prepared Cargo integration test; both must pass."""

    language = "rust"
