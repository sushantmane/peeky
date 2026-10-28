"""The CLI on a C++ directory: its one program is found, and members read without this."""

import json
import os

import pytest
from native_compilers import cxx_is_gcc

from peeky.cli import main

RING = """#include <cstdio>
#include <cstdlib>
#include <vector>

class Ring {
public:
    explicit Ring(int capacity) : slots_(capacity), head_(0), size_(0) {}
    void push(int value) {
        int tail = (head_ + size_) % capacity();
        slots_[tail] = value;
        if (size_ < capacity()) {
            ++size_;
        }
    }
    int capacity() const { return static_cast<int>(slots_.size()); }

private:
    std::vector<int> slots_;
    int head_;
    int size_;
};

int main(int argc, char** argv) {
    Ring ring(argc > 1 ? std::atoi(argv[1]) : 3);
    for (int i = 2; i < argc; ++i) {
        ring.push(std::atoi(argv[i]));
    }
    std::printf("DONE\\n");
    return 0;
}
"""


@pytest.fixture
def ring(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP") or not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_DAP and PEEKY_NATIVE_CXX to enable C++ conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "ring"
    root.mkdir()
    (root / "ring.cpp").write_text(RING)
    (root / "check.py").write_text("print('an independent checker beside the program')\n")
    return root


@pytest.mark.timeout(120)
def test_a_cpp_directory_probes_its_program_and_members_are_read_through_this(ring, capsys):
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "10",
            "--capture",
            "tail,this.head_,this.size_,this.slots_[0]",
            "--hits",
            "2",
            "--json",
            "--no-save",
            "--quiet",
            "--",
            "3",
            "7",
            "8",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    values = [(row["hit"], row["selector"], row.get("value")) for row in result["rows"]]
    assert (1, "this.head_", "0") in values and (1, "this.size_", "0") in values
    assert (2, "this.size_", "1") in values and (2, "this.slots_[0]", "7") in values
    assert all(row["function"].startswith("Ring::push") for row in result["rows"])
    # A bare member name is never read in place of a local; the error names the selector.
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "10",
            "--capture",
            "size_,missing_",
            "--json",
            "--no-save",
            "--quiet",
            "--",
            "3",
            "7",
        ]
    )
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    assert "value" not in rows["size_"]
    assert "select it as this.size_ if the code means the member" in rows["size_"]["error"]
    assert "member" not in rows["missing_"]["error"]
    try:
        assert main(["session", "start", str(ring), "--at", "10", "--", "3", "7"]) == 0
        assert " in Ring::push(int)" in capsys.readouterr().out
        assert main(["session", "set", "size_=2"]) != 0
        error = capsys.readouterr().err
        assert "it can be read as this.size_" in error
        assert "only the stopped function's own locals can be changed" in error
    finally:
        main(["session", "close"])


STATIC = """#include <cstdio>

struct Acc {
    int calls = 7;
    int run() {
        static int calls = 100;
        calls += 1;
        std::printf("static calls=%d\\n", calls);
        return calls;
    }
};

int main() {
    Acc acc;
    return acc.run() > 0 ? 0 : 1;
}
"""


@pytest.mark.timeout(120)
def test_a_static_that_shadows_a_member_is_never_read_as_the_member(ring, capsys):
    # lldb-dap lists function-local statics outside the Locals scope: `calls` is not a
    # visible local here, yet the code means the static, not this->calls. #364: the static
    # is read.
    (ring / "ring.cpp").write_text(STATIC)
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "8",
            "--capture",
            "calls,this.calls",
            "--json",
            "--no-save",
            "--quiet",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    rows = {row["selector"]: row for row in result["rows"]}
    assert rows["calls"]["value"] == "101", rows["calls"]
    assert rows["this.calls"]["value"] == "7"
    assert "static calls=101" in result["output"]


WIDE = """#include <cstddef>
#include <cstdio>

int main() {
    std::size_t count = 3;
    long total = 5000000000L;
    std::printf("COUNT:%zu TOTAL:%ld\\n", count, total);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_cpp_size_t_and_long_locals_can_be_changed_within_their_range(ring, capsys):
    (ring / "ring.cpp").write_text(WIDE)
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "7",
            "--capture",
            "count,total",
            "--set",
            "count=18446744073709551615",
            "--set",
            "total=-9000000000",
            "--json",
            "--no-save",
            "--quiet",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert "COUNT:18446744073709551615 TOTAL:-9000000000" in result["output"]
    code = main(
        ["probe", str(ring), "--at", "7", "--set", "count=-1", "--json", "--no-save", "--quiet"]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["error"]["code"] == "MUTATION_REJECTED"
    try:
        assert main(["session", "start", str(ring), "--at", "7"]) == 0
        assert main(["session", "inspect"]) == 0
        text = capsys.readouterr().out
        assert "count: " in text and "[settable]" in text.split("count: ", 1)[1].splitlines()[0]
    finally:
        main(["session", "close"])


BITS = """#include <cstdio>

struct Bits {
    int lo : 3;
    unsigned short hi : 5;
};

int main() {
    typedef float int64_t;
    int64_t lossy = 1;
    Bits bits{1, 2};
    auto [lo, hi] = bits;
    int plain = 7;
    std::printf("BITS:%d %d %d %.0f\\n", lo, hi, plain, lossy);
    return 0;
}
"""


@pytest.mark.timeout(120)
def test_writes_that_cannot_reach_the_program_are_never_reported_as_done(ring, capsys):
    (ring / "ring.cpp").write_text(BITS)
    try:
        assert main(["session", "start", str(ring), "--at", "14"]) == 0
        capsys.readouterr()
        assert main(["session", "inspect", "--json"]) == 0
        names = {
            v["name"] for s in json.loads(capsys.readouterr().out)["scopes"] for v in s["variables"]
        }
        if "hi" in names:  # clang 19+ describes structured bindings of bitfields
            # A computed value: refused (or unchanged), never "set hi: 2 -> 2".
            assert main(["session", "set", "hi=17"]) != 0
            error = capsys.readouterr().err
            said = ["refused", "did not"]
            if cxx_is_gcc():
                # GCC gives it no location (#167): neither read nor changed, and said so.
                said.append("neither read nor change it")
            assert "MUTATION_REJECTED" in error and any(text in error for text in said), error
        # The session is still usable, and a stored local takes a canonical literal.
        assert main(["session", "set", "plain=+5"]) == 0
        assert "set plain: 7 -> 5" in capsys.readouterr().out
        # A typedef that redefines int64_t as float: the read-back differs; fail closed.
        assert main(["session", "set", "lossy=16777217"]) != 0
        error = capsys.readouterr().err
        assert "stored 16777216 in 'lossy', not 16777217" in error, error
    finally:
        main(["session", "close"])
