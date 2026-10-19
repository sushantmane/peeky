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
from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs
from peeky.rust_mir import OwnedLocals, ir_functions, load_bodies
from peeky.rust_symbols import (
    OWNED_CONTAINERS,
    RUST_SCALARS,
    UNIQUE_SCALARS,
    UNVERIFIED,
    active_variant,
    application_symbols,
    composite_container,
    container_view,
    lldb_byte,
    lldb_char,
    lldb_float_debug,
)

DEPS = "target/debug/deps"
MIR_DIR = "target/peeky-mir"
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
            **(
                {"mir_wrapper_sha256": hashlib.sha256(MIR_WRAPPER.encode()).hexdigest()}
                if self.opt_level == "0"
                else {}
            ),
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
            if self.opt_level == "0":
                # Outside the workspace, in Peeky's own directory; checked before every
                # command (validate_workspace).
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
            memory, parameters = ir_locals(binary.with_name(binary.name + ".ll"))
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
            lines, layouts, units = application_symbols(
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
    collapsed = any(
        "collapse_debuginfo" in (Path(workspace) / name).read_text(errors="replace")
        for name in sources
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


def _frame_hash(frame):
    return re.search(r"::(h[0-9a-f]{16})$", str((frame or {}).get("name") or ""))


def _is_pointer(kind):
    """Whether LLDB's C-style type is a pointer at its top level: `Pt *`, `unsigned char
    (*)[8]`, `long (**)()`, `unsigned char (*(*)[2])[2]`; not a generic's argument
    (`Vec<Pt *>`) or an array of pointers (`long (*[2])(long)`, `unsigned char *[2]`)."""
    text = str(kind or "")
    while (stripped := re.sub(r"<[^<>]*>", "", text)) != text:
        text = stripped
    return bool(re.search(r"\*\s*$|\(\*+\)", text))


# MIR types that do not own what they point at.
_NON_OWNING = re.compile(r"&|\*const |\*mut |(?:unsafe )?(?:extern \S+ )?fn\(")


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
                )
            )
        if not infos:
            raise ValueError("No application units were recorded for this build")
        return function_scopes("\n".join(infos), "\n".join(tables), workspace, tuple(self.lines))

    def launch_args(self, program, root, args=None):
        return {
            **super().launch_args(program, root, args),
            # Never execute Python scripts embedded beside symbol files.
            "initCommands": ["settings set target.load-script-from-symbol-file false"],
        }

    def settable(self, variable, frame=None):
        """A Rust integer local the program keeps in memory (see validate_mutation_target)."""
        if variable.get("type") not in RUST_INTEGERS or not _writable_integer(variable):
            return False
        hashed = _frame_hash(frame)
        return bool(hashed) and self.memory.get((hashed[1], variable.get("name"))) is True

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
        hashed = _frame_hash(frame)
        if not hashed or self.memory.get((hashed[1], name)) is not True:
            where = display_function((frame or {}).get("name")) or "this frame"
            raise ValueError(
                f"Rust mutation requires a local the program keeps in memory; {name!r} in "
                f"{where} is kept in a register and the debugger only shows a copy of it, "
                "so changing it would not affect the program. Locals the code borrows or "
                "reassigns "
                "live in memory; otherwise edit the source and run verify"
            )

    def view_variable(self, variable, fetch, read=None, *, frame=None, top=False, innermost=False):
        """Qualified views: containers read from memory, active enum variants; None
        leaves ordinary values unchanged.

        A container is read only for a top-level variable of ``frame`` that is either a
        proven reference parameter (rust_ir), once the frame is past the parameter's
        declaration lines, where the entry block has stored it, or an owned `Vec<T>`
        or `String` local the build's MIR proves initialized at the frame's line
        (rust_mir). A `&mut` parameter and an owned local are read only in the
        innermost frame: from an outer frame, a callee (`retain`, `sort_by` calling back
        into your code) may be midway through changing it.
        """
        parameter = None
        hashed = _frame_hash(frame) if top else None
        proof = self.parameters.get((hashed[1], variable.get("name"))) if hashed else None
        line = (frame or {}).get("line")
        if (
            proof
            and type(line) is int
            and line > proof["line"]
            and (innermost or not proof["type"].startswith("&mut "))
        ):
            parameter = proof["type"]
        proofs = getattr(self, "owned", None)
        owned = bool(
            hashed
            and innermost
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
        container = container_view(
            variable,
            read,
            self.layouts,
            optimized=self.target.opt_level != "0",
            parameter=parameter,
            owned=owned,
            declared=proofs.declared_type(hashed[1], variable.get("name")) if owned else None,
        )
        if container is not None:
            return container
        if top and proofs is not None and self.target.opt_level == "0":
            # An unmangled function (`#[no_mangle]`) has no hash: its bindings come from
            # the bodies on the stop line, and nothing there is ever proven.
            function = hashed[1] if hashed else None
            declared = proofs.declared_type(function, variable.get("name")) if hashed else None
            source = ((frame or {}).get("source") or {}).get("path")
            types = (
                [declared]
                if declared is not None
                else proofs.binding_types(
                    function,
                    variable.get("name"),
                    source,
                    line,
                    # lldb-dap shows `{{closure}}` escaped: `_$u7b$$u7b$closure$u7d$$u7d$`.
                    closure=bool(
                        re.search(
                            r"\{\{closure\}\}|\$u7b\$\$u7b\$closure",
                            str((frame or {}).get("name") or ""),
                        )
                    ),
                )
            )
            plain = bool(types) and all(_NON_OWNING.match(t or "") for t in types)
            # LLDB names a wide Box (`Box<str>`, `Box<[T]>`, `Box<dyn Trait>`) itself, even
            # where the MIR says only `T`; a reference to one (`&Box<str>`) is not owning.
            owning = any(
                re.match(r"(?:std::boxed::|alloc::boxed::)?Box<", t or "") for t in types
            ) or (str(variable.get("type") or "").startswith("alloc::boxed::Box<") and not plain)
            # What LLDB shows as a pointer (`Pt *`, `unsigned char (*)[8]`, `long (**)()`)
            # is read through only when the MIR says every binding is a reference, a raw
            # pointer or a fn pointer: a generic `T`, `Self` or `impl Trait` may be a Box.
            pointer = _is_pointer(variable.get("type"))
            if (owning or (pointer and not plain)) and not (
                innermost
                and hashed
                and proofs.proven(
                    hashed[1],
                    variable.get("name"),
                    line,
                    ((frame or {}).get("source") or {}).get("path"),
                )
            ):
                # #101: a moved or dropped Box still holds its pointer, and LLDB would
                # read the freed allocation as its fields. The pointer is shown; what it
                # points at is not.
                # Only the address: LLDB's summary of a `char *` (`Box<u8>`) is read
                # through the pointer too.
                address = re.match(r"0x[0-9a-fA-F]+", str(variable.get("value") or ""))
                return {
                    "name": variable.get("name"),
                    "type": variable.get("type"),
                    "value": address[0] if address else "<not shown: may point at freed memory>",
                    "variablesReference": 0,
                    "rust_view": "unproven",
                }
        view = active_variant(variable, fetch, self.layouts)
        if view and view["rust_view"] == "active-variant" and self.target.opt_level != "0":
            # Optimized code may track an enum's tag and payload separately and LLDB
            # zero-fills a missing piece without reporting it, so a decoded variant is
            # not trustworthy there.
            return {**view, "value": UNVERIFIED, "rust_view": "opaque"}
        return view

    def display_type(self, variable, frame, top=True):
        """A value's Rust primitive type where LLDB shows a C-style name: a root local's
        from the build's MIR (`usize`, not `unsigned long`), any other's when only one
        primitive renders as that name (`double` is `f64`)."""
        kind = variable.get("type")
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        if top and hashed and proofs is not None:
            declared = proofs.declared_type(hashed[1], variable.get("name"))
            if kind in RUST_SCALARS.get(declared, ()):
                return declared
        return UNIQUE_SCALARS.get(kind)

    def display_value(self, variable):
        """A scalar as Rust's `{:?}` shows it: `f64`/`f32` (`10.0`, not LLDB's `10`),
        `u8`/`i8` as integers (`65`, not LLDB's `'A'`) and `char` as `'b'` (not LLDB's
        `U+0062 U+0062 U'b'`)."""
        value, kind = variable.get("value"), variable.get("type")
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
