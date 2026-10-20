"""The agent guide is read whole: agents' file viewers commonly return at most 20 KB."""

from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_the_agent_guide_fits_one_read():
    # Eval 4 (C): the 20.4 KB guide came back truncated on the first read.
    assert len((DOCS / "AGENT_GUIDE.md").read_bytes()) < 20 * 1024


def test_the_guide_links_resolve():
    guide = (DOCS / "AGENT_GUIDE.md").read_text()
    for name in ("ERRORS.md", "REMOTE.md", "RUST_AGENT.md#reading-rust-values"):
        assert f"]({name})" in guide
        assert (DOCS / name.split("#")[0]).is_file()
    assert "## Reading Rust values" in (DOCS / "RUST_AGENT.md").read_text()


def test_the_docs_say_reading_any_global_lists_and_reprs_them_all(capsys):
    # Review of #104 (round 2): reading one global, or a misspelled name, lists every
    # global and runs all their reprs; the docs said only "its __repr__".
    import pytest

    from peeky.cli import main

    with pytest.raises(SystemExit):
        main(["session", "inspect", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Listing them runs every global's repr" in help_text
    assert "a misspelled one included" in help_text
    guide = (DOCS / "AGENT_GUIDE.md").read_text()
    assert "one, or a misspelled name, lists all, running all reprs" in guide
    cookbook = " ".join((DOCS / "COOKBOOK.md").read_text().split())
    assert "Then all of them are listed and every repr runs" in cookbook
    assert "a `for` loop's counter while its body calls, on one line or several" in cookbook
