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
