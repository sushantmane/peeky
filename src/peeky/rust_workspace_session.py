"""A probe or session on a Rust workspace's Peeky build (rust_build; ADR 0008, phase 2):
the program the build made, read as rust.RustSession reads a crate's, from the build's
record. The session holds a lease on the build, so no later build removes its files, and
checks the record again before it runs anything (verify): a file changed while it runs
is a provenance change.

One analysed compile is read: the one whose sources hold the file the probe stops in.
Only its debug information units, its MIR and its LLVM IR are read; a frame in a
dependency's code, or in another analysed compile's, has no MIR Peeky read, and none of
its values is proven."""

import hashlib
import json
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import NamedTuple

from peeky import rust_build


class _Item(NamedTuple):
    path: str
    sha256: str


class _Probe(NamedTuple):
    argv: tuple
    stdout: str


class _Record:
    """What a native session reads of a build's manifest, by build.BuildManifest's names."""

    def __init__(self, manifest, environment, rustc, root, workspace):
        self.build_id = manifest["build_id"]
        self.manifest_id = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.effective_environment = tuple(sorted(environment.items()))
        self.probes = (_Probe((rustc, "-vV"), manifest["probes"]["rustc"]),)
        self.artifacts = tuple(_Item(path, sha) for path, sha in manifest["artifacts"].items())
        # The build ran in the workspace itself: its inputs are the user's files, by their
        # paths from the workspace, which the debug information's are relative to.
        self.original_inputs = self.inputs = tuple(
            _Item((Path(root) / path).relative_to(workspace).as_posix(), sha)
            for path, sha in manifest["sources"].items()
            if (Path(root) / path).is_relative_to(workspace)
        )


class _Prepared:
    """What a native session reads of its prepared build (build.PreparedRun's names): the
    workspace, which the debug information's paths are relative to, and Peeky's private
    directory for the session (the debugger's home), removed with the lease released."""

    def __init__(self, workspace, record, lease, private):
        self.workspace = self.source_root = Path(workspace)
        self.manifest = record
        self.lldb_home = Path(private) / "lldb-home"
        self._lease, self._private = lease, Path(private)
        # The other analysed crates, which a note names as proven apart.
        self.analysed_crates = frozenset()

    def cleanup(self):
        try:
            shutil.rmtree(self._private, ignore_errors=True)
        finally:
            self._lease.close()


class _UnitTarget:
    """The workspace's target as a crate's session reads it: its tools and arguments,
    with the analysed compile's sources (relative to the workspace), at opt-level 0."""

    opt_level = "0"
    tests = ()

    def __init__(self, target, sources):
        self._target, self.sources = target, tuple(sources)

    def __getattr__(self, name):
        return getattr(self._target, name)

    @property
    def verification_inputs(self):
        return self.sources


def _session_class():
    from peeky.rust import RustSession

    class WorkspaceSession(RustSession):
        """rust.RustSession on a workspace's build: its record checked again before
        anything runs, and the program run where Cargo runs it."""

        # Set by prepare: the root, the build's directory and record, the program's
        # working directory.
        root = directory = build = cwd = None

        def assert_current(self):
            try:
                rust_build.verify(self.target._target, self.root, self.directory, self.build)
            except rust_build.BuildRequired as exc:
                what = str(exc).removeprefix("No current Peeky build of this workspace: ")
                raise RuntimeError(
                    f"Provenance: {what.split('. Run ', 1)[0]}, while this session ran on "
                    "that build. Run `peeky build` again, then start a new session"
                ) from None

        def owns_source(self, path):
            """Whether ``path`` (resolved) is one of the analysed compile's files: the
            program a stop is read in. A dependency's file, or another member's, is read
            as a library's, though under the workspace."""
            try:
                relative = Path(path).relative_to(self.prepared.workspace).as_posix()
            except ValueError:
                return False
            return relative in self.target.sources

        def launch_args(self, program, root, args=None):
            launch = super().launch_args(program, root, args)
            return {**launch, "cwd": str(self.cwd)}

    return WorkspaceSession


def _private(directory):
    """A new private directory for one session's files, in the build's directory."""
    base = Path(directory) / "sessions"
    base.mkdir(mode=0o700, exist_ok=True)
    private = base / secrets.token_hex(6)
    private.mkdir(mode=0o700)
    return private


def _environment(target, run_directory):
    """What the debugger, and the program it runs, are given: the tools' directory first
    on PATH, the C locale, the SDK on macOS, and the run's package directory as Cargo
    gives it (`CARGO_MANIFEST_DIR`)."""
    env = {
        "PATH": ":".join(dict.fromkeys((str(Path(target.rustc).parent), "/usr/bin", "/bin"))),
        "LC_ALL": "C",
        "CARGO_MANIFEST_DIR": str(run_directory),
    }
    if target.sysroot is not None:
        env["SDKROOT"] = str(Path(target.sysroot).resolve(strict=True))
    return env


# What rustc's expansion adds before a crate's own text: its prelude, after the crate's
# own inner attributes and documentation (`-Zunpretty=expanded`).
_INJECTED_FEATURE = "#![feature(prelude_import)]\n"
_INJECTED_PRELUDE = re.compile(
    r"^extern crate (?:std|core);\n#\[prelude_import\]\n"
    r"use (?:std|core)::prelude::rust_20[0-9]{2}::\*;\n",
    re.M,
)
# The name the expanded text goes by among a session's sources: no path ends in it.
EXPANDED = "<expanded>"


def expanded_code(path):
    """The analysed crate's text as rustc expands it (``path``: the build's
    `expanded.rs`), without what the expansion itself adds (``_INJECTED_FEATURE``, which
    would read as a nightly feature, and the prelude's `use`); as written where that
    isn't found as rustc writes it, so the feature reads as one. None where unread."""
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith(_INJECTED_FEATURE):
        return text
    rest = text[len(_INJECTED_FEATURE) :]
    found = _INJECTED_PRELUDE.search(rest)
    if found is None:
        return text
    return rest[: found.start()] + rest[found.end() :]


def prepare_session(target, program, root, *, deadline=None):
    """The session on ``target``'s current build under ``root`` that stops in ``program``
    (a file of an analysed package): refused where there is none (BuildRequired), or where
    no analysed compile, or more than one, read that file."""
    from peeky.native import module_identity
    from peeky.rust import SYMBOL_BUDGET_S, _absolute_tool, std_macro_files, unsafe_source
    from peeky.rust_ir import ir_locals
    from peeky.rust_mir import _STD_CRATES, OwnedLocals, ir_functions, load_bodies
    from peeky.rust_symbols import application_symbols, without_numbered_structs

    root = Path(root).resolve(strict=True)
    lease = rust_build.lease_build(target, root)
    private = None
    try:
        manifest = lease.manifest
        directory = Path(manifest["directory"])
        workspace = Path(manifest["workspace"])
        relative = Path(program).resolve().relative_to(root).as_posix()
        units = [unit for unit in manifest["units"] if relative in unit["sources"]]
        if not units:
            packages = ", ".join(target.config.analyse)
            raise ValueError(
                f"Peeky stops only in the analysed packages' files ({packages}), as their "
                f"build read them: {relative} is none of those. Probe a line there, or add "
                'its package to "analyse" and build again'
            )
        if len(units) > 1:
            crates = ", ".join(f"`{unit['crate']}`" for unit in units)
            raise ValueError(
                f"Peeky reads one analysed compile per session, and {relative} is in several "
                f"the program runs ({crates}). Probe a file of one of them alone"
            )
        unit = units[0]
        if not unit["expanded"]:
            raise ValueError(
                f"rustc couldn't expand `{unit['package']}`'s {unit['crate']}, so Peeky can't "
                "read its macros' code. See `peeky build`'s output"
            )
        files = directory / unit["unit"]
        declared = []
        for name in unit["sources"]:
            path = (root / name).resolve()
            if not path.is_relative_to(workspace):
                raise ValueError(
                    f"Peeky reads an analysed compile's files inside the workspace, and "
                    f"`{unit['package']}`'s {name} is outside {workspace}"
                )
            declared.append(path.relative_to(workspace).as_posix())
        declared = tuple(sorted(declared))
        entry = Path(unit["source"]).relative_to(workspace).as_posix()
        crate = unit["crate"]
        run = manifest["run"]
        binary = directory / run["executable"]
        symbols = binary
        if sys.platform == "darwin":
            # Cargo's copy of the bundle keeps its DWARF file's name (`app-<hash>`).
            found = sorted(
                (binary.with_name(binary.name + ".dSYM") / "Contents/Resources/DWARF").iterdir()
            )
            if len(found) != 1 or not found[0].is_file() or found[0].is_symlink():
                raise ValueError(f"The build's debug symbols for {binary.name} are not one file")
            symbols = found[0]
        package = next(each for each in manifest["local"] if each["name"] == run["package"])
        tested = "bin" not in run["kinds"]
        cwd = (root / package["directory"]).resolve() if tested else workspace
        env = _environment(target, (root / package["directory"]).resolve())
        private = _private(directory)
        record = _Record(manifest, env, target.rustc, root, workspace)
        prepared = _Prepared(workspace, record, lease, private)
        ir = files / "ir.ll"
        declarations = {}
        memory, parameters = ir_locals(ir, declarations)
        texts = {
            name: (workspace / name).read_bytes().decode("utf-8", errors="replace")
            for name in declared
        }
        # The scans of the crate's text read it as rustc expands it too: what macros
        # write into it, a dependency's procedural macros' among it (``EXPANDED``).
        expanded = expanded_code(files / "expanded.rs")
        if expanded is None:
            raise ValueError(
                f"Peeky can't read rustc's expansion of `{unit['package']}`'s {crate}: build again"
            )
        defined = set()
        # The other crates of the build, whose code these proofs read none of: the
        # dependencies, and the other analysed crates, proven apart.
        analysed = frozenset(each["crate"] for each in manifest["units"])
        foreign = frozenset(manifest["crates"]) - {crate} - _STD_CRATES
        prepared.analysed_crates = analysed - {crate}
        owned = OwnedLocals(
            load_bodies(files / "mir", crate, defined),
            ir_functions(ir, declared),
            (*declared, EXPANDED),
            texts={**texts, EXPANDED: expanded},
            defined=defined,
            foreign=foreign,
        )
        dwarfdump = _absolute_tool(target.dwarfdump, "dwarfdump")
        budget = SYMBOL_BUDGET_S
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
        lines, layouts, debug_units, blocks, interior = application_symbols(
            dwarfdump,
            symbols,
            workspace,
            env,
            entrypoint=entry,
            crate=crate,
            declared=declared,
            budget=budget,
            only_named=True,
        )
        rust = [name for name in declared if name.endswith(".rs")]
        layouts = without_numbered_structs(
            layouts,
            [*(texts[name] for name in rust), expanded],
            [t for n, t in texts.items() if n not in rust],
        )
        identity = module_identity(dwarfdump, binary, symbols, workspace, env, deadline=deadline)
        session = _session_class()(
            _UnitTarget(target, declared),
            prepared,
            _absolute_tool(target.adapter, "adapter"),
            lines,
            identity,
            binary,
            symbols,
            layouts,
            private,
            memory,
            parameters,
            owned,
        )
        session.root, session.directory, session.build, session.cwd = (
            root,
            directory,
            manifest,
            cwd,
        )
        session.units = tuple(debug_units)
        session.code_blocks = blocks
        session.declarations_ir = declarations
        # A dependency's macros write its own code, taken as sound as std's are; a local
        # procedural macro writes the project's, which only the expansion shows. What the
        # build generated and the crate read (a build script's `OUT_DIR` file it
        # `include!`s) is the project's code too.
        session.unsafe_code = unsafe_source(texts, rust)
        if session.unsafe_code is None and unit["generated"]:
            made = {
                f"{name}, which the build generated": (directory / "target" / name)
                .read_bytes()
                .decode("utf-8", errors="replace")
                for name in unit["generated"]
            }
            session.unsafe_code = unsafe_source(
                made, [label for label in made if label.split(",", 1)[0].endswith(".rs")]
            )
        if session.unsafe_code is None and manifest["macros"]:
            macros = ", ".join(f"`{name}`" for name in manifest["macros"])
            label = f"`{crate}` as rustc expands it, with the local procedural macros {macros}"
            session.unsafe_code = unsafe_source({label: expanded}, [label])
        owned.unsafe_code = session.unsafe_code
        owned.interior = interior
        owned.std_macros = std_macro_files(target.rustc, record.probes, (workspace, root))
        session.assert_current()
        return session
    except BaseException:
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        lease.close()
        raise
