"""Test-only escape hatch for surveying C++ with GCC (#167, part 2). With
PEEKY_TEST_CPP_GCC=1, a C++ target whose compiler is GCC builds instead of being refused,
and without clang's `--driver-mode=g++`. tests/conftest.py applies it in the test process;
the Popen wrapper below applies it in the session workers and probe children that process
starts. Only the test harness reads the variable: Peeky itself never does, so no user can
reach it. Never set in CI: the C++ rules are not checked against GCC's debug information."""

import os
import subprocess
import sys
from pathlib import Path

HERE = str(Path(__file__).resolve().parent)
DRIVER = "--driver-mode=g++"
# Off while a test checks the refusal itself (test_cpp_gcc).
ENABLED = True
_applied = False


def wanted():
    return os.environ.get("PEEKY_TEST_CPP_GCC") == "1"


PREFIX = (
    f"import sys; sys.path.insert(0, {HERE!r}); import cpp_gcc_survey; cpp_gcc_survey.apply(); "
)
ISOLATED = "import sys; sys.path.insert(0, sys.argv.pop(1)); "


def _rewrite(args):
    if not isinstance(args, (list, tuple)) or len(args) < 3 or args[0] != sys.executable:
        return args
    args = list(args)
    if args[1:3] == ["-m", "peeky.worker"]:
        run = "import runpy; runpy.run_module('peeky.worker', run_name='__main__', alter_sys=True)"
        return [args[0], "-c", PREFIX + run, *args[3:]]
    if "-c" in args:
        index = args.index("-c") + 1
        code = args[index]
        if "peeky.cli" in code and "cpp_gcc_survey" not in code:
            # After the isolated bootstrap puts this Peeky on the path.
            head = ISOLATED if code.startswith(ISOLATED) else ""
            args[index] = head + PREFIX + code[len(head) :]
    return args


class _Popen(subprocess.Popen):
    def __init__(self, args, *rest, **options):
        super().__init__(_rewrite(args), *rest, **options)


def apply():
    global _applied
    if _applied or not wanted():
        return
    _applied = True
    from peeky import native

    family = native.compiler_family
    original = native.NativeTarget._compile_flags

    def as_clang(path):
        said = family(path)
        return "clang" if said == "gcc" and ENABLED else said

    def compile_flags(self):
        driver, flags, sysroot, sdk = original(self)
        if ENABLED and self.language == "cpp" and family(native._tool(self.compiler)) == "gcc":
            driver = tuple(flag for flag in driver if flag != DRIVER)
            flags = [flag for flag in flags if flag != DRIVER]
        return driver, flags, sysroot, sdk

    native.compiler_family = as_clang
    native.NativeTarget._compile_flags = compile_flags
    subprocess.Popen = _Popen
