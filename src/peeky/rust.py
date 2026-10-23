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
from peeky.assembler import rust_refusal
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
from peeky.identifiers import is_located, is_name
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
from peeky.rust_mir import (
    _BORROWING,
    _FN_POINTER,
    _MENTIONED,
    OwnedLocals,
    frame_shape,
    ir_functions,
    load_bodies,
    stop_block,
)
from peeky.rust_symbols import (
    CALLER_ADVICE,
    CALLER_CHANGING,
    OPTIMIZED_ADVICE,
    OWNED_CONTAINERS,
    REFERENCE_PARAMETERS,
    RUST_METADATA,
    RUST_SCALARS,
    STRUCT_PREFIX,
    UNIQUE_SCALARS,
    UNVERIFIED,
    _composite_parameter,
    _spelled,
    active_variant,
    application_symbols,
    composite_container,
    container_view,
    enum_withheld,
    lldb_byte,
    lldb_char,
    lldb_float_debug,
    option_layouts,
    option_name,
    option_view,
    struct_name,
    struct_view,
    uninitialized_advice,
    unread_elements,
    without_numbered_structs,
)
from peeky.text_sets import text_kind
from peeky.textlines import read_source, source_lines, split_lines

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
        _CARGO_STDERR.fullmatch(line) for line in split_lines(stderr) if line.strip()
    ) or not any(line.startswith("error: could not compile") for line in split_lines(stderr)):
        return False
    lines = split_lines(stdout)
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
    lines = [line.strip() for line in split_lines(result.get("stdout", "")) if line.strip()]
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
        for line in split_lines(listing.split("\n\n", 1)[0])
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
        judged = []

        def validate_workspace(workspace, env):
            # Before every probe and Cargo command: revalidate the exact staged bytes
            # Cargo will read (not the earlier preflight), ancestor configuration and
            # the helper rustc will select from PATH.
            staged = validate_cargo_inputs(workspace, sources=self.sources, tests=self.tests)
            if staged != cargo_inputs:
                raise RuntimeError("Staged Cargo inputs differ from validated preflight")
            if not judged:
                # Inline assembly that can read a file, which no dependency file lists,
                # is refused before anything runs (#180); the staged bytes are rechecked
                # before each command.
                texts = {
                    name: (workspace / name).read_bytes().decode("utf-8", errors="replace")
                    for name in cargo_inputs.inputs
                }
                if (refused := rust_refusal(texts, (*self.sources, *self.tests))) is not None:
                    raise refused
                judged.append(True)
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
            # Unsafe code can free what safe code shows owned (#156 review): where the
            # crate has any, no owned container, field container or frozen borrow reads.
            # Every declared file counts, an integration test's too (#156 review: what
            # Peeky reads from rustc's dependency file is not exact for every file name).
            session.unsafe_code = unsafe_source(texts, rust)
            if owned is not None:
                owned.unsafe_code = session.unsafe_code
                # Where std's printing is written, to trust a read it makes (#192 review).
                owned.std_macros = std_macro_files(
                    rustc, prepared.manifest.probes, (prepared.workspace, root)
                )
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


def std_macro_files(rustc, probes, outside):
    """The files std's and alloc's `print!`, `println!`, `eprint!`, `eprintln!` and
    `format!` are written in, exactly as this toolchain's MIR names a span in them (#192
    review 4): its sysroot's library sources (`rust-src`, where rustc maps its own
    paths back), or rustc's `/rustc/<commit-hash>/library/…`, the hash from the build's
    own `rustc -vV`. Never a file under ``outside`` (the crate, its build): a crate's
    own `src/library/std/src/macros.rs` is no std file."""
    sysroot = Path(rustc).parent.parent
    commit = None
    for probe in probes:
        if tuple(probe.argv) == (rustc, "-vV"):
            found = re.search(r"^commit-hash: ([0-9a-f]{40})$", probe.stdout, re.M)
            commit = found[1] if found else None
    files = set()
    for library in ("std", "alloc"):
        files.add(str(sysroot / "lib/rustlib/src/rust/library" / library / "src/macros.rs"))
        if commit:
            files.add(f"/rustc/{commit}/library/{library}/src/macros.rs")
    roots = [Path(each).resolve() for each in outside]
    return frozenset(
        file
        for file in files
        if not any(Path(file).resolve().is_relative_to(root) for root in roots)
    )


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
    # Every function the dump holds, to tell the crate's own `std::ptr::eq` (#259).
    defined = set()
    return OwnedLocals(
        load_bodies(Path(workspace) / MIR_DIR, crate, defined),
        ir_functions(ir, sources),
        sources,
        precise=not collapsed,
        # Bytes, so lines split on `\n` alone as rustc counts them (no universal newlines).
        texts={
            name: (Path(workspace) / name).read_bytes().decode("utf-8", errors="replace")
            for name in sources
        },
        defined=defined,
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
        for dependency in dependency_paths(depfile.read_text(), root, "rustc"):
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

# What a Rust function may return that Peeky shows (#155), as LLDB names it: integers
# (u8 and i8 as C characters, converted), bool and floats.
RUST_RETURNS = RUST_INTEGERS | {"bool", "float", "double", "unsigned char", "char", "signed char"}

# The MIR's names for the integers `set` changes (as RUST_INTEGERS are LLDB's).
_SETTABLE_RUST = frozenset({"i16", "i32", "i64", "u16", "u32", "u64", "isize", "usize"})


# LLDB's names for a `Vec`, `String`, `&str`, `Box<str>` or slice: a field of one of
# these types may be read from memory (#156).
_FIELD_CONTAINER = re.compile(
    r"alloc::vec::Vec<.+>|alloc::string::String|&(?:mut )?str|&(?:mut )?\[.+\]"
    r"|alloc::boxed::Box<str, alloc::alloc::Global>"
)
_UNSAFE_CRATE = (
    "the crate has unsafe code (in {source}), which can free a buffer without Peeky seeing it"
)
_UNREAD_CRATE = (
    "Peeky cannot rule out unsafe code in {source} ({why}), and unsafe code can free a "
    "buffer without Peeky seeing it"
)
# Where a crate's own code can do what safe Rust cannot: an `unsafe` block, function,
# impl, trait or extern block, an `#[unsafe(...)]` attribute, and the attributes that
# are unsafe without saying so before the 2024 edition (a `#[no_mangle]` function can
# replace `free`), and `global_asm!`, which needs no `unsafe` but can define `free`
# too (`asm!` and `naked_asm!` need it, but count anyway). Only `r#unsafe` is an
# identifier: a macro may drop the `#` of `#unsafe` (#156 review), and `r#asm` is `asm`.
_UNSAFE_CODE = re.compile(
    r"(?<!\w)(?:unsafe|no_mangle|export_name|link_section|global_asm|naked_asm)\b"
    r"|(?<!\w)asm\s*!"
)
_RAW_UNSAFE = re.compile(r"(?<!\w)r#unsafe\b")
_NIGHTLY_CRATE = (
    "the crate enables nightly features (in {source}), which can free a buffer without "
    "Peeky seeing it"
)
# An inner attribute (`#![...]`), and a nightly feature it enables, alone or under a
# `cfg_attr`: Peeky's MIR builds set RUSTC_BOOTSTRAP, so `#![feature(custom_mir)]`
# works, whose `Drop(*v, ...)` frees what a `&Vec` points at with no `unsafe`.
_SPACE = "[\\s\u200e\u200f]*"
_INNER_ATTRIBUTE = re.compile(f"#{_SPACE}!{_SPACE}\\[")
_FEATURE = re.compile(f"(?<!\\w)(?:r#)?feature{_SPACE}\\(")
_SQUARE = re.compile(r"[\[\]]")


def _unsafe_code(code):
    """Whether ``code`` (Rust without comments and literals, or another file's text) has
    unsafe code (_UNSAFE_CODE)."""
    return any(
        found.start() < 2 or not _RAW_UNSAFE.match(code, found.start() - 2)
        for found in _UNSAFE_CODE.finditer(code)
    )


def _nightly(code):
    """Whether ``code`` has an inner attribute naming `feature(...)` (one not closed runs
    to the end)."""
    for attribute in _INNER_ATTRIBUTE.finditer(code):
        depth = 0
        for bracket in _SQUARE.finditer(code, attribute.end() - 1):
            depth += 1 if bracket[0] == "[" else -1
            if not depth:
                if _FEATURE.search(code, attribute.end(), bracket.start()):
                    return True
                break
        else:
            return bool(_FEATURE.search(code, attribute.end()))
    return False


def unsafe_source(texts, rust):
    """Why the crate may have unsafe code, or None: ``texts`` ({name: text}) are the
    crate's files, ``rust`` the Rust sources among them. Rust sources are read without
    their comments and strings, and one Peeky cannot be sure it reads as rustc does
    (rust_lexer.scan) counts; any other file a macro could `include!`, as plain text. A
    nightly feature counts too: one can free memory without `unsafe`."""
    from peeky.rust_lexer import scan

    for name in rust:
        code, why = scan(texts.get(name, ""))
        if code is None:
            return _UNREAD_CRATE.format(source=name, why=why)
        if _unsafe_code(code):
            return _UNSAFE_CRATE.format(source=name)
        if _nightly(code):
            return _NIGHTLY_CRATE.format(source=name)
    for name, text in texts.items():
        if name not in rust and _unsafe_code(text):
            return _UNSAFE_CRATE.format(source=name)
        if name not in rust and _nightly(text):
            return _NIGHTLY_CRATE.format(source=name)
    return None


_NO_MEMORY = (
    "the debugger gives no memory address for it (lldb-dap from LLVM 20 reads memory, "
    "with readMemory)"
)


# A String as the MIR names it: what a text (#137) changes.
_TEXT_RUST = frozenset({"std::string::String", "alloc::string::String", "String"})
# A `&mut` reference to a Vec, String, slice or str, as the IR or the MIR names its type.
_MUT_CONTAINER = re.compile(
    r"&mut (?:(?:std|alloc)::vec::Vec<|(?:std|alloc)::string::String$|\[|str$)"
)


# The prelude's own types (#183), named as the program names them, and the default
# allocator's argument, which the program never writes.
_PRELUDE_PATH = re.compile(
    r"(?<![\w:])(?:alloc::vec::|alloc::string::|alloc::boxed::|core::option::|core::result::)"
    r"(?=(?:Vec|String|Box|Option|Result)\b)"
)
_GLOBAL_ARGUMENT = re.compile(r",\s*alloc::alloc::Global(?=\s*[,>])")


# A primitive LLDB names the C way inside a type's arguments (`Vec<unsigned int>`):
# which Rust one it is only the debug information can tell (`unsigned long`: u64 or
# usize).
_C_NAMED = re.compile(
    r"[<,(]\s*(?:unsigned |signed )?(?:char|short|int|long|long long|__int128|char32_t)"
    r"\s*[,>)]"
)


def rust_spelling(name):
    """A Rust type as the program spells it (#183): `Vec<u32>`, `String`, `Option<i32>`,
    `Box<T>`, `Result<T, E>`, without the default allocator (`alloc::alloc::Global`).
    ``name``: as the debug information gives it (Rust's primitives, whole paths)."""
    return _PRELUDE_PATH.sub("", _GLOBAL_ARGUMENT.sub("", str(name)))


class _Copied(str):
    """``mid_statement``'s reason, with its own ``advice`` where it has one (#192: a
    binding of the program's own holds the copy, and no other place on the statement
    helps), and the short form a row gives (``row``, #300: `[not settable: …]`)."""

    def __new__(cls, reason, advice=None, row=None):
        made = super().__new__(cls, reason)
        made.advice = advice
        made.row = row
        return made


# What a variable holding a pointer is called in a refusal (OwnedLocals.untraced_reads),
# alone and as several; None: another value, or variables of different kinds.
_POINTER_KINDS = {
    "reference": ("a reference", "references"),
    "raw pointer": ("a raw pointer", "raw pointers"),
    "closure": ("a closure", "closures"),
    "coroutine": ("a coroutine", "coroutines"),
    None: ("a value holding a pointer", "values holding pointers"),
}


def _untraced(proofs, hashed, held, home, target):
    """The clause of a refusal naming what a statement under way used that may point at
    ``target`` ("it", or the local's name), where Peeky does not follow it (#300;
    ``held``: the temporaries that may hold a copy): ("uses `e` and `l` (at line 39),
    references Peeky can't trace that may point at it", their places)."""
    places, variables = proofs.untraced_reads(hashed, held)
    made = _line_list(places, home)
    where = f" (at {made})" if made else ""
    if not variables:
        return f"reads through a pointer{where} Peeky can't trace that may point at {target}", (
            places
        )
    kinds = {kind for _, kind in variables}
    one, many = _POINTER_KINDS[kinds.pop() if len(kinds) == 1 else None]
    names = _names([name for name, _ in variables])
    what = one if len(variables) == 1 else many
    return f"uses {names}{where}, {what} Peeky can't trace that may point at {target}", places


def _either(clauses):
    """`a`, `a or b`, `a, b or c`: clauses for a message."""
    return clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + f" or {clauses[-1]}"


def _copy_reason(proofs, hashed, found, explicit, traced, home, target="it"):
    """How sure a refusal may be of what a statement under way holds (#300), from
    ``copy_hazards`` at the same places reading pointers each way (``found``: "any";
    ``explicit``, ``traced``; each [(place, hazards)], or None where not understood):

    - where at every place something the MIR shows reading it is used again (a copy, a
      read through a pointer made from it, or what holds either): ("named", the
      program's own bindings, their places) where any of them holds a copy the program
      goes on with (#192: one taken from it is used at the other places), else ("read",
      None, the places of those temporaries);
    - else ("may", what the statement does that may leave a copy of ``target``, places):
      it reads it at some place but maybe not the stop's, gives a call a pointer into it,
      or uses one Peeky does not follow."""
    bindings = proofs.body(hashed).named_locals()
    every, sure, calls = dict(found), dict(explicit or ()), dict(traced or ())
    named = [hazards & bindings for hazards in every.values()]
    reads = [sure.get(place, frozenset()) - bindings for place in every]
    if all(sure.get(place) for place in every):
        if held := frozenset().union(*named):
            return "named", proofs.copy_names(hashed, held), proofs.copy_lines(hashed, held, True)
        return "read", None, proofs.copy_lines(hashed, frozenset().union(*reads))
    shown = frozenset().union(*sure.values()) | bindings
    given = frozenset().union(*calls.values()) - shown
    rest = frozenset().union(*every.values()) - shown - given
    clauses, places = [], set()
    if read := frozenset().union(*reads):
        places |= (lines := proofs.copy_lines(hashed, read))
        clauses.append(f"reads {target} into a temporary (at {_line_list(lines, home)})")
    if copied := frozenset().union(*named):
        places |= (lines := proofs.copy_lines(hashed, copied, True))
        names = _names(proofs.copy_names(hashed, copied))
        clauses.append(f"copies {target} into {names} (at {_line_list(lines, home)})")
    if given:
        places |= (lines := proofs.copy_lines(hashed, given))
        clauses.append(f"gives a call a pointer into {target} (at {_line_list(lines, home)})")
    if rest or not clauses:
        clause, lines = _untraced(proofs, hashed, rest or given or read, home, target)
        places |= lines
        clauses.append(clause)
    return "may", _either(clauses), places


# A `for` loop's item from a standard slice iterator's `next()` (`for e in expenses`,
# `.iter()`, `.iter_mut()`), and that call: such an item points at a slice's or Vec's
# element. MIR prints the iterator's whole path (`std::slice::Iter`); an adapter's
# (`Enumerate<…>`) is shortened, so it could be the crate's own, and isn't counted.
_FOR_ITEM = re.compile(r"(?:copy|move) \(\(_([0-9]+) as Some\)\.0: &.+\)")
_SLICE_NEXT = re.compile(
    r"<std::slice::Iter(?:Mut)?<'_, .+> as Iterator>::next\((?:move|copy) _[0-9]+\) -> "
)


def _binding_at(proofs, body, root, line, path):
    """The MIR local of the binding of ``root`` (`p`, or `p @ main.rs:3`) that code on
    ``line`` of ``path`` names, or None. OwnedLocals._local's, where one binding is in
    scope there. Where several are (a `let p` shadowing a parameter `p`), the innermost
    of those declared above the line, if their scopes nest: a `let` on the stop's own
    line hasn't run at a line breakpoint, and Rust names the innermost binding. A
    selector naming its declaration, or bindings in scopes that don't nest, give
    None."""
    local = proofs._local(body, root, line, path)
    name, at, _ = root.partition(" @ ")
    if local is not None or at:
        return local
    bindings = body.bindings.get(name, [])
    visible = proofs._visible(body, line, path, None, None, bindings)
    shown = str(path).replace("\\", "/")
    above = [
        binding
        for binding in bindings
        if visible is not None
        and binding[4] in visible
        and binding[2] < line
        and (shown == binding[1] or shown.endswith("/" + binding[1]))
    ]
    if not above or any(binding[0] is None for binding in above):
        return None

    def enclosing(scope):
        found = set()
        for _ in range(4096):
            scope = body.scopes.get(scope)
            if scope is None:
                break
            found.add(scope)
        return found

    inner = [
        binding
        for binding in above
        if all(other is binding or other[4] in enclosing(binding[4]) for other in above)
    ]
    return inner[0][0] if len(inner) == 1 else None


def _slice_element(proofs, body, assigned):
    """Whether the values ``assigned`` to a reference local (``body._assignments()``'s)
    are all a standard slice iterator's items, in a crate whose `std` paths are the
    standard library's (OwnedLocals._std_compares)."""
    found = [_FOR_ITEM.fullmatch(value) for value in assigned]
    if not found or not all(found):
        return False
    every = body._assignments()
    for match in found:
        calls = every.get(int(match[1]), ())
        if not calls or not all(_SLICE_NEXT.match(call) for call in calls):
            return False
    # A crate's own `trait Iterator` (`impl Iterator for std::slice::Iter<…>`) is dumped
    # `<std::slice::Iter<…> as Iterator>::next` too (review of #276).
    return bool(proofs._std_compares()) and bool(proofs._std_iterator())


def _redirected(body, local):
    """Whether the argument ``local`` of the MIR ``body`` may hold another value than
    the call passed, at some stop: the body assigns to it, whole or in part, or takes
    it by `&mut` or as a raw pointer, which can write it (review of #276: `p = &GLOBAL;`).
    Also any `asm!` in the body, which may write any operand (`inout(reg) copy _1 =>
    _1`, round 3), as for C and C++; and a body Peeky doesn't fully read
    (``supported``). Anywhere in the body counts, before the stop or not."""
    if not body.supported or body._assignments().get(local):
        return True
    taken = re.compile(rf"&(?:mut |raw (?:mut|const) )_{local}(?![0-9])")
    return any(
        text.startswith("asm!") or taken.search(text)
        for block in body.blocks.values()
        for text, _, _ in block.statements
    )


def _names(names):
    """`n`, `n` and `m`, `a`, `b` and `c`: names for a message."""
    shown = [f"`{name}`" for name in names]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + f" and {shown[-1]}"


def _line_list(places, home=None):
    """`line 60`, `lines 3 and 4`, `lines 3, 4 and 7`: the lines of ``places`` ({(file,
    line)}) for a message; empty if none. A line of a file other than ``home`` (the
    function's) is named with its file (#222: `format!`'s code is at
    `alloc/src/macros.rs:113`, which `line 113` read as the program's own)."""
    own = sorted({number for file, number in places if home is None or _same_file(file, home)})
    others = sorted(
        {
            f"{_short_file(file)}:{number}"
            for file, number in places
            if home is not None and not _same_file(file, home)
        }
    )
    shown = []
    if len(own) == 1:
        shown.append(f"line {own[0]}")
    elif own:
        shown.append("lines " + ", ".join(map(str, own[:-1])) + f" and {own[-1]}")
    if others:
        shown.append(", ".join(others))
    return " and ".join(shown)


def _same_file(file, home):
    file, home = str(file).replace("\\", "/"), str(home).replace("\\", "/")
    return file == home or file.endswith("/" + home) or home.endswith("/" + file)


def _short_file(file):
    """A MIR span's file as a message names it: the standard library's from its crate
    (`/rustc/<hash>/library/alloc/src/macros.rs` is `alloc/src/macros.rs`)."""
    text = str(file).replace("\\", "/")
    return text.partition("/library/")[2] if "/library/" in text else text


def _never_rank(why):
    """Which of a name's reasons `describe` gives where a generic's instances differ:
    first the ones no edit to the binding itself helps."""
    starts = ("async state", "inlined", "another binding")
    return next((rank for rank, start in enumerate(starts) if why.startswith(start)), 3)


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


# The standard types whose own fields Peeky does not read (#164): each holds no pointer
# but what its type arguments hold (as its MIR name, rust_mir._FREE_PATHS).
_FREE_RECORDS = frozenset(
    {
        "alloc::string::String",
        "alloc::vec::Vec",
        "alloc::boxed::Box",
        "alloc::collections::vec_deque::VecDeque",
        "alloc::collections::btree::map::BTreeMap",
        "alloc::collections::btree::set::BTreeSet",
        "alloc::rc::Rc",
        "alloc::sync::Arc",
        "alloc::alloc::Global",
        "core::option::Option",
        "core::result::Result",
        "core::cell::Cell",
        "core::cell::RefCell",
        "std::collections::hash::map::HashMap",
        "std::collections::hash::set::HashSet",
        "std::hash::random::RandomState",
    }
)


# Of those, the ones that take no type argument.
_UNARGUED_RECORDS = frozenset(
    {"alloc::string::String", "alloc::alloc::Global", "std::hash::random::RandomState"}
)
_STD_PREFIXES = ("std::", "core::", "alloc::")


def free_types(dies):
    """The names (as the MIR prints them, `Span`, and qualified, `spans::Span`) of the
    crate's own structs, enums and unions without type parameters that provably hold no
    pointer, read from the debug information (#164): every field, variant and element,
    however deep, is a number, a C-like enum, an array, a crate type of the same kind,
    or a standard container of such (``_FREE_RECORDS``, by their type arguments); a
    `Box` (which rustc describes as a pointer named for it) by what it owns. A type
    that holds itself through a `Box` or container (`Rec { next: Option<Box<Rec>> }`)
    is judged by everything else it holds: owning a value of its own type borrows
    nothing. Any reference, raw pointer, trait object, function pointer, type not
    described or other standard type holds one. A name two types share is kept only if
    both hold none."""
    from peeky.fields import _target
    from peeky.native import _qualified, _text

    def target(die):
        return _target(die["attrs"].get("DW_AT_type"))

    def members(die):
        for child in die["children"]:
            tag = dies[child]["tag"]
            if tag == "DW_TAG_member":
                yield child
            elif tag in ("DW_TAG_variant_part", "DW_TAG_variant"):
                yield from members(dies[child])

    def parameters(die):
        return [
            child
            for child in die["children"]
            if dies[child]["tag"]
            in ("DW_TAG_template_type_parameter", "DW_TAG_template_value_parameter")
        ]

    def held(offset):
        """The types a value of the type at ``offset`` holds, or None if it may hold a
        pointer by itself."""
        die = dies.get(offset)
        if die is None:
            return None
        tag, attrs = die["tag"], die["attrs"]
        if tag in ("DW_TAG_base_type", "DW_TAG_enumeration_type"):
            return []
        if tag in (
            "DW_TAG_typedef",
            "DW_TAG_const_type",
            "DW_TAG_volatile_type",
            "DW_TAG_array_type",
        ) or (
            tag == "DW_TAG_pointer_type"
            and (_text(attrs.get("DW_AT_name")) or "").startswith("alloc::boxed::Box<")
        ):
            return [target(die)]
        if (
            tag not in ("DW_TAG_structure_type", "DW_TAG_union_type")
            or "DW_AT_declaration" in attrs
        ):
            return None
        qualified = _qualified(dies, offset) or ""
        if qualified.startswith(_STD_PREFIXES):
            record = qualified.split("<", 1)[0]
            if record not in _FREE_RECORDS:
                return None
            # An enum's type arguments are its variants' (`Option<T>`'s on `Some<T>`).
            found = [
                target(dies[child])
                for each in [die, *(dies.get(target(dies[member])) for member in members(die))]
                if each is not None and each["tag"] == "DW_TAG_structure_type"
                for child in parameters(each)
                if dies[child]["tag"] == "DW_TAG_template_type_parameter"
            ]
            return found if found or record in _UNARGUED_RECORDS else None
        if parameters(die) or (_text(attrs.get("DW_AT_name")) or "").startswith("dyn "):
            return None
        return [target(dies[member]) for member in members(die)]

    # Every type the crate's own types reach, then those that reach one holding a
    # pointer by itself (or not described).
    edges, unsafe, pending = {}, set(), []
    names = {}
    for offset, die in dies.items():
        if (
            die["tag"]
            not in ("DW_TAG_structure_type", "DW_TAG_enumeration_type", "DW_TAG_union_type")
            or "DW_AT_declaration" in die["attrs"]
        ):
            continue
        qualified = _qualified(dies, offset) or ""
        name = _text(die["attrs"].get("DW_AT_name")) or ""
        if not name or "<" in name or qualified.startswith(_STD_PREFIXES):
            continue
        names[offset] = {name, qualified} - {""}
        pending.append(offset)
    while pending:
        offset = pending.pop()
        if offset in edges or offset in unsafe:
            continue
        found = held(offset) if offset is not None else None
        if found is None:
            unsafe.add(offset)
            continue
        edges[offset] = found
        pending.extend(found)
    holders = {}
    for offset, found in edges.items():
        for each in found:
            holders.setdefault(each, []).append(offset)
    tainted, pending = set(unsafe), list(unsafe)
    while pending:
        for holder in holders.get(pending.pop(), ()):
            if holder not in tainted:
                tainted.add(holder)
                pending.append(holder)
    verdicts = {}
    for offset, each in names.items():
        for name in each:
            verdicts[name] = verdicts.get(name, True) and offset not in tainted
    return frozenset(name for name, verdict in verdicts.items() if verdict)


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


def _may_hold_raw(kind, generics=()):
    """Whether a value of MIR type ``kind`` may hold a raw pointer, or reach one through
    its references (#255): anything but a primitive, `str`, a fn pointer, or a
    reference, tuple or array made only of those. A struct's or enum's name says nothing
    of its fields; None may be anything; ``generics`` as for _may_own."""
    if kind is None:
        return True
    kind = kind.strip()
    if _FN_POINTER.match(kind):
        return False
    if reference := re.match(r"&(?:'\S* )?(?:mut )?", kind):
        return _may_hold_raw(kind[reference.end() :], generics)
    if kind == "str" or (kind in _PRIMITIVES and generics is not None and kind not in generics):
        return False
    if kind.startswith("[") and kind.endswith("]"):
        return _may_hold_raw(_top_level_parts(kind[1:-1], ";")[0], generics)
    if kind.startswith("(") and kind.endswith(")"):
        parts = [part for part in _top_level_parts(kind[1:-1], ",") if part.strip()]
        return any(_may_hold_raw(part, generics) for part in parts)
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


def _caller_reason(name, held, changed="it"):
    """Why a caller frame's container is not trusted (#260): ``held`` is what the frame
    holds it as (`a local`). Whether the call under way was lent it is not checked, so
    this holds whether it was or not."""
    return (
        f"`{name}` is {held} of a caller frame: Peeky doesn't check whether it was lent to "
        f"{CALLER_CHANGING}, which may be changing {changed}, so its fields aren't trusted "
        "here; Peeky reads it in the innermost frame"
    )


def _raw_container(why, taint=None):
    """A container left raw, saying ``why`` (view_variable). Where a call under way may be
    changing it (a caller frame's, owned or `&mut`), its own fields (`friends.len`) are not
    its value either: they show `[stale]`, each saying why (#260)."""
    if CALLER_CHANGING in why:
        return {"raw_reason": why, "taint": "stale", "carry": True}
    return {"raw_reason": why, **({"taint": taint} if taint else {})}


def _unproven(variable, why=None, pointer=None):
    """``variable`` shown only as its pointer (#101, #108); ``why``, where it is a
    reference past #110's proof, says so (#205). ``pointer`` (#255): "raw" for a raw
    pointer, "unknown" for one the build's debug information does not tell from a raw
    pointer, for the row's note."""
    # Only the address: LLDB's summary of a `char *` (`Box<u8>`) is read through the
    # pointer too.
    address = re.match(r"0x[0-9a-fA-F]+", str(variable.get("value") or ""))
    return {
        "name": variable.get("name"),
        "type": variable.get("type"),
        "value": address[0] if address else "<not shown: may point at freed memory>",
        "variablesReference": 0,
        "rust_view": "unproven",
        **({"why": why} if why else {}),
        **({"pointer": pointer} if pointer else {}),
    }


# What may have run since a reference's last use (rust_mir.OwnedLocals._judge's gaps).
_GAP_WORDS = {
    "call": ("a call", "calls"),
    "drop": ("a drop", "drops"),
    "write": ("a write", "writes"),
    "end": ("the end of a variable's scope", "the ends of variables' scopes"),
}


def _listed(items):
    """``items`` joined as a list in a sentence: `a`, `a and b`, `a, b and c`."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _gap_reason(name, reasons, line):
    """Why the MIR does not show that what the reference ``name`` points at still stands
    at a stop on ``line`` (#259): a clause after "because", from the reason
    OwnedLocals.reference_valid gave (``reasons``, its ``why``). It names what may have
    run since its last use, and where."""
    reason = reasons[0] if reasons else ("since", frozenset())
    if reason[0] in ("dropped", "captured"):
        # Between `drop((*v))` and the store of `*v = new` (through a closure's or a
        # future's capture too).
        return f"what `{name}` points at may be dropped here, before a new value is stored there"
    if reason[0] == "unassigned":
        return f"`{name}` may not be assigned yet here"
    if reason[0] == "elsewhere":
        return (
            "the build's MIR cannot place this stop: it is in code from another file, on a line "
            "the build's IR does not explain, or in a function Peeky could not analyze"
        )
    gaps = reason[1] if reason[0] == "since" else frozenset()
    if not gaps or ("start", None) in gaps:
        return (
            f"`{name}` is not used ahead, and the MIR does not show that nothing has moved, "
            f"changed or freed what it points at since its last use; probe a line that still "
            f"uses `{name}`"
        )
    # Calls and drops are what can free it; a write or a scope's end is named only alone.
    gaps = {gap for gap in gaps if gap[0] in ("call", "drop")} or gaps
    if line is not None and all(at == line for _, at in gaps):
        kinds = [_GAP_WORDS[kind][0] for kind in _GAP_WORDS if (kind, line) in gaps]
        return (
            f"the stop may be past {_listed(kinds)} on this line that "
            f"{'runs' if len(kinds) == 1 else 'run'} after `{name}`'s last use; probe a line "
            f"that uses `{name}` before any call"
        )
    parts = []
    for kind, (one, many) in _GAP_WORDS.items():
        lines = sorted(at for each, at in gaps if each == kind and at is not None)
        macro = (kind, None) in gaps
        if not lines and not macro:
            continue
        where = []
        if lines:
            # The walk back stops at a limit: there may be more than were found.
            shown = [str(n) for n in lines[:4]] + (["others"] if len(lines) > 4 else [])
            where.append(("on lines " if len(lines) > 1 else "on line ") + _listed(shown))
        if macro:
            where.append("in a macro's code")
        # `on lines 3 and 5, and in a macro's code`.
        joined = ", and ".join(where) if " and " in where[0] else _listed(where)
        parts.append(f"{many if len(lines) + macro > 1 else one} {joined}")
    return (
        f"`{name}` is not used ahead, and {_listed(parts)} may have run since its last use; "
        f"probe a line that still uses `{name}`"
    )


class RustSession(NativeSession):
    # mid_statement judges a field or element by the part it changes (#192 review).
    projected_copies = True
    # Which element's address a pointer holds is told in C and C++ only (#302).
    value_extent = None

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
    # Why the crate may have unsafe code, or None (``unsafe_source``; #156 review).
    unsafe_code = None

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
        from peeky.fields import RUST_LIBRARIES

        return function_scopes(
            "\n".join(infos),
            "\n".join(tables),
            workspace,
            tuple(self.lines),
            hidden=self._macro_binding(),
            never=self._register_names,
            settable=(RUST_LIBRARIES, self._kept_in_memory),
            rust=True,
        )

    def _kept_in_memory(self, linkages, name):
        """For `describe`: whether the build's IR keeps the local ``name`` in memory in
        every copy of the function (``linkages``), as a field change needs (#105); never
        in an optimized build."""
        if self._optimized() or not self.memory:
            return False
        hashes = {found[1] for linkage in linkages if (found := _HASH_IN.search(linkage or ""))}
        return bool(hashes) and all(self.memory.get((hashed, name)) is True for hashed in hashes)

    def _register_names(self, linkages, names):
        """For `describe` (#138): of a function's integer locals (``names``: {name:
        types}; `x @ main.rs:6` for one of several of its name, #206), those no stop can
        change, and why, as their rows say (see never_settable_row): `async state` where
        an async body keeps one across an
        `.await` in its state; `inlined` where only a copy of the function inlined where
        it is called keeps it out of memory; `another binding in a register (line N)`
        where a binding of the name has a slot but the one declared on line N has none
        (the IR's proof is per name); else `register`."""
        if self._optimized() or not self.memory:
            return {}
        hashes = sorted(
            {found[1] for linkage in linkages if (found := _HASH_IN.search(linkage or ""))}
        )
        if not hashes:
            return {}
        proofs = getattr(self, "owned", None)
        bodies = {hashed: proofs.body(hashed) if proofs is not None else None for hashed in hashes}

        def why(hashed, name):
            if _state_held(bodies[hashed], _plain(name)):
                return "async state"
            kind, lines = self._not_in_memory(hashed, name)
            if kind == "inlined":
                return "inlined"
            if kind in ("other", "some"):
                return f"another binding in a register (line {', '.join(map(str, lines))})"
            return "register"

        found = {}
        for name, kinds in names.items():
            if not kinds or not set(kinds) <= _SETTABLE_RUST:
                continue
            if any(self.memory.get((hashed, _plain(name))) is True for hashed in hashes):
                continue
            whys = {why(hashed, name) for hashed in hashes}
            found[name] = min(whys, key=lambda each: (_never_rank(each), each))
        return found

    def _macro_binding(self):
        """For `describe`, which bindings the DWARF declares are a macro's own (sixth
        evaluation, finding 12): `println!`'s `args` (`core::fmt::rt::Argument[1]`),
        which rustc places on the line invoking the macro. Told per function by its own
        MIR body (the IR function its linkage name is), which places each binding where
        its code is: a name the body binds in another crate's file (std's `macros.rs`),
        declared where the body places no binding of that name in the program's
        sources, while every binding of the name the body places there has a
        declaration of its own (one moved to the invoking line, by a
        `#[collapse_debuginfo(yes)]` macro of the crate's, keeps them all). Also the
        bindings a `?` desugars to (`residual`, `val`: tenth evaluation, prep 7), which
        the DWARF does not mark artificial: every binding the body places on that line
        under that name has a span ending right after a `?` of the source, where a
        binding the program writes spans its name (``_try_bindings``). Shown when unsure: a
        function with no body matched, and anything without the MIR (an optimized
        build)."""
        proofs = getattr(self, "owned", None)
        if proofs is None:
            return None
        declared = set(self.lines)
        texts = {}

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
                if self._try_bindings(bodies, source, name, line, texts):
                    return True
                places = own.get(name, set())
                if name not in foreign or source is None or (source, line) in places:
                    return False
                at = {(where, number) for where, each, number in positions if each == name}
                return places <= at

            return left_out

        return hidden

    def _try_bindings(self, bodies, source, name, line, texts):
        """Whether ``bodies`` (the MIR of one function's copies) bind ``name`` on
        ``line`` of ``source`` only as a `?` desugars it: each such binding's span ends
        right after a `?` in the source (`residual` spans only the `?`, `val` the
        expression the `?` tries), which an identifier's never does. False where any span is missing or the source
        can't be read as rustc read it. ``texts``: {source: its lines}, read once."""
        if source is None or source not in self.lines:
            return False
        bindings = [
            binding
            for body in bodies
            for binding in body.bindings.get(name, ())
            if binding[1] == source and binding[2] == line
        ]
        ends = [end for body in bodies for end in body.binding_ends.get((name, source, line), ())]
        if not bindings or len(ends) != len(bindings):
            # A binding whose span the MIR did not give: unsure, so shown.
            return False
        if source not in texts:
            try:
                text = read_source(Path(self.prepared.source_root) / source)
            except (OSError, UnicodeDecodeError):
                text = None
            # As rustc counts columns: in characters, with no byte order mark.
            texts[source] = (
                None if text is None else source_lines(text.removeprefix("\ufeff"), True)
            )
        lines = texts[source]
        if lines is None:
            return False
        for end_line, end_column in ends:
            if (
                not 1 <= end_line <= len(lines)
                or not 2 <= end_column <= len(lines[end_line - 1]) + 1
            ):
                return False
            if lines[end_line - 1][end_column - 2] != "?":
                return False
        return True

    def launch_args(self, program, root, args=None):
        launch = super().launch_args(program, root, args)
        return {
            **launch,
            "initCommands": [
                *launch.get("initCommands", ()),
                # Never execute Python scripts embedded beside symbol files.
                "settings set target.load-script-from-symbol-file false",
            ],
        }

    # The rules behind a [settable] mark (mid_statement, _root_holds, validate_member)
    # read the stopped frame alone, never the frames below it: marks are kept by it.
    marks_by_frame = True

    def _optimized(self):
        return getattr(getattr(self, "target", None), "opt_level", "0") != "0"

    def return_withheld(self, variable):
        """Why a Rust return LLDB lists is not shown, or None: integers, bool and floats,
        which come back in a register; never an enum, Option, struct, reference, String
        or char (LLDB shows a char as C's, #155)."""
        kind = str(variable.get("type") or "")
        if kind in RUST_RETURNS:
            return None
        return (
            f"{kind or 'its type'} is not an integer, bool or float: the debugger may read an "
            "enum, struct, reference or char from the wrong place, or show it as C would, so "
            "Peeky shows only scalar returns"
        )

    @staticmethod
    def return_shown(variable):
        """(type, value) as Rust names and prints them: `unsigned char` 'A' is u8 65."""
        kind, value = variable.get("type"), variable.get("value")
        if (byte := lldb_byte(value, kind)) is not None:
            return ("u8" if kind == "unsigned char" else "i8"), byte
        return UNIQUE_SCALARS.get(kind, kind), value

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

    text_language = "rust"
    rust_lines = True

    def text_storage(self, variable, frame):
        """(kind, shape) for text_sets: a String, with its layout from the build's debug
        information; ValueError for anything else (#137)."""
        return text_kind(variable, "rust"), self.layouts.get("alloc::string::String")

    def validate_text_target(self, variable, frame, stop=None):
        """A String's buffer is written only where the MIR proves the local initialized
        at the stop (#108): a moved or dropped String's pointer may point into freed
        memory, and a String not yet assigned holds whatever its slot held. And only
        where no borrow of it is used after the stop (#164): a `&str` taken before the
        change keeps the old length, so it would read a mix of old and new text, or bytes
        that are not UTF-8 (undefined behavior). ``stop``: the stop's reason."""
        name = variable.get("name")
        hashed = _frame_hash(frame)
        proofs = getattr(self, "owned", None)
        place = frame or {}
        line, path = place.get("line"), (place.get("source") or {}).get("path")
        if not (hashed and proofs is not None and proofs.proven(hashed[1], name, line, path)):
            raise ValueError(
                f"`{name}` cannot be changed at this stop: the build's MIR does not prove it "
                "initialized here (it may be moved, dropped or not yet assigned), and a "
                "String's buffer may then be freed memory"
            )
        if unsafe := getattr(self, "unsafe_code", None):
            # #164 review 3: a raw pointer, or an address kept as an integer (`s.as_ptr()
            # as usize`), holds the String's bytes where no borrow shows it.
            raise ValueError(
                f"`{name}` cannot be changed to a text: {unsafe}, or read the String's bytes "
                "at their old length where no borrow shows it (through a raw pointer, or an "
                "address kept as an integer: `s.as_ptr() as usize`)"
            )
        # Before the line's first code (as `mid_statement` requires), or anywhere on it.
        start = stop == "breakpoint" or (stop == "step" and self._line_start(frame))
        lent = proofs.borrow_live(
            hashed[1],
            name,
            line,
            path,
            breakpoint=start,
            column=place.get("column"),
            block=self._stop_block(hashed[1], frame),
            free_types=self._free_types(),
        )
        if lent is not False:
            raise ValueError(
                f"`{name}` cannot be changed to a text at this stop: change it before a borrow "
                "of it is taken, or after the borrow's last use. "
                + (
                    "A borrow of it (a `&str`, a reference or an iterator) is still used after "
                    "the stop"
                    if lent
                    else "Peeky cannot tell from the MIR whether a borrow of it (a `&str`, a "
                    "reference or an iterator) is still used after the stop"
                )
                + ", and a borrow keeps the old length: it would read old and new text mixed, "
                "or bytes that are not UTF-8"
            )

    def text_settable(self, variable, frame, stop=None):
        """Whether a text change could be made to this String here (for `[settable]`)."""
        try:
            self.validate_mutation_target(variable, frame)
            self.validate_text_target(variable, frame, stop)
        except ValueError:
            return False
        return True

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
                "Rust mutation requires a local the program keeps in memory: change what "
                f"{name!r} is computed from before it is assigned, or copy it into a local of "
                "the poll's own after the `.await` with a candidate edit, in the same probe. "
                f"In {where}, it is held in the async body's state across an `.await`, at "
                "every stop of the body: a field of the future, which Peeky never writes"
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
                "Rust mutation requires a local the program keeps in memory: keep it there with "
                "a candidate edit (`std::hint::black_box(&mut x);` after its `let`, with "
                "`--set x=…` in the same probe), or edit the source and run verify. "
                f"In {where}, {name!r} is kept in a register, and the debugger only shows a "
                "copy of it, so changing it would not affect the program"
            )

    def instance_name(self, frame):
        """The frame's function as the build's debug information names it, where its
        symbol's hash is all that tells it apart: a generic's instance with its type
        arguments (`rc::tf<rc::A>`), a closure or async body by its number
        (`cl::main::{closure#1}`, where LLDB shows `{{closure}}` for each). None for any
        other frame. One past 300 characters is cut and ends with `…`."""
        hashed = _frame_hash(frame)
        proofs = getattr(self, "owned", None)
        functions = getattr(proofs, "functions", None) if proofs is not None else None
        function = functions.get(hashed[1]) if hashed and isinstance(functions, dict) else None
        if function is None:
            return None
        own = getattr(function, "instance", "") or (
            function.name if str(function.name).startswith("{") else ""
        )
        if not own:
            return None
        named = "::".join((*function.ancestors, own))
        return named if len(named) <= 300 else named[:299] + "…"

    def never_settable_row(self, variable, frame, member=False, steps=None):
        """`register` where no stop of this build can change the row (#138): the build's
        IR keeps its local (for ``member``, the local holding the field or element) only
        in a register in the frame's function, so the debugger shows a copy. A root row
        only when it is an integer, which nothing else would rule out. Else None, as for
        a member whose path (``steps``: the debugger's child names) `set` refuses first,
        whatever holds it (`sharers.len`, a standard-library type's field, #260)."""
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
        if member and steps is not None and self._path_refused(frame, variable, steps):
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

    def _path_refused(self, frame, root, steps):
        """Whether `set` refuses the member ``steps`` reach from the root local ``root``
        for its path alone (a pointer, a standard-library type, not an integer), as it
        judges that before the root's memory (validate_member)."""
        from peeky.fields import Unsettable, settable_member

        try:
            kind, _, graph = self._root_type(frame, root)
            settable_member(graph, kind, str(root.get("name")), [str(step) for step in steps])
        except Unsettable:
            return True
        return False

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

    def _in_scope(self, hashed, name, line, path):
        """(source, line) of the declaration of the root local ``name`` in scope at
        ``line`` of ``path`` (the MIR's scopes), for _declared_type to pick among a
        function's locals of that name (#206: a loop's `entry`, not an earlier loop's);
        None where the name says it (`x @ main.rs:6`) or that is not one binding."""
        if not is_name(str(name), limit=None) or path is None or type(line) is not int:
            return None
        found = self._binding_line(hashed, str(name), line, path)
        return None if found is None else (path, found)

    def _frame_scope(self, frame, root):
        """_in_scope for the root local ``root`` (a debugger variable) of ``frame``."""
        hashed, place = _frame_hash(frame), frame or {}
        if not hashed:
            return None
        path = (place.get("source") or {}).get("path")
        return self._in_scope(hashed[1], root.get("name"), place.get("line"), path)

    def declared_at(self, variable, frame):
        """(`entry @ main.rs:81`, how many bindings that names) for a root local lldb-dap
        lists by its plain name where its function binds that name more than once
        (#206): where it is declared, as `describe` lists it and as lldb-dap names it
        where another of its name is in scope too. None for another, or where the MIR
        does not tell which it is."""
        name = variable.get("name")
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        if not hashed or proofs is None or not is_name(str(name), limit=None):
            return None
        body = proofs.body(hashed[1])
        places = {(Path(b[1]).name, b[2]) for b in (body.bindings.get(name, ()) if body else ())}
        if len(places) < 2:
            return None
        place = frame or {}
        line, path = place.get("line"), (place.get("source") or {}).get("path")
        local = proofs._local(body, name, line, path) if type(line) is int and path else None
        found = {
            (Path(b[1]).name, b[2])
            for b in body.bindings.get(name, ())
            if local is not None and b[0] == local
        }
        if len(found) != 1:
            return None
        file, number = found.pop()
        # Bindings that share it (`let x = 1; let x = "a";` on one line, or a file of the
        # same name in another directory): it names none of them (#215 review).
        shared = {
            (b[0], b[1]) if b[0] is not None else (None, b[1], b[4])
            for b in body.bindings.get(name, ())
            if (Path(b[1]).name, b[2]) == (file, number)
        }
        return f"{name} @ {file}:{number}", len(shared)

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
                f"(rust-cookbook 6.6): `std::hint::black_box(&mut {name});` after the `let` on "
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
            f"A candidate edit keeps it in memory, in the same probe (rust-cookbook 6.6): {keep}, "
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

    def never_settable(self, program, lines, names, text=False):
        """Why a probe's `--set` of one of ``names`` can change nothing at any stop of
        ``lines`` of ``program`` in this build (#138): the refusal, as (code, message)
        where no such local exists there, else as its message; or None. ``text``: the
        values are texts (#137), which change a String rather than an integer. Only what the
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
                        why = self._never_in(hashed, root, steps, source, line, text)
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
        # A generic's instance with its type arguments (`tf<gen::A>`), so a refusal names
        # each instance it holds in.
        return "::".join((*function.ancestors, getattr(function, "instance", "") or function.name))

    def _never_in(self, hashed, root, steps, source, line, text=False):
        """Why ``root`` (and the path ``steps`` into it) can never change in the function
        ``hashed``: (kind, detail), or None. Raises fields.Unsettable when the debug
        information Peeky needs could not be read (nothing is ruled out then)."""
        from peeky.fields import (
            Unsettable,
            debugger_steps,
            member_indexes,
            pointer_advice,
            settable_member,
        )

        plain = _plain(root)
        # The part a change makes, which the program's own copies are judged by (#192).
        projection = ()
        proofs = getattr(self, "owned", None)
        body = proofs.body(hashed) if proofs is not None else None
        kept = self.memory.get((hashed, plain))
        if kept is None and body is not None and plain not in body.bindings:
            return ("absent", f"has no local `{plain}`")
        # Its type first, as `set` judges it.
        if not steps and text:
            declared = proofs.declared_type(hashed, root, line, source) if proofs else None
            if declared is not None and declared not in _TEXT_RUST:
                borrowed = declared.lstrip("&*").removeprefix("mut ").removeprefix("const ")
                return (
                    "type",
                    f"`{root}` is a {declared}"
                    + (
                        ", text it borrows, which may be read-only (a literal); only a String "
                        "the frame owns can be changed to a text"
                        if borrowed in ("str", *_TEXT_RUST)
                        else "; a text changes a String"
                    ),
                )
        elif not steps:
            declared = proofs.declared_type(hashed, root, line, source) if proofs else None
            if declared is not None and declared not in _SETTABLE_RUST:
                return (
                    "type",
                    f"`{root}` is a {declared}; only integer locals (i16 to i64, u16 to u64, "
                    "isize, usize) can be changed"
                    + (
                        f", and a String to a text in double quotes (--set '{root}=\"apple\"')"
                        if declared in _TEXT_RUST
                        else ""
                    ),
                )
        else:
            functions, graph = self._field_types()
            types = functions.get(hashed)
            if types is None:
                return ("type", "Peeky cannot match it to the build's debug information")
            try:
                kind, framed = self._declared_type(
                    types, root, self._in_scope(hashed, root, line, source)
                )
                # As the stop's walk reads the selector (`sp[-1]`, `sp[01]` are `sp[1]`).
                named = debugger_steps(graph, kind, root, steps)
                if named is None:
                    return None
                settable_member(
                    graph,
                    kind,
                    root,
                    named,
                    # The call advice only for a proven parameter (tenth evaluation).
                    owner=lambda: pointer_advice(
                        root, self._pointer_kind(hashed, root, line, source)
                    ),
                )
                projection = member_indexes(graph, kind, root, named)
                if not framed:
                    raise Unsettable(
                        f"`{root}` is not kept in this frame's own memory (a static, or a "
                        "value the debug information places in registers)"
                    )
            except Unsettable as exc:
                return ("type", str(exc))
        if kept is True:
            return self._copied_everywhere(hashed, plain, source, line, projection)
        if _state_held(body, plain):
            return ("state", plain)
        if kept is None and body is None:
            return ("unproven", plain)
        parameter = body is not None and any(
            binding[0] in body.arguments for binding in body.bindings.get(plain, ())
        )
        return ("register", (parameter, self._not_in_memory(hashed, root, line, source)))

    def _copied_everywhere(self, hashed, plain, source, line, projection=()):
        """("statement", why) where every place ``line`` can stop at in ``hashed`` is
        inside a statement that may hold a copy of ``plain`` it uses again (a statement's
        later line: `let arr = [s.a,` / `k,` / `s.b];` at `k,`), so no stop there can
        change it for that statement (#183); else None. It says the statement has read
        it only where the MIR shows that at every place (#300: ``_copy_reason``)."""
        proofs = getattr(self, "owned", None)
        check = getattr(proofs, "copied_everywhere", None)
        if check is None or self._optimized():
            return None
        path = str(Path(self.prepared.source_root) / source)
        if not check(hashed, plain, line, path, projection=projection):
            return None
        found, explicit, traced = (
            proofs.coded_hazards(hashed, plain, line, path, projection, reads)
            for reads in ("any", "explicit", "traced")
        )
        kind, detail, places = _copy_reason(
            proofs, hashed, found, explicit, traced, source, f"`{plain}`"
        )
        made = _line_list(places, source)
        if kind == "named":
            # #192: a binding of the program's own holds the old value it goes on with.
            return (
                "statement",
                f"at every place line {line} can stop at, the program has already copied "
                f"`{plain}` into {_names(detail)}" + (f" (at {made})" if made else "") + " and "
                "goes on with that copy, so a change there may never be seen: Peeky proves no "
                "read of the changed part first (a reference lent to a call, or `{:?}`, is not "
                "one)",
            )
        if kind == "may":
            return (
                "statement",
                f"every place line {line} can stop at is inside a statement that {detail}, so "
                "it may hold a copy, which it uses after the stop, and a change there may not "
                "reach it",
            )
        return (
            "statement",
            f"every place line {line} can stop at is inside a statement that has already "
            f"read `{plain}` into a temporary" + (f" (at {made})" if made else "") + ", "
            "which it uses after the stop, so a change there would not reach it",
        )

    def _never_message(self, name, root, steps, source, line, failures):
        """The refusal for a `--set` that ``failures`` [(function, (kind, detail))] rule
        out in every function at ``line``, naming each rule and what to do instead."""
        from peeky.fields import FUNCTION, by_function

        plain = _plain(root)
        # (function, rule naming it as FUNCTION): the functions sharing a rule are named
        # together, each rule once (a generic's instances, a closure on the line).
        parts, registers, states = [], set(), False  # registers: the advice for each
        statements = False
        for function, (kind, detail) in failures:
            if kind == "register":
                parameter, why = detail
                rule, advice = self._register_text(FUNCTION, plain, why, parameter)
                parts.append((function, rule))
                registers.add(advice)
            elif kind == "state":
                states = True
                parts.append(
                    (
                        function,
                        f"in {FUNCTION}, `{plain}` is held in the async body's state across "
                        "an `.await` (a field of the future, which the poll reaches through "
                        "a pointer Peeky never writes) at every stop of the body",
                    )
                )
            elif kind == "absent":
                parts.append((function, f"{FUNCTION} {detail}"))
            elif kind == "statement":
                statements = (
                    "copied"
                    if "has already copied" in detail
                    else "untraced"
                    if "may hold a copy" in detail
                    else True
                )
                parts.append((function, f"in {FUNCTION}, {detail}"))
            elif kind == "unproven":
                registers.add(self._register_text(function, plain, ("own", None))[1])
                parts.append(
                    (
                        function,
                        f"in {FUNCTION}, the build's IR gives `{plain}` no memory slot of its "
                        "own (a register, or no such local), so a change would not affect the "
                        "program",
                    )
                )
            else:
                parts.append((function, f"in {FUNCTION}, {detail}"))
        text = (
            f"`{name}` cannot be changed at {source}:{line}, at any stop of this build: "
            + by_function(parts)
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
        if statements == "copied":
            text += (
                ". Change it before the program copies it (`--set-at` an earlier line), or "
                "change the copy instead"
            )
        elif statements:
            text += (
                ". Change it at the first line of that statement (`--set-at` it), before it "
                + ("starts" if statements == "untraced" else "reads the value")
            )
        if states:
            text += (
                ". Change what it is computed from before it is assigned, or copy it with a "
                f"candidate edit in the same probe: `let mut {plain}2 = {plain}; "
                f"std::hint::black_box(&mut {plain}2);` on a line before the stop's, then use "
                f"and set `{plain}2`"
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
                "this poll's copy of the parameter, which a poll resuming after an `.await` "
                f"never assigns, so it may hold leftovers; the future's field, the other `{name}` "
                "row" + (" (withheld too)," if 0 in why else ",") + " holds the value passed in"
            )
        return (
            "never assigned by a poll resuming after an `.await` (the future keeps no copy: "
            "the body does not use it after), so it may hold leftovers"
        )

    def mid_statement(self, variable, frame, stop=None, steps=None, members=False):
        """Why a change to the root local ``variable`` of the stopped ``frame`` (or to the
        part the debugger's child names ``steps`` reach in it; with ``members``, to any
        part of it, leaving out the program's own copies, which each part is judged by)
        may not reach a statement under way at the stop, or None (#105). Allowed only
        where the stop is before its line's first code (``stop``, its reason: a line
        breakpoint's, or a step's that ends at the line's lowest address) and from there
        no temporary the build's MIR shows may hold a value read from it is used again: a
        statement that starts on an earlier line (`let arr = [s.a,` / `k,` / `s.b];` at
        its last line) has already read it. A step that ends elsewhere (back from a call, as after
        `step --out`, or onto a later part of a statement spanning lines), any other
        stop, and anything the MIR does not explain are refused."""
        hashed, proofs, place = _frame_hash(frame), getattr(self, "owned", None), frame or {}
        line = place.get("line")
        if stop == "step" and not self._line_start(frame):
            return _Copied(
                f"the stop is part-way through line {line}, past its first instruction (a "
                "step back from a call, as after `step --out`, or onto a later part of a "
                "statement spanning lines)",
                row=f"part-way through line {line}",
            )
        if stop not in ("breakpoint", "step"):
            return _Copied(
                f"the stop ({stop or 'unknown'}) is not a line breakpoint's or a step's, so "
                "Peeky cannot tell where it is in its statement",
                row="not stopped by a breakpoint or step",
            )
        found = asked = None
        if hashed and proofs is not None and not self._optimized():
            asked = {
                "hashed": hashed[1],
                "variable_name": variable.get("name"),
                "line": line,
                "path": (place.get("source") or {}).get("path"),
                "breakpoint": True,
                "column": place.get("column"),
                "block": self._stop_block(hashed[1], frame),
                "projection": self._projection(
                    hashed[1], variable.get("name"), steps, self._frame_scope(frame, variable)
                ),
                "named": not members,
            }
            found = proofs.copy_hazards(**asked)
        if found is not None and not any(hazards for _, hazards in found):
            return None
        if found:
            # #183: where the copy was made, not an example of where one may be; #300: as a
            # fact only what the MIR shows at every place the stop can be.
            home = (place.get("source") or {}).get("path")
            kind, detail, places = _copy_reason(
                proofs,
                hashed[1],
                found,
                proofs.copy_hazards(**asked, reads="explicit"),
                proofs.copy_hazards(**asked, reads="traced"),
                home,
            )
            made = _line_list(places, home)
            if kind == "named":
                # #192: the program's own binding goes on with the old value.
                name, names = variable.get("name"), _names(detail)
                return _Copied(
                    f"the program has already copied its value into {names}"
                    + (f" (at {made})" if made else "")
                    + f" and goes on with that copy, so a change to `{name}` may never be seen: "
                    "Peeky proves no read of the changed part first (a reference lent to a "
                    "call, or `{:?}`, is not one)",
                    f"Change it before {names} copies it (a stop on an earlier line), "
                    f"or change {names} instead",
                    f"copied into {names}" + (f" ({made})" if made else ""),
                )
            advice = (
                "That statement may already hold a copy of the old value and would not see "
                "the change. Change it before that statement starts: at a stop on an earlier "
                f"line than {line}, or at an earlier stop of line {line} (a probe's "
                "`--set-hit`), where `inspect` marks it [settable]"
            )
            if kind == "may":
                return _Copied(
                    f"a statement under way at the stop {detail}, so it may hold a copy of it, "
                    "which it uses after the stop",
                    advice,
                    "a statement under way may hold a copy" + (f" ({made})" if made else ""),
                )
            # The stop is where its line's code starts (a line breakpoint's, or a step's
            # landing there): the statement under way began before it, on an earlier
            # line or at an earlier stop of this one, never at this stop (#222).
            return _Copied(
                "a statement under way at the stop has already read it into a temporary"
                + (f" (at {made})" if made else "")
                + ", which it uses after the stop",
                advice,
                "a statement under way read it" + (f" ({made})" if made else ""),
            )
        return _Copied(
            "Peeky cannot tell from the build's MIR whether a statement under way at the "
            "stop has read it",
            row="Peeky can't tell if a statement under way read it",
        )

    def _projection(self, hashed, root, steps, where=None):
        """The MIR projection the debugger's child names ``steps`` make in the root local
        ``root`` of the function ``hashed`` (fields.member_indexes): () for the whole
        local, None where the debug information does not tell (#192 review). ``where``:
        the declaration in scope at the stop (_in_scope)."""
        from peeky.fields import Unsettable, member_indexes

        if not steps:
            return ()
        try:
            functions, graph = self._field_types()
            types = functions.get(hashed)
            if types is None:
                return None
            kind, _ = self._declared_type(types, root, where)
            return member_indexes(graph, kind, root, list(steps))
        except Unsettable:
            return None

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
                        table, self.prepared.workspace, tuple(self.lines), RUST_METADATA, rust=True
                    )
                self.assert_current()
            except (OSError, RuntimeError, ValueError):
                rows = []
            self._rows = sorted(rows)
        return self._rows

    def _pointer_advice(self, frame, root, steps):
        """At a stop: fields.pointer_advice for the binding of ``root`` the frame's line
        shows (``_pointer_kind``)."""
        from peeky.fields import pointer_advice

        hashed, place = _frame_hash(frame), frame or {}
        path = (place.get("source") or {}).get("path")
        kind = self._pointer_kind(hashed[1], root, place.get("line"), path) if hashed else None
        return pointer_advice(root, kind)

    def _pointer_kind(self, hashed, root, line, path):
        """What the reference or raw pointer root local ``root`` (`e`, or `e @
        main.rs:64`) of the function ``hashed`` is at ``line`` of ``path``: the MIR's
        binding in scope there (OwnedLocals._local), compared with the body's
        arguments. "parameter", an argument the body never reassigns or lends mutably
        (``_redirected``); "element", a local proven to be a standard slice
        iterator's item (``_slice_element``); "local", one whose value comes from no
        argument; or None where Peeky can't tell (no single whole-local binding in
        scope, a closure's capture, a copy or a pattern of a parameter, a parameter
        that may have been pointed elsewhere)."""
        proofs = getattr(self, "owned", None)
        body = proofs.body(hashed) if proofs is not None and hashed else None
        if body is None or type(line) is not int or not path:
            return None
        local = _binding_at(proofs, body, str(root), line, path)
        if local is None:
            return None
        if local in body.arguments:
            # Only a parameter that still holds what the call passed.
            return None if _redirected(body, local) else "parameter"
        assigned = body._assignments().get(local, ())
        mentioned = {int(n) for value in assigned for n in _MENTIONED.findall(value)}
        if not assigned or mentioned & body.arguments:
            return None
        return "element" if _slice_element(proofs, body, assigned) else "local"

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
        return (
            *self._declared_type(types, root.get("name"), self._frame_scope(frame, root)),
            graph,
        )

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
        return cached[:2]

    def _field_display_type(self, frame, path, kind):
        """The Rust primitive a field or element shows as (`usize`, not LLDB's `unsigned
        long`, which is also `u64`): the base type the build's debug information
        declares at ``path`` (the root local's name, then the debugger's child names),
        where LLDB's ``kind`` spells it (seventh evaluation, finding 18); else None."""
        from peeky.fields import Unsettable, member_base

        if kind in UNIQUE_SCALARS or not any(kind in names for names in RUST_SCALARS.values()):
            return None
        hashed = _frame_hash(frame)
        try:
            functions, _ = self._field_types()
        except Unsettable:
            return None
        types = functions.get(hashed[1]) if hashed else None
        try:
            root, _ = self._declared_type(
                types or {}, path[0], self._frame_scope(frame, {"name": path[0]})
            )
        except Unsettable:
            return None
        declared = member_base(self._field_cache[2], root, path[1:])
        return declared if kind in RUST_SCALARS.get(declared, ()) else None

    def _raw_pointer(self, variable, frame, ancestry):
        """Whether a field or element LLDB shows as a data pointer (``ancestry``: the
        (name, type) of each value above it, its root local first) is a raw pointer, or is
        read through one (#255): "raw" where its Rust type in the build's debug
        information says so (fields.rust_raw_pointer), never LLDB's C-style name, which a
        reference shares (`long *` is `&i64` and `*const i64`). Where that does not say:
        "unknown" in a local whose MIR type may hold a raw pointer (a struct's name says
        nothing of its fields). None otherwise: a reference or a Box, a pointer in a local
        of references and primitives, or no data pointer."""
        from peeky.fields import Unsettable, rust_raw_pointer

        levels = _constructors(variable.get("type"))
        if levels[:1] != ["pointer"] or levels[1:2] == ["function"]:
            return None
        path = [step for step, _ in ancestry] + [variable.get("name")]
        hashed = _frame_hash(frame)
        if hashed and all(isinstance(step, str) for step in path):
            try:
                functions, _ = self._field_types()
                root, _ = self._declared_type(
                    functions.get(hashed[1]) or {},
                    path[0],
                    self._frame_scope(frame, {"name": path[0]}),
                )
                raw = rust_raw_pointer(self._field_cache[2], root, path[1:])
            except (Unsettable, TypeError, IndexError):
                raw = None
            if raw is not None:
                return "raw" if raw else None
        proofs = getattr(self, "owned", None)
        if proofs is None:
            return "unknown"
        types = self._binding_types({"name": path[0]}, frame)
        generics = proofs.type_parameters(hashed[1]) if hashed else ()
        if not types or any(_may_hold_raw(each, generics) for each in types):
            return "unknown"
        return None

    def _free_types(self):
        """The names of the crate's own types that hold no pointer (#164), for the borrow
        check of a text change: read from the build's debug information once."""
        found = self.__dict__.get("_free_cache")
        if found is None:
            try:
                self._field_types()
            except ValueError:
                pass
            found = self.__dict__.get("_free_cache") or frozenset()
            self._free_cache = found
        return found

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
        self._free_cache = free_types(dies)
        graph = type_graph(dies, kinds, RUST_LIBRARIES)
        # And what a local reference or Box points at, one level down: the fields a
        # reference parameter's target holds (#156). Changes never walk through either.
        aimed = {
            node[2]
            for kind in kinds
            if (node := graph.get(kind)) and node[0] in ("pointer", "reference") and node[2]
        } - set(graph)
        if aimed:
            for offset, node in type_graph(dies, aimed, RUST_LIBRARIES).items():
                graph.setdefault(offset, node)
        # The third, through pointers and references too, only names a field's type.
        return functions, graph, type_graph(dies, kinds, RUST_LIBRARIES, pointees=True)

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
        ancestry=None,
    ):
        """Qualified views: containers read from memory, active enum variants; None
        leaves ordinary values unchanged; {"raw_reason": why} leaves a container raw,
        saying why (#156), with "taint" for what it holds (#205) and "carry" where each
        value read through it says why too (#260: a call may be changing it).

        A container is read only for a top-level variable of ``frame`` that is either a
        proven reference parameter (rust_ir), once the frame is past the parameter's
        declaration lines, where the entry block has stored it, or an owned `Vec<T>`
        or `String` local the build's MIR proves initialized at the frame's line
        (rust_mir). A `&mut` parameter and an owned local are read only in the
        innermost frame: from an outer frame, a callee (`retain`, `sort_by` calling back
        into your code) may be midway through changing it.

        ``taint`` (see aggregate_taint) says the value is inside a local whose pointers
        may reach freed memory: a pointer there shows only its address. A raw pointer
        anywhere inside a value shows only its address too (#255). ``ancestry``:
        the (name, type) of each value above a field or element, its root local first,
        for a `Vec`, `String`, `&str` or `Box<str>` field (see _field_container).
        """
        kind = variable.get("type")
        raw = self._raw_pointer(variable, frame, ancestry) if not top and ancestry else None
        if raw == "raw" or (raw and not taint):
            # #255: a raw pointer inside a value (a field, a tuple or array element, a
            # NonNull's) keeps no borrow, so nothing shows that what it points at still
            # stands, however live the value is: only its address, as a local's. One the
            # debug information does not tell apart in a moved value is the next rule's.
            return _unproven(variable, pointer=raw)
        if taint and not top and (_holds_data_pointers(kind) or _is_wide_box(kind)):
            # #108: a Box field (or a Vec's, String's or Rc's pointer, a reference) of a
            # moved, dropped or unproven local; its own fields hold no pointer.
            return _unproven(variable)
        if not top and ancestry:
            held = self._field_container(variable, read, frame, ancestry, innermost, stop, taint)
            if held is not None:
                return held
        parameter = None
        hashed = _frame_hash(frame) if top else None
        proof = self.parameters.get((hashed[1], variable.get("name"))) if hashed else None
        line = (frame or {}).get("line")
        if top and hashed and not innermost and getattr(self, "owned", None) is not None:
            # #260: a `&mut` Vec, String or slice in a caller frame. The call under way
            # (`friends.retain_mut(decide)`) may be part-way through changing it, so even
            # its own fields (`friends.len`) are not its value: they show `[stale]`. A shared
            # one reads in any frame, as a reference parameter's contents do.
            kinds = [proof["type"]] if proof else self._binding_types(variable, frame)
            if kinds and all(_MUT_CONTAINER.match(str(kind or "")) for kind in kinds):
                held = "a `&mut` " + ("parameter" if proof else "reference")
                return _raw_container(
                    _caller_reason(variable.get("name"), held, "what it points at")
                )
        if (
            proof
            and self.target.opt_level == "0"
            and type(line) is int
            and line > proof["line"]
            and (innermost or not proof["type"].startswith("&mut "))
        ):
            parameter = proof["type"]
        # In a crate with unsafe code, what a reference parameter points into may have
        # been freed by it (a caller's `ptr::read`): Peeky reads none of it (#156).
        lent = parameter if getattr(self, "unsafe_code", None) else None
        if lent:
            parameter = None
        proofs = getattr(self, "owned", None)
        # An optimized build's line table cannot place a proof (#13): nothing is proven.
        unoptimized = self.target.opt_level == "0"
        shown_path = ((frame or {}).get("source") or {}).get("path")
        verdict, reasons = [], []

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
                        why=reasons,
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
        if (
            not parameter
            and top
            and innermost
            and unoptimized
            and hashed
            and proofs is not None
            and (kind in ("&str", "&mut str", "&str *") or str(kind).startswith(("&[", "&mut [")))
            and REFERENCE_PARAMETERS.get(
                declared := proofs.declared_type(hashed[1], variable.get("name"), line, shown_path)
            )
            # In a crate with unsafe code, what #110 shows standing may have been freed.
            and not getattr(self, "unsafe_code", None)
            and reference_valid() is True
        ):
            # A `&str` or slice local whose target #110 shows still standing (`v` of `for
            # v in args[2].split(',')`, #156) reads as a parameter's does; so does a `&&str`
            # (`cell` of `for cell in cells`, #205), whose `&str` outlives it.
            parameter = declared
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
            # Nor in a crate with unsafe code, which may have freed what it owns.
            and not getattr(self, "unsafe_code", None)
        )
        layouts = self.layouts
        if option_layouts(kind, layouts):
            # #303: an `Option` of a tuple or struct, read from memory under its proofs.
            return self._option_view(variable, read, frame, layouts, top=top, innermost=innermost)
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
            # #255: a raw pointer keeps no borrow, so nothing proves what it points at: only
            # its address, whatever #110 would find, and also where it cannot judge (an
            # optimized build). An array of them (`[*const Pt; 2]`) is not collapsed: each
            # element is a raw pointer inside a value.
            levels = _constructors(kind)
            if (
                levels[:1] == ["pointer"]
                and levels[1:2] != ["function"]
                and types
                and all(re.match(r"\*(?:const|mut) ", each or "") for each in types)
            ):
                return _unproven(variable, pointer="raw")
            raw_array = (
                levels[:1] == ["array"]
                and bool(types)
                and all(re.match(r"\[+\*(?:const|mut) ", each or "") for each in types)
            )
            provable = innermost and unoptimized and hashed
            gated = (owning or (pointer and not plain)) and not (
                provable
                and proofs.proven(hashed[1], variable.get("name"), line, shown_path)
                # Unsafe code may have freed what a Box holds without moving it.
                and not getattr(self, "unsafe_code", None)
            )
            # Judged in every frame (`--frame N` reads callers): only proofs that make a
            # value readable (an owned container, a Box) are the innermost frame's.
            lent_only = not gated
            if not gated and unoptimized and hashed and not raw_array:
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
            if (
                gated
                and lent_only
                and str(kind).startswith("alloc::vec::Vec<")
                and not _holds_data_pointers(kind)
                and provable
                and proofs.proven(hashed[1], variable.get("name"), line, shown_path)
            ):
                # A Vec of references past #110's proof (`names: Vec<&str>` after its last
                # use, #205): its own fields (`names.len`) are its own, initialized, and
                # read; the pointers inside it show only their address.
                return {
                    "raw_reason": "its items are references, and the build's MIR does not "
                    "show that what they point at still stands here, because "
                    + _gap_reason(variable.get("name"), reasons, line),
                    "taint": "pointers",
                }
            if (
                gated
                and lent_only
                and str(kind).startswith("alloc::vec::Vec<")
                and not _holds_data_pointers(kind)
            ):
                # A Vec #110 does not show standing and the MIR does not prove initialized
                # here (`sharers`, moved on this line), or in a caller frame (#260): not a
                # Box, so its own fields show, as a moved struct's do (#108); the pointers
                # inside it show only their address.
                name = variable.get("name")
                if not provable:
                    return _raw_container(_caller_reason(name, "a local"))
                why = (
                    f"the build's MIR does not prove `{name}` initialized at this line: it "
                    "may be moved, dropped or not yet assigned around it"
                )
                element = str(kind).removeprefix("alloc::vec::Vec<")
                if re.search(r"[&*]", element):
                    why += ", and its items are references"
                return _raw_container(why, taint="pointers")
            if gated:
                # #101: a moved or dropped Box still holds its pointer, and LLDB would
                # read the freed allocation as its fields. The pointer is shown; what it
                # points at is not.
                why = None
                if (
                    lent_only
                    and plain
                    and (
                        _holds_data_pointers(kind)
                        or kind in ("&str", "&mut str")
                        or str(kind).startswith(("&[", "&mut ["))
                    )
                ):
                    # A reference past its last use (`cell` of `for cell in cells`, #205).
                    why = (
                        "the build's MIR does not show that what it points at still stands "
                        "here, because " + _gap_reason(variable.get("name"), reasons, line)
                    )
                return _unproven(variable, why)
        view = active_variant(variable, fetch, layouts, top=top)
        if view and view["rust_view"] == "active-variant" and self.target.opt_level != "0":
            # Optimized code may track an enum's tag and payload separately and LLDB
            # zero-fills a missing piece without reporting it, so a decoded variant is
            # not trustworthy there.
            why = "Peeky decodes an enum's variant only in an unoptimized build"
            return enum_withheld(variable, UNVERIFIED, why, top=top, instead=OPTIMIZED_ADVICE)
        if view is None and lent and (REFERENCE_PARAMETERS.get(lent) or _composite_parameter(lent)):
            # A reference parameter's contents, left raw in a crate with unsafe code.
            return {"raw_reason": getattr(self, "unsafe_code", None)}
        if view is None and top and not parameter and kind in OWNED_CONTAINERS:
            # Why an owned Vec or String stays raw, for its row's note (#156).
            why = self._owned_raw(variable, frame, innermost, owned)
            return _raw_container(why) if why else None
        if (
            view is None
            and top
            and not innermost
            and not parameter
            and str(kind).startswith("alloc::vec::Vec<")
            and (types := self._binding_types(variable, frame))
            and all(str(each or "").startswith("std::vec::Vec<") for each in types)
            and (why := self._owned_raw(variable, frame, innermost, owned))
        ):
            # An owned Vec of other elements (`Vec<Friend>`) in a caller frame says why too,
            # never offering a length the call may be changing (#260).
            return _raw_container(why)
        if view is None and top and kind in ("&str", "&mut str"):
            # A closure's capture (`name` of `|f| f.name == *name`, #260) is never judged:
            # why its text is not shown.
            if self._captured(variable, frame):
                name = variable.get("name")
                return {
                    "raw_reason": f"`{name}` is this closure's capture, and Peeky does not "
                    "judge whether what a capture points at still stands: read it in the "
                    "function that makes the closure"
                }
        return view

    def _captured(self, variable, frame):
        """Whether the frame's root local ``variable`` is a closure's capture: every
        binding of its name in the frame's MIR body is reached through the closure's
        environment, `_1` (`(*((*_1).0: &&str))`)."""
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        body = proofs.body(hashed[1]) if hashed and proofs is not None else None
        if body is None or not body.name.startswith("{closure"):
            return False
        name = str(variable.get("name")).partition(" @ ")[0]
        bindings = body.bindings.get(name, ())
        return bool(bindings) and all(
            binding[0] is None
            and re.match(
                r"[(*]*_1\b", body.places.get((name, binding[1], binding[2], binding[4]), "")
            )
            for binding in bindings
        )

    def _option_view(self, variable, read, frame, layouts, *, top, innermost):
        """An `Option` of a tuple or struct the crate defines (#303), read from memory as
        an owned `Vec` or `String` local is: a local or parameter of the innermost frame
        of an unoptimized build, which the MIR proves initialized at the stop, and whose
        MIR type picks its layout (LLDB's names collide); one holding a `String`, only
        in a crate with no unsafe code, which may have freed its text. Anything else is
        withheld, saying why, where it is stored and what to read instead."""
        kind, name = variable.get("type"), variable.get("name")
        hashed, proofs = _frame_hash(frame) if top else None, getattr(self, "owned", None)
        place = frame or {}
        line, path = place.get("line"), (place.get("source") or {}).get("path")
        # What to do instead, where reading what an `if let` binds is not the way.
        instead = None
        if self.target.opt_level != "0":
            why = "Peeky decodes an enum's variant only in an unoptimized build"
            instead = OPTIMIZED_ADVICE
        elif not top:
            why = (
                "Peeky reads an `Option` of a tuple or struct only as a local or parameter, "
                "not inside another value"
            )
        elif not innermost:
            why = (
                f"`{name}` is a local of a caller frame: Peeky doesn't check whether it was "
                f"lent to {CALLER_CHANGING}, which may be changing it, so Peeky reads it only "
                "in the innermost frame"
            )
            instead = CALLER_ADVICE
        elif hashed is None or proofs is None:
            why = "Peeky cannot match this frame to the build's MIR"
        elif not proofs.proven(hashed[1], name, line, path):
            why = (
                f"the build's MIR does not prove `{name}` initialized at this line: it may "
                "be moved, dropped or not yet assigned around it"
            )
            # It may hold nothing to borrow here: an `if let` on it reads nothing.
            instead = uninitialized_advice(str(name).partition(" @ ")[0])
        else:
            declared = proofs.declared_type(hashed[1], name, line, path)
            chosen = option_name(declared, kind, layouts)
            if chosen is None and declared is None:
                why = (
                    "Peeky picks an `Option`'s layout by its type in the build's MIR, which "
                    "gives none here (in a generic function, it may name a type parameter)"
                )
            elif chosen is None:
                why = (
                    "Peeky picks an `Option`'s layout by its type in the build's MIR, "
                    f"`{str(declared)[:120]}`, and that matches no one layout it reads"
                )
            elif "string" in layouts[chosen] and getattr(self, "unsafe_code", None):
                why = self.unsafe_code
            else:
                return option_view(variable, read, layouts[chosen])
        return enum_withheld(variable, UNVERIFIED, why, top=top, instead=instead)

    def _owned_raw(self, variable, frame, innermost, owned):
        """Why an owned `Vec` or `String` local stays raw (see view_variable)."""
        name = variable.get("name")
        if self.target.opt_level != "0":
            return "Peeky reads a container's contents only in an unoptimized build"
        if not innermost:
            return _caller_reason(name, "a local")
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        if hashed is None or proofs is None:
            return "Peeky cannot match this frame to the build's MIR"
        place = frame or {}
        if not owned and not proofs.proven(
            hashed[1], name, place.get("line"), (place.get("source") or {}).get("path")
        ):
            return (
                f"the build's MIR does not prove `{name}` initialized at this line: it may "
                "be moved, dropped or not yet assigned around it"
            )
        if getattr(self, "unsafe_code", None):
            return getattr(self, "unsafe_code", None)
        if not isinstance(variable.get("memoryReference"), str):
            return _NO_MEMORY
        return None

    def _field_container(self, variable, read, frame, ancestry, innermost, stop, taint):
        """#156: a `Vec`, `String`, `&str` or `Box<str>` field (or element) held where
        nothing can change or free it while the frame stands, read as an owned container
        is: in what a reference parameter the IR proves live points at (`self.values`,
        `&self` and `&mut self`; a `&mut` one only in the innermost frame, as for the
        parameter itself), or in a local (or what its Box holds) the MIR proves
        initialized at the stop, in the innermost frame. The path from the root is plain
        struct and tuple fields and fixed-size array elements (fields.held_container),
        never a pointer, an enum or a standard-library type (a `RefCell`'s contents
        change behind `&self`). A `&str` field borrows: through an owned local, only
        while #110 shows what it borrows still stands; through a reference parameter,
        what it borrows outlives the call. The view, or {"raw_reason": why} where it
        stays raw (elements Peeky does not read too: `Vec<String>`, #183); None where this
        does not apply (another type, a local not proven initialized, whose row says so:
        #108)."""
        from peeky.fields import Unsettable, held_container

        kind = str(variable.get("type") or "")
        root = str(ancestry[0][0])
        if (
            not _FIELD_CONTAINER.fullmatch(kind)
            or taint == "stale"
            or not (is_name(root, limit=None) or is_located(root, r" @ .+", limit=None))
        ):
            return None
        steps = [str(step) for step, _ in ancestry[1:]] + [str(variable.get("name"))]
        if self.target.opt_level != "0":
            return {"raw_reason": "Peeky reads a container's contents only in an unoptimized build"}
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        if not hashed or proofs is None:
            return {"raw_reason": "Peeky cannot match this frame to the build's MIR"}
        if getattr(self, "unsafe_code", None):
            return {"raw_reason": getattr(self, "unsafe_code", None)}
        place = frame or {}
        line, path = place.get("line"), (place.get("source") or {}).get("path")
        proof = self.parameters.get((hashed[1], root))
        verdict, declared, reasons = [], None, []

        def valid():
            # #110 for the root, once.
            if not verdict:
                verdict.append(
                    proofs.reference_valid(
                        hashed[1],
                        root,
                        line,
                        path,
                        breakpoint=innermost and stop == "breakpoint",
                        column=place.get("column"),
                        block=self._stop_block(hashed[1], frame) if innermost else None,
                        why=reasons,
                    )
                )
            return verdict[0]

        if proof and type(line) is int and line > proof["line"]:
            if proof["type"].startswith("&mut ") and not innermost:
                # Its own fields (`self.values.len`) may be mid-change too (#260).
                return _raw_container(
                    _caller_reason(root, "a `&mut` parameter", "what it points at")
                )
            through, dereference = "reference", True
        else:
            if taint == "pointers" or not innermost:
                return _raw_container(_caller_reason(root, "a local"))
            if not proofs.proven(hashed[1], root, line, path):
                return {
                    "raw_reason": f"the build's MIR does not prove `{root}` initialized at "
                    "this line: it may be moved, dropped or not yet assigned around it"
                }
            declared = proofs.declared_type(hashed[1], root, line, path) or ""
            through, dereference = "owned", bool(re.match(r"(?:std::boxed::)?Box<", declared))
            if re.match(r"&(?:mut )?[\w(]", declared):
                # A reference local (`entry` of `for entry in entries.iter()`, #206): what
                # it points at, where #110 shows that still standing; what the target's
                # own references borrow outlives it (`&'b S<'a>` needs `'a: 'b`).
                if valid() is not True:
                    return {
                        "raw_reason": f"`{root}` is a reference, and the build's MIR does "
                        "not show that what it points at still stands here"
                        + (f", because {_gap_reason(root, reasons, line)}" if reasons else "")
                    }
                through, dereference = "lent", True
        try:
            functions, graph = self._field_types()
            types = functions.get(hashed[1])
            if types is None:
                raise Unsettable("Peeky cannot match this frame to the build's debug information")
            offset, _ = self._declared_type(
                types, root, self._in_scope(hashed[1], root, line, path)
            )
            name = held_container(graph, offset, root, steps, dereference=dereference)
        except Unsettable as exc:
            return {"raw_reason": str(exc)}
        layouts = self.layouts
        if name not in layouts or layouts[name].get("kind") == "unread":
            # Elements Peeky never reads (`Vec<String>`): said on the field's own row, as
            # in a listing of what holds it (#183).
            return {"raw_reason": unread_elements(name)}
        if through == "reference" and valid() is False:
            return {
                "raw_reason": f"the build's MIR does not show that what `{root}` points at "
                f"still stands here, because {_gap_reason(root, reasons, line)}"
            }
        if (
            through == "owned"
            and name.startswith("&")
            # A struct with no lifetime holds only `&'static str` (#110 judges the rest).
            and (not declared or _BORROWING.search(declared.removeprefix("std::boxed::")))
            and valid() is not True
        ):
            return {
                "raw_reason": f"it borrows, and the build's MIR does not show that what "
                f"`{root}` borrows still stands here"
                + (f", because {_gap_reason(root, reasons, line)}" if reasons else "")
            }
        view = container_view(variable, read, layouts, optimized=False, field=name)
        if view is None and not isinstance(variable.get("memoryReference"), str):
            return {"raw_reason": _NO_MEMORY}
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
        Rc; any struct, as the MIR names no fields). None: all of it but what a raw
        pointer in it points at (view_variable, #255), where the build's MIR proves it
        initialized at the stop in the innermost frame of an unoptimized build.
        "pointers": its own fields, not what its pointers reach, in a caller frame
        proven the same way (a callee may be midway through replacing a Box it lent).
        "stale": its fields marked as the bytes it left behind, and not what its
        pointers reach, where it may be moved, dropped, partly moved or not yet
        assigned, or nothing is proven (an optimized build, a frame the MIR cannot
        match). The worker passes the result to ``view_variable`` for everything inside
        it."""
        proofs, kind = getattr(self, "owned", None), variable.get("type")
        if proofs is None:
            return None
        if _holds_data_pointers(kind) or _is_wide_box(kind):
            # A pointer at the top is the Box and reference gates' (view_variable); in a
            # crate with unsafe code, the pointers in what it points at show only their
            # address (`self.b`, a Box unsafe code may have freed, #156).
            return "pointers" if getattr(self, "unsafe_code", None) else None
        name = str(variable.get("name"))
        if not (is_name(name, limit=None) or is_located(name, r" @ .+", limit=None)):
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
            # Unsafe code may have freed what its pointers reach (#156).
            return None if innermost and not getattr(self, "unsafe_code", None) else "pointers"
        return "stale"

    def display_type(self, variable, frame, top=True, path=None):
        """A value's Rust type where LLDB shows a C-style name: a root local's primitive
        from the build's MIR (`usize`, not `unsigned long`), a field's or element's
        (``path``: the root's name, then the child names) from the build's debug
        information, any other's when only one primitive renders as that name (`double`
        is `f64`); and a reference parameter's from the build's IR (`&mut
        limiter::Window`, not `limiter::Window *`), which declares each instantiation's
        own types, never a type parameter."""
        kind = variable.get("type")
        if not top and path and (declared := self._field_display_type(frame, path, kind)):
            return declared
        hashed, proofs = _frame_hash(frame), getattr(self, "owned", None)
        proof = self.parameters.get((hashed[1], variable.get("name"))) if top and hashed else None
        if proof and (match := re.fullmatch(r"&(?:mut )?(.+)", proof["type"])):
            # The parameter's only declaration (ir_locals): LLDB's name must spell it.
            if _spelled(f"{match[1]} *", kind):
                return rust_spelling(proof["type"])
        if top and hashed and proofs is not None:
            declared = proofs.declared_type(hashed[1], variable.get("name"))
            if kind in RUST_SCALARS.get(declared, ()):
                return declared
        if scalar := UNIQUE_SCALARS.get(kind):
            return scalar
        return self._rust_name(frame, [variable.get("name")] if top else path, kind)

    def _rust_name(self, frame, path, kind):
        """A library type as the program spells it (#183: `Vec<u32>`, not LLDB's
        `alloc::vec::Vec<unsigned int, alloc::alloc::Global>`; `String`): the name the
        build's debug information gives the type at ``path`` (the root local's name, then
        the debugger's child names), where LLDB's ``kind`` spells it; without one, LLDB's
        own where it names no primitive the C way. None where nothing changes."""
        from peeky.fields import Unsettable, member_type

        kind = str(kind or "")
        if not re.search(r"(?<![\w:])(?:alloc|core|std)::", kind):
            return None
        name = None
        hashed = _frame_hash(frame)
        if hashed and path and all(isinstance(step, str) for step in path):
            try:
                functions, _ = self._field_types()
                root, _ = self._declared_type(
                    functions.get(hashed[1]) or {},
                    path[0],
                    self._frame_scope(frame, {"name": path[0]}),
                )
                node = member_type(self._field_cache[2], root, path[1:])
            except (Unsettable, TypeError, IndexError):
                node = None
            if node is not None and node[0] == "record":
                name = node[11]
            if name and not _spelled(name, kind):
                name = None
        if name is None and not _C_NAMED.search(kind):
            name = kind
        shown = rust_spelling(name) if name else None
        return shown if shown and shown != kind else None

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
