"""A C++ program a little bigger than the evaluations' (#397): eight files using the STL
(examples/large/cpp-shop) have 2-4 MB of line tables and 19-45 MB of debug information,
as llvm-dwarfdump writes them. Peeky reads both as they are written, so the program is
neither refused nor read without its checks."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from peeky.cli import main

ROOT = Path(__file__).resolve().parents[2]

# A ninth file, which checks quotes against rules a config line names: std::regex and
# std::function take its line tables past 3 MB.
AUDIT = """#include "common.hpp"
#include <algorithm>
#include <functional>
#include <regex>
#include <sstream>
#include <unordered_map>

namespace shop {

std::string audit(const std::vector<Quote> &quotes, const std::string &config) {
    std::unordered_map<std::string, std::function<bool(const Quote &, Cents)>> rules{
        {"max", [](const Quote &q, Cents limit) { return q.total() <= limit; }},
        {"min", [](const Quote &q, Cents limit) { return q.total() >= limit; }},
    };
    std::regex rule(R"((\\w+)=(\\d+))");
    std::vector<std::pair<std::string, Cents>> checks;
    for (auto it = std::sregex_iterator(config.begin(), config.end(), rule);
         it != std::sregex_iterator(); ++it)
        checks.emplace_back((*it)[1].str(), std::stoll((*it)[2].str()));
    std::sort(checks.begin(), checks.end());
    std::ostringstream out;
    for (const auto &quote : quotes)
        for (const auto &[name, limit] : checks)
            if (auto found = rules.find(name); found != rules.end() && !found->second(quote, limit))
                out << "order " << quote.order_id << " fails " << name << '\\n';
    return out.str();
}

}  // namespace shop
"""


@pytest.fixture
def shop(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "shop"
    shutil.copytree(ROOT / "examples/large/cpp-shop", root)
    return root


def line_of(path, marker):
    return next(i for i, text in enumerate(path.read_text().splitlines(), 1) if marker in text)


def run(capsys, *arguments):
    code = main([*arguments, "--json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(300)
def test_a_local_of_an_stl_heavy_program_is_judged_and_can_be_changed(shop, capsys):
    # Its debug information is 19-33 MB of text, over the 16 MiB Peeky once read: every
    # local was "not judged", and no value could be changed in the program.
    line = line_of(shop / "scheduler.cpp", "plan.push_back")
    code, result = run(
        capsys,
        "probe",
        str(shop / "main.cpp"),
        "--at",
        f"scheduler.cpp:{line}",
        "--capture",
        "day,today,next.order_id",
        "--set",
        "day=5",
        "--quiet",
        "--no-save",
    )
    assert code == 0, result.get("error")
    unread = "could not read the build's debug information"
    assert not any(unread in warning for warning in result["warnings"]), result["warnings"]
    rows = [(row["selector"], row["value"], row.get("note")) for row in result["rows"]]
    assert rows[:3] == [("day", "0", None), ("today", "0", None), ("next.order_id", "1002", None)]
    assert result["set_outcomes"][0]["ended"] == "made"
    assert "day 5: 3" in result["output"] and "day 6: 3" in result["output"]


@pytest.mark.timeout(300)
def test_describe_lists_the_functions_of_an_stl_heavy_program(shop, capsys):
    code, result = run(capsys, "describe", str(shop / "main.cpp"))
    assert code == 0, result
    assert "scopes_unavailable" not in result, result["scopes_unavailable"]
    (plan,) = [scope for scope in result["scopes"] if scope["scope"] == "shop::Scheduler::plan"]
    assert {"day", "today", "plan", "next"} <= set(plan["names"]), plan


@pytest.mark.timeout(300)
def test_a_program_with_over_2_mib_of_line_tables_is_not_refused(shop, capsys):
    # With std::regex in a ninth file its line tables are 3.2-3.7 MB of text: refused with
    # BUDGET_EXHAUSTED "Native symbol metadata exceeds limit" at the old 2 MiB.
    (shop / "audit.cpp").write_text(AUDIT)
    line = line_of(shop / "pricing.cpp", "OBSERVE")
    code, result = run(
        capsys,
        "probe",
        str(shop / "main.cpp"),
        "--at",
        f"pricing.cpp:{line}",
        "--capture",
        "tier_discount,coupon_discount,quote.subtotal",
        "--quiet",
        "--no-save",
    )
    assert code == 0, result.get("error")
    assert "audit.cpp" in result["target_identity"]["sources"]
    rows = [(row["selector"], row["value"], row.get("note")) for row in result["rows"]]
    assert rows == [
        ("tier_discount", "648", None),
        ("coupon_discount", "0", None),
        ("quote.subtotal", "16200", None),
    ]


SMALL = """#include <cstdio>

int main() {
    int total = 0;
    for (int i = 0; i < 3; i++) total += i;
    std::printf("%d\\n", total); /* READ */
    return 0;
}
"""


@pytest.fixture
def small(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "small.cpp"
    source.write_text(SMALL)
    return source


def probe_small(capsys, source):
    return run(
        capsys,
        "probe",
        str(source),
        "--at",
        str(line_of(source, "READ")),
        "--capture",
        "total",
        "--quiet",
        "--no-save",
    )


@pytest.mark.timeout(240)
def test_line_tables_over_peekys_limit_are_a_budget_refusal_that_says_what_to_do(
    small, capsys, monkeypatch
):
    monkeypatch.setattr("peeky.native.MAX_LINE_TEXT", 100)
    code, result = probe_small(capsys, small)
    assert code == 2 and result["error"]["code"] == "BUDGET_EXHAUSTED", result
    assert result["error"]["message"] == (
        "Peeky reads at most 100 bytes of a build's line tables, and this build has more: "
        "probe a smaller program (fewer source files, standard headers or std collections)."
    )
    assert result["error"]["hint"].startswith("Probe a smaller program")


@pytest.mark.timeout(240)
def test_debug_information_over_peekys_limit_leaves_locals_unjudged_and_says_why(
    small, capsys, monkeypatch
):
    monkeypatch.setattr("peeky.native.MAX_DIES", 5)
    code, result = probe_small(capsys, small)
    assert code == 0, result.get("error")
    (row,) = result["rows"]
    assert row["value"] == "3"
    assert row["note"] == (
        "Peeky does not judge whether this local is initialized here: it could not read the "
        "build's debug information (over Peeky's limit of 5 entries; probe a smaller program), "
        "so it may hold leftovers"
    )


def stand_in(tmp_path, monkeypatch, mode):
    """PEEKY_NATIVE_DWARFDUMP as a script that runs the real tool, but for the whole debug
    information (`--debug-info`), which it never gives: it ``mode``s (sleeps or fails)."""
    real = os.environ.get("PEEKY_NATIVE_DWARFDUMP") or shutil.which("llvm-dwarfdump")
    if not real:
        pytest.skip("set PEEKY_NATIVE_DWARFDUMP to enable native conformance")
    script = tmp_path / "tools" / "llvm-dwarfdump"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if '--debug-info' in sys.argv[1:]:\n"
        f"    if {mode!r} == 'sleep':\n"
        "        time.sleep(60)\n"
        "    sys.stderr.write('llvm-dwarfdump: error: no debug information\\n')\n"
        "    sys.exit(3)\n"
        f"os.execv({str(shutil.which(real) or real)!r}, [{real!r}, *sys.argv[1:]])\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PEEKY_NATIVE_DWARFDUMP", str(script))


@pytest.mark.timeout(240)
def test_reading_debug_information_out_of_time_fails_the_launch(small, capsys, monkeypatch):
    # Only a build over Peeky's bounds is read without its debug information; a read the
    # time ran out on is a TIMEOUT (it was taken for unreadable debug information).
    stand_in(small.parent, monkeypatch, "sleep")
    monkeypatch.setattr("peeky.native._remaining", lambda deadline, limit=15: 2)
    code, result = probe_small(capsys, small)
    assert code != 0 and result["error"]["code"] == "TIMEOUT", result
    assert result["error"]["message"].startswith(
        "Reading the build's debug information timed out after 2.0 s: probe a smaller program"
    )


@pytest.mark.timeout(240)
def test_a_failing_dwarfdump_fails_the_launch(small, capsys, monkeypatch):
    stand_in(small.parent, monkeypatch, "fail")
    code, result = probe_small(capsys, small)
    assert code != 0 and result["error"]["code"] == "DEBUGGER_ERROR", result
    assert (
        "exit=3, diagnostics=llvm-dwarfdump: error: no debug information"
        in (result["error"]["message"])
    )
