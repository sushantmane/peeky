"""ADR 0008 phase 1's acceptance of one study project, in its image (accept.sh runs it).

It builds the project as the phase asks (build.sh), measures the binary against Peeky's symbol
bounds (measure.py), and runs Peeky on it as an agent would. It checks how many units Peeky
judges and reads. Every value whose correctness it asserts has an oracle outside Peeky, named
in its check: the program's output (jq's, valkey's CLIENT LIST, gtest's), the source the build
generated (jq's parser.c), another tool's output (mpy-cross's, read by mpy-tool), the program's
arguments (fmt's argc), or the program's own copy (valkey's original). Values captured only to
select a hit (MicroPython's comp.pass), or checked only for their note (its l_finally_block),
have none. Each refusal must say why. Then it checks the refusals
projects share: GCC's and DWARF 4's builds, and the project's optimized build. A file edited
without a rebuild, and the optimized build read with "optimized": true, are checked where
Peeky judges a unit: json's binary is refused whole, so neither applies. Each check records
how long Peeky took. Fixtures, logs and Peeky's state are in one scratch directory
(PEEKY_ACCEPT_SCRATCH, which accept.sh makes and removes).

Usage: accept.py <jq|valkey|micropython|fmt3158|fmt3272|json>

The report is JSON on stdout, and a line per check on stderr. Exit status 1 if a check failed.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTBED = Path("/testbed")
HERE = Path(__file__).resolve().parent
# Never `/` or `/root`: one directory only root can write, which accept.sh removes.
SCRATCH = Path(os.environ.get("PEEKY_ACCEPT_SCRATCH") or HERE.parents[1] / "scratch")
STATE = SCRATCH / "state"
TOOLS = {
    "PEEKY_NATIVE_DAP": "/usr/bin/lldb-dap-18",
    "PEEKY_NATIVE_DWARFDUMP": "/usr/bin/llvm-dwarfdump-18",
    "PEEKY_NATIVE_CC": "/usr/bin/clang-18",
    "PEEKY_NATIVE_CXX": "/usr/bin/clang++-18",
}


class Acceptance:
    def __init__(self, project):
        self.project = project
        self.checks, self.probes, self.builds = [], [], {}
        self.binary = None
        SCRATCH.mkdir(mode=0o700, exist_ok=True)
        STATE.mkdir(mode=0o700, exist_ok=True)
        # Peeky builds and keeps its state only where no other user can write.
        self.env = {
            **os.environ,
            **TOOLS,
            "PEEKY_STATE_DIR": str(STATE),
            "TMPDIR": str(STATE),
            "PYTHONPATH": str(HERE.parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def check(self, name, ok, detail=None, seconds=None):
        entry = {"check": name, "passed": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        if seconds is not None:
            entry["seconds"] = seconds
        self.checks.append(entry)
        shown = f" ({seconds} s)" if seconds is not None else ""
        print(f"{'PASS' if ok else 'FAIL'} {name}{shown}", file=sys.stderr, flush=True)
        if not ok:
            print(f"     {json.dumps(detail)[:3000]}", file=sys.stderr, flush=True)
        return ok

    def build(self, variant=None):
        started = time.monotonic()
        done = subprocess.run(
            ["bash", str(HERE / "build.sh"), self.project, *([variant] if variant else [])],
            capture_output=True,
            text=True,
            env=self.env,
        )
        seconds = round(time.monotonic() - started, 1)
        self.builds[variant or "default"] = seconds
        if done.returncode:
            raise RuntimeError(f"build {variant or 'default'} failed: {done.stderr[-3000:]}")
        self.binary = done.stdout.strip()
        return self.binary

    def peeky(self, *argv, program=(), timeout=300):
        """(exit status, its JSON answer, seconds) of `peeky ARGV --json -- PROGRAM`, run in
        /testbed."""
        started = time.monotonic()
        tail = ["--", *program] if program else []
        done = subprocess.run(
            [sys.executable, "-m", "peeky.cli", *argv, "--json", *tail],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=TESTBED,
            timeout=timeout,
        )
        seconds = round(time.monotonic() - started, 2)
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            answer = {"stdout": done.stdout[-3000:], "stderr": done.stderr[-3000:]}
        return done.returncode, answer, seconds

    def probe(self, at, capture, *program, config=None, extra=()):
        """A probe of the binary: (exit status, result, seconds)."""
        target = ["--config", str(config)] if config else ["--binary", self.binary]
        argv = ["probe", ".", *target, "--at", at, "--no-save", "--quiet", *extra]
        if capture:
            argv += ["--capture", ",".join(capture)]
        code, result, seconds = self.peeky(*argv, program=program)
        self.probes.append(
            {
                "at": at,
                "status": result.get("status") if isinstance(result, dict) else None,
                "seconds": seconds,
                "peeky_seconds": result.get("seconds") if isinstance(result, dict) else None,
            }
        )
        return code, result, seconds

    def config(self, **given):
        """A binary config naming this host's tools, written to the state directory."""
        path = STATE / "binary.json"
        language = "cpp" if self.project.startswith(("fmt", "json")) else "c"
        compiler = TOOLS["PEEKY_NATIVE_CXX" if language == "cpp" else "PEEKY_NATIVE_CC"]
        data = {
            "binary": self.binary,
            "compiler": compiler,
            "adapter": TOOLS["PEEKY_NATIVE_DAP"],
            "dwarfdump": TOOLS["PEEKY_NATIVE_DWARFDUMP"],
            **given,
        }
        path.write_text(json.dumps(data))
        return path

    def measure(self, compiler):
        started = time.monotonic()
        done = subprocess.run(
            [
                sys.executable,
                str(HERE / "measure.py"),
                TOOLS["PEEKY_NATIVE_DWARFDUMP"],
                str(TESTBED / self.binary),
                str(TESTBED),
                compiler,
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        if done.returncode:
            raise RuntimeError(f"measure failed: {done.stderr[-3000:]}")
        report = json.loads(done.stdout)
        report["seconds"] = round(time.monotonic() - started, 1)
        bounds = report["bounds"]
        fits = (
            report["line_tables"]["bytes"] <= bounds["line_tables"]
            and report["debug_information"]["bytes"] <= bounds["debug_information"]
            and report["debug_information"]["entries"] <= bounds["entries"]
            and report["debug_information"]["kept"] <= bounds["kept"]
        )
        self.check(
            "symbols fit Peeky's bounds (MAX_LINE_TEXT, MAX_DEBUG_INFO, MAX_DIES, MAX_KEPT)",
            fits,
            {
                "line_tables": report["line_tables"],
                "debug_information": report["debug_information"],
                "bounds": bounds,
            },
        )
        return report

    def describe(self, config=None):
        target = ["--config", str(config)] if config else ["--binary", self.binary]
        return self.peeky("describe", ".", *target)

    def refusals(self, edited, at, program, extra=(), none_judged=False):
        """The refusals every project shares, at ``at`` (a line of a unit Peeky judges in
        the default build), running ``program``: an edit without a rebuild names the file;
        the project's optimized build is refused unless opted in; GCC's and DWARF 4's are
        refused, each saying what to rebuild with. ``none_judged``: a project none of whose
        units Peeky judges, where a refusal lists the reasons instead, and an edit can't
        be noticed: no file is checked of a binary refused whole."""
        if not none_judged:
            path = TESTBED / edited
            original = path.read_bytes()
            path.write_bytes(original + b"\n/* edited after the build */\n")
            try:
                code, result, seconds = self.probe(at, [], *program, extra=extra)
            finally:
                path.write_bytes(original)
            error = (result.get("error") or {}) if isinstance(result, dict) else {}
            self.check(
                "an edit without a rebuild is refused, naming the file",
                code == 2
                and error.get("code") == "PROVENANCE_CHANGED"
                and edited in error.get("message", ""),
                error,
                seconds,
            )
        for variant, expected in (
            ("optimized", r"built with (-O\w+) \(its recorded flags\)"),
            ("gcc", r"built by GCC, which writes no checksum of its files: rebuild with clang"),
            ("dwarf4", r"DWARF 4, which records no checksum of its files: rebuild with -gdwarf-5"),
        ):
            self.build(variant)
            code, result, seconds = self.probe(at, [], *program, extra=extra)
            error = (result.get("error") or {}) if isinstance(result, dict) else {}
            found = re.search(expected, error.get("message", ""))
            self.check(
                f"the {variant} build is refused, saying why",
                code == 2 and error.get("code") == "UNSUPPORTED" and found,
                error,
                seconds,
            )
            if variant == "optimized" and found and not none_judged:
                config = self.config(optimized=True)
                # The optimized build's own breakpoint line, at or after ``at``: an -O level
                # leaves some lines no code.
                _, manifest, _ = self.describe(config)
                file, _, number = at.rpartition(":")
                lines = sorted(
                    each
                    for each in (manifest.get("breakpoint_lines") or {}).get(file, ())
                    if each >= int(number)
                )
                there = f"{file}:{lines[0]}" if lines else at
                code, result, seconds = self.probe(there, [], *program, config=config, extra=extra)
                identity = result.get("target_identity") or {}
                self.check(
                    'the optimized build is read as optimized with "optimized": true',
                    # The session's level is the highest of any unit read (MicroPython's
                    # vm.c is -O3 in a -Os build).
                    result.get("hits", 0) >= 1 and identity.get("optimization") not in (None, "O0"),
                    {
                        "status": result.get("status"),
                        "error": result.get("error"),
                        "hits": result.get("hits"),
                        "refused unit's level": found[1],
                        "optimization": identity.get("optimization"),
                    },
                    seconds,
                )

    def units(self, manifest, total, judged, unread=()):
        """Records how many units `describe` judges, whose text it reads, and why any call
        may return twice; checks that it judges ``judged`` of ``total`` units, and reads the
        text of every one of them but those ``unread`` gives a reason for, each its own."""
        units = manifest.get("units") or []
        read = [unit for unit in units if unit.get("judged")]
        withheld = [unit for unit in read if unit.get("text")]
        twice = manifest.get("returns_twice") or {}
        said = [
            re.sub(r"Peeky can't read \S+ as compiled: ", "", unit["text"]) for unit in withheld
        ]
        detail = {
            "units": len(units),
            "judged": len(read),
            "text unread": len(withheld),
            "why unread": sorted(set(said))[:5],
            "not judged": {
                unit["unit"]: unit.get("why") for unit in units if not unit.get("judged")
            },
            "returns_twice": twice,
        }
        self.summary = detail
        self.check(
            f"{judged} of the {total} units are judged, and all but {len(unread)} of them read",
            len(units) == total and len(read) == judged and sorted(said) == sorted(unread),
            detail,
        )
        return detail

    def report(self):
        return {
            "project": self.project,
            "passed": all(each["passed"] for each in self.checks),
            "units": getattr(self, "summary", None),
            "builds_seconds": self.builds,
            "checks": self.checks,
            "probes": self.probes,
        }


def _line(path, pattern, start=1):
    """The number of the first line of ``path`` (under /testbed), from ``start``, that
    ``pattern`` matches."""
    for number, text in enumerate((TESTBED / path).read_text().splitlines(), 1):
        if number >= start and re.search(pattern, text):
            return number
    raise LookupError(f"{pattern!r} not in {path}")


def _rows(result):
    return {
        (row.get("hit"), row.get("selector")): row
        for row in (result.get("rows") or [] if isinstance(result, dict) else [])
    }


def _error(result):
    return (result.get("error") or {}) if isinstance(result, dict) else {}


def _no_uncovered(run, detail):
    """Code no unit covers, beyond what truly is, is the first reason any call may return
    twice: it comes first in the note."""
    why = (detail.get("returns_twice") or {}).get("why") or ""
    return why


def jq(run):
    """jq-2650: `jq '.a.[]'`, with its grammar generated by `bison -l`: an action is probed
    in src/parser.c; a breakpoint in src/parser.y is refused; built with bison's `#line`,
    parser.c's table records no MD5s, and its breakpoints are refused saying so."""
    run.build()
    report = run.measure("clang-18")
    code, manifest, seconds = run.describe()
    # With bison -l and flex -L, every unit is judged, parser.c and lexer.c too.
    detail = run.units(manifest, 36, 36)
    run.check(
        "no code no unit covers is named: the toolchain's archive code is its own",
        "is code no unit's debug information covers" not in _no_uncovered(run, detail),
        detail["returns_twice"],
    )
    (SCRATCH / "in.json").write_text('{"a": 7}\n')
    data = str(SCRATCH / "in.json")
    # The grammar's action for `Term: FIELD`, which `.a` reduces by.
    case = _line("src/parser.c", r"case \d+: /\* Term: FIELD  \*/")
    action = _line("src/parser.c", r"\(yyval\.blk\) = gen_index\(gen_noop\(\)", case)
    lines = (TESTBED / "src/parser.c").read_text().splitlines()
    rule = int(re.search(r"case (\d+):", lines[case - 1])[1])
    code, result, seconds = run.probe(f"src/parser.c:{action}", ["yyn", "yylen"], ".a.[]", data)
    rows = _rows(result)
    run.check(
        "an action in src/parser.c is probed: its rule and length, as the grammar has them",
        result.get("hits") == 1
        and rows.get((1, "yyn"), {}).get("value") == str(rule)
        and rows.get((1, "yylen"), {}).get("value") == "1",
        {"rows": list(rows.values()), "error": result.get("error")},
        seconds,
    )
    # yyerror, in the grammar's epilogue: the message and the count the program prints.
    after = _line("src/parser.c", r"\(\*errors\)\+\+;") + 1
    code, result, seconds = run.probe(f"src/parser.c:{after}", ["s", "errors[0]"], ".a.[]", data)
    rows, output = _rows(result), result.get("output", "")
    said = re.search(r'"(.*)"', rows.get((1, "s"), {}).get("value", "")) if rows else None
    run.check(
        "yyerror's message and error count are what jq prints",
        said is not None
        and f"jq: error: {said[1]} (Unix shell quoting issues?)" in output
        and rows.get((1, "errors[0]"), {}).get("value") == "1"
        and "jq: 1 compile error" in output,
        {"rows": list(rows.values()), "output": output[:500]},
        seconds,
    )
    # A value the program prints: main.c's result, `.a + 1`. -M: monochrome, as the
    # debugger gives jq a terminal.
    dumped = _line("src/main.c", r"^\s*jv_dump\(result, dumpopts\);")
    code, result, seconds = run.probe(
        f"src/main.c:{dumped}", ["result.u.number"], "-M", ".a + 1", data
    )
    rows = _rows(result)
    run.check(
        "main.c's result is the number jq prints",
        rows.get((1, "result.u.number"), {}).get("value") == "8"
        and result.get("output", "").strip().splitlines()[:1] == ["8"],
        {"rows": list(rows.values()), "output": result.get("output", "")[:300]},
        seconds,
    )
    # jv_print.c's text is read: the number it prints is the one jq prints.
    units = {unit["unit"]: unit for unit in manifest.get("units", [])}
    term = _line("src/jv_print.c", r"^\s*double d = jv_number_value\(x\);") + 1
    code, result, seconds = run.probe(f"src/jv_print.c:{term}", ["d"], "-M", ".a + 1", data)
    row = _rows(result).get((1, "d"), {})
    unread = units.get("src/jv_print.c", {}).get("text")
    run.check(
        "jv_print.c's text is read, and its number is the one jq prints",
        unread is None
        and row.get("value") == "8"
        and result.get("output", "").strip().splitlines()[:1] == ["8"],
        {"text": unread, "row": row, "output": result.get("output", "")[:300]},
        seconds,
    )
    code, result, seconds = run.probe("src/parser.y:670", ["x"], ".a", data)
    error = _error(result)
    run.check(
        "a breakpoint in src/parser.y is refused: no judged unit has code there",
        code == 2
        and error.get("code") == "UNSUPPORTED"
        and "has code in src/parser.y" in error.get("message", ""),
        error,
        seconds,
    )
    run.refusals("src/main.c", f"src/main.c:{dumped}", ("-M", ".a + 1", data))
    run.build("lines")
    code, result, seconds = run.probe(f"src/parser.c:{after}", ["s"], ".a.[]", data)
    error = _error(result)
    message = error.get("message", "")
    run.check(
        "built with bison's #line, src/parser.c is refused: no MD5s, bison -l named",
        code == 2
        and error.get("code") == "UNSUPPORTED"
        and "src/parser.c is a unit Peeky doesn't judge" in message
        and "records no MD5s" in message
        and "bison -l" in message,
        error,
        seconds,
    )
    return report


def _resp(port, *words, timeout=5.0):
    """A command's reply from the valkey server at ``port``, as text, or None."""
    import socket

    request = f"*{len(words)}\r\n" + "".join(f"${len(w)}\r\n{w}\r\n" for w in words)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as stream:
            stream.sendall(request.encode())
            data = b""
            while True:
                chunk = stream.recv(65536)
                if not chunk:
                    break
                data += chunk
                head = data.split(b"\r\n", 1)[0]
                if data.endswith(b"\r\n") and (
                    head[:1] in (b"+", b"-", b":")
                    or (head[:1] == b"$" and len(data) >= len(head) + 4 + int(head[1:] or 0))
                ):
                    break
    except OSError:
        return None
    return data.decode(errors="replace")


def valkey(run):
    """valkey-1842: Peeky launches the replica in the background; a primary is started, and
    `ACL LOAD` sent to the replica; `peeky job wait` returns the hit, which reads the
    primary's client's `user` as a null pointer (the program's CLIENT LIST shows it as
    `(superuser)`), before the replica crashes on it."""
    run.build()
    report = run.measure("clang-18")
    code, manifest, seconds = run.describe()
    run.units(manifest, 173, 106)
    acl = SCRATCH / "valkey.acl"
    acl.write_text(
        "user default on nopass ~* &* +@all\n"
        "user foo on #551a821992d8592d71c26a4989e26ce1d39e90ba3c20e3eaf99eed4a2e64251f +@all "
        "~* resetchannels &*\n"
    )
    for name in ("primary", "replica"):
        (SCRATCH / name).mkdir(exist_ok=True)
    server = str(TESTBED / run.binary)
    common = ["--aclfile", str(acl), "--save", "", "--appendonly", "no"]
    primary = subprocess.Popen(
        [server, "--port", "6379", "--dir", str(SCRATCH / "primary"), *common],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    loop = _line("src/acl.c", r"^\s*user \*original = c->user;") + 1
    try:
        started = time.monotonic()
        code, job, _ = run.peeky(
            "probe",
            ".",
            "--binary",
            run.binary,
            "--at",
            f"src/acl.c:{loop}",
            "--capture",
            "original,c.user,c.id",
            "--hits",
            "2",
            "--until",
            "hits",
            "--background",
            program=[
                "--port",
                "6380",
                "--replicaof",
                "127.0.0.1 6379",
                "--dir",
                str(SCRATCH / "replica"),
                *common,
            ],
        )
        linked = None
        for _ in range(120):
            info = _resp(6380, "INFO", "replication") or ""
            if "master_link_status:up" in info:
                linked = round(time.monotonic() - started, 1)
                break
            time.sleep(0.5)
        listed = _resp(6380, "CLIENT", "LIST") or ""
        primary_client = re.search(r"id=(\d+) .*flags=M .*user=(\S+)", listed)
        _resp(6380, "ACL", "LOAD", timeout=5)
        result = {}
        for _ in range(120):
            code, result, _ = run.peeky("job", "wait", job.get("job", ""))
            if code != 3:
                break
        seconds = round(time.monotonic() - started, 1)
    finally:
        primary.terminate()
        primary.wait(timeout=10)
    rows = _rows(result)
    user = rows.get((1, "c.user"), {})
    ident = rows.get((1, "c.id"), {})
    run.probes.append(
        {"at": f"src/acl.c:{loop}", "status": result.get("status"), "seconds": seconds}
    )
    run.check(
        "the replica's probe is a background job: `peeky job wait` returns its hit",
        job.get("status") == "running" and result.get("hits", 0) >= 1 and linked is not None,
        {
            "job": job,
            "linked_after": linked,
            "hits": result.get("hits"),
            "status": result.get("status"),
        },
        seconds,
    )
    run.check(
        "the program's CLIENT LIST shows the primary's client with no user: (superuser)",
        primary_client is not None and primary_client[2] == "(superuser)",
        listed[:600],
    )
    # `original` was set from c->user on the line before the stop: the program's own copy.
    original = rows.get((1, "original"), {})
    run.check(
        "the hit reads the primary's client's user as a null pointer, by its id, as its copy",
        primary_client is not None
        and re.fullmatch(
            r"0x0+", str(user.get("value", "")).split()[0] if user.get("value") else ""
        )
        and original.get("value") == user.get("value")
        and ident.get("value") == primary_client[1],
        {"c.user": user, "c.id": ident, "original": original},
    )
    run.check(
        "the replica then crashes on it, as the bug report says",
        _error(result).get("code") == "PROGRAM_CRASHED" or bool(result.get("crash")),
        {"status": result.get("status"), "error": _error(result), "crash": result.get("crash")},
    )
    main = _line("src/server.c", r"\bint main\(int argc, char \*\*argv\)")
    early = min(
        each for each in manifest.get("breakpoint_lines", {}).get("src/server.c", ()) if each > main
    )
    run.refusals(
        "src/acl.c",
        f"src/server.c:{early}",
        ("--port", "6390", "--save", ""),
        extra=("--until", "hits"),
    )
    return report


IDGEN = """class IDGenerator:
    def __init__(self, max_id: int):
        self._i = 0
        self._mi = max_id

    def get(self):
        try:
            return self._i
        finally:
            self._i += 1
            if self._i > self._mi:
                self._i = 0

id_gen = IDGenerator(10)
print(id_gen.get())
"""


def micropython(run):
    """micropython-13569: py/compile.c's compile_try_finally shows the stack size it
    computes for `get`, as mpy-cross's output of the same script records it (mpy-tool's
    n_state, less its one local), with the nlr_push note below the stop."""
    run.build()
    report = run.measure("clang-18")
    code, manifest, seconds = run.describe()
    detail = run.units(manifest, 218, 215)
    run.check(
        "nlr_push, assembly no unit covers, is named first why any call may return twice",
        _no_uncovered(run, detail).startswith(
            "nlr_push is code no unit's debug information covers"
        ),
        detail["returns_twice"],
    )
    script = SCRATCH / "idgen.py"
    script.write_text(IDGEN)
    compiled = SCRATCH / "idgen.mpy"
    subprocess.run(
        [str(TESTBED / "mpy-cross/build/mpy-cross"), str(script), "-o", str(compiled)],
        check=True,
        capture_output=True,
    )
    dump = subprocess.run(
        [sys.executable, str(TESTBED / "tools/mpy-tool.py"), "-d", str(compiled)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    prelude = re.search(r"simple_name: get\n.*?prelude: \((\d+), ", dump, re.S)
    stack = int(prelude[1]) - 1 if prelude else None
    end = _line("py/compile.c", r"^STATIC void compile_try_finally\(")
    last = _line("py/compile.c", r"^\s*compile_decrease_except_level\(comp\);", end)
    code, result, seconds = run.probe(
        f"py/compile.c:{last}",
        # comp.pass only selects the hit of the emitting pass; its value isn't asserted.
        ["comp.pass", "comp.scope_cur.stack_size"],
        str(script),
        extra=("--hits", "4"),
    )
    rows = _rows(result)
    emitted = [
        rows.get((hit, "comp.scope_cur.stack_size"), {})
        for hit in range(1, 5)
        if rows.get((hit, "comp.pass"), {}).get("value") == "'\\x04'"
    ]
    run.check(
        "compile_try_finally's stack size for `get` is what mpy-cross's output records",
        stack is not None and emitted and emitted[0].get("value") == str(stack),
        {"n_state - 1": stack, "rows": list(rows.values())},
        seconds,
    )
    run.check(
        "the program prints the bug's AttributeError",
        "AttributeError: 'int' object has no attribute '_i'" in result.get("output", ""),
        result.get("output", "")[:400],
    )
    # Checked only for its note: its value, a label number, has no oracle, and isn't asserted.
    first = _line("py/compile.c", r"^\s*uint l_finally_block = comp_next_label\(comp\);", end)
    code, result, seconds = run.probe(f"py/compile.c:{first}", ["l_finally_block"], str(script))
    row = _rows(result).get((1, "l_finally_block"), {})
    note = str(row.get("note") or row.get("error") or "")
    run.check(
        "a local declared at the stop carries the note naming nlr_push",
        "any call above the stop may return twice (nlr_push is code no unit" in note,
        row,
        seconds,
    )
    run.refusals("py/compile.c", f"py/compile.c:{first}", (str(script),))
    return report


# fmt's `#define FMT_HAS_INCLUDE(x) __has_include(x)`: a probe in a macro, whose operand Peeky
# doesn't read (#479). fmt-3272's util.cc and os.cc use it.
FMT_MACRO = (
    "its preprocessing asks whether a file exists (__has_include, in include/fmt/core.h:73), "
    "naming it in a way Peeky doesn't read"
)


def fmt(run, binary_test, total, judged, filtered, unread=()):
    """fmt-3158, fmt-3272: the test units' line tables record no MD5s and name <stdin>, so
    a probe in them, or in include/fmt/'s template code they instantiate, is refused
    saying so. test-main.cc has MD5s: it is judged and read. ``unread``: why the units
    Peeky judges but can't read the text of aren't read."""
    run.build()
    report = run.measure("clang++-18")
    code, manifest, seconds = run.describe()
    detail = run.units(manifest, total, judged, unread)
    test = f"test/{binary_test}.cc"
    reason = "its line table records no MD5s, and names <stdin>"
    run.check(
        f"{test}'s unit isn't judged: no MD5s, <stdin> named",
        str(detail["not judged"].get(test, "")).startswith(reason),
        detail["not judged"],
    )
    first = _line(test, r"^TEST\(") + 1
    code, result, seconds = run.probe(f"{test}:{first}", [], f"--gtest_filter={filtered}")
    error = _error(result)
    run.check(
        f"a probe in {test} is refused, saying why",
        code == 2 and f"{test} is a unit Peeky doesn't judge: {reason}" in error.get("message", ""),
        error,
        seconds,
    )
    header = "include/fmt/ranges.h"
    inside = _line(header, r"^\s*auto it = ctx.begin\(\);|^\s*return out;")
    code, result, seconds = run.probe(f"{header}:{inside}", [], f"--gtest_filter={filtered}")
    error = _error(result)
    run.check(
        f"a probe in {header}'s template code is refused, naming the units and why",
        code == 2
        and "Units Peeky doesn't judge name it in their line tables" in error.get("message", "")
        and reason in error.get("message", ""),
        error,
        seconds,
    )
    main = _line("test/test-main.cc", r"testing::InitGoogleTest\(&argc, argv\);")
    code, result, seconds = run.probe(
        f"test/test-main.cc:{main}", ["argc", "argv[1]"], f"--gtest_filter={filtered}"
    )
    rows = _rows(result)
    argc = str(rows.get((1, "argc"), {}).get("value", ""))
    refused = str(rows.get((1, "argv[1]"), {}).get("error", ""))
    unread = next(
        (
            unit.get("text")
            for unit in manifest.get("units", [])
            if unit["unit"] == "test/test-main.cc"
        ),
        None,
    )
    # argc is the program's arguments' count: its path and the filter. argv is a pointer,
    # which carries no length: Peeky reads no element past what it points at, and says where
    # to index the array instead. The filter is gtest's own print.
    run.check(
        "test-main.cc is read: argc is 2, and argv[1] is refused as a pointer's element",
        unread is None
        and argc == "2"
        and "argv is a pointer, which carries no length" in refused
        and f"Note: Google Test filter = {filtered}" in result.get("output", ""),
        {"rows": list(rows.values()), "text": unread, "output": result.get("output", "")[:300]},
        seconds,
    )
    run.refusals("test/test-main.cc", f"test/test-main.cc:{main}", (f"--gtest_filter={filtered}",))
    return report


def json_(run):
    """json-4237: test-udt_cpp11, filtered: both units' tables record no MD5s and name
    <stdin>, so a probe in to_json.hpp's overload for enums is refused, saying so."""
    run.build()
    report = run.measure("clang++-18")
    code, manifest, seconds = run.describe()
    error = _error(manifest)
    reason = "its line table records no MD5s, and names <stdin>"
    # describe refuses the binary whole: its 2 units are counted from measure.py's read.
    run.summary = {"units": report["count"]["units"], "judged": 0, "not judged": report["summary"]}
    run.check(
        "neither of the 2 units is judged: describe refuses, saying why",
        code == 2
        and error.get("code") == "UNSUPPORTED"
        and reason in error.get("message", "")
        and report["count"]["units"] == 2
        and "judged" not in report["summary"],
        {"error": error, "units": report["count"], "summary": report["summary"]},
        seconds,
    )
    header = "include/nlohmann/detail/conversions/to_json.hpp"
    enum = _line(header, r"enable_if_t<std::is_enum<EnumType>::value, int> = 0>")
    body = _line(header, r"external_constructor<value_t::number_integer>::construct", enum)
    code, result, seconds = run.probe(f"{header}:{body}", ["e"], "-tc=basic usage")
    error = _error(result)
    run.check(
        "a probe in to_json.hpp's overload for enums is refused, saying why",
        code == 2
        and error.get("code") == "UNSUPPORTED"
        and "Peeky judges no unit of" in error.get("message", "")
        and reason in error.get("message", ""),
        error,
        seconds,
    )
    run.refusals(header, f"{header}:{body}", ("-tc=basic usage",), none_judged=True)
    return report


PROJECTS = {
    "jq": jq,
    "valkey": valkey,
    "micropython": micropython,
    "fmt3158": lambda run: fmt(run, "ranges-test", 5, 1, "ranges_test.format_map"),
    "fmt3272": lambda run: fmt(run, "xchar-test", 6, 3, "xchar_test.format", (FMT_MACRO,) * 2),
    "json": json_,
}


def main(project):
    run = Acceptance(project)
    try:
        measured = PROJECTS[project](run)
    except Exception as exc:
        run.check("the acceptance ran to its end", False, f"{type(exc).__name__}: {exc}")
        measured = None
    report = run.report()
    if measured is not None:
        report["symbols"] = {
            key: measured[key]
            for key in ("line_tables", "debug_information", "bounds", "count", "summary")
        }
    json.dump(report, sys.stdout, indent=1)
    print()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
