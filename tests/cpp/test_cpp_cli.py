"""The CLI on a C++ directory: its one program is found, and members read without this."""

import json
import os

import pytest

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
def test_a_cpp_directory_probes_its_program_and_reads_members_without_this(ring, capsys):
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "10",
            "--capture",
            "tail,head_,size_,this.size_,slots_[0]",
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
    assert (1, "head_", "0") in values and (1, "size_", "0") in values
    assert (2, "size_", "1") in values and (2, "this.size_", "1") in values
    assert (2, "slots_[0]", "7") in values
    assert all(row["function"].startswith("Ring::push") for row in result["rows"])
    code = main(
        [
            "probe",
            str(ring),
            "--at",
            "10",
            "--capture",
            "missing_",
            "--json",
            "--no-save",
            "--quiet",
            "--",
            "3",
            "7",
        ]
    )
    [row] = json.loads(capsys.readouterr().out)["rows"]
    assert "not a member of this either" in row["error"] and "size_" in row["error"]
