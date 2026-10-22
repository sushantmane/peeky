"""The agent guide is read whole: agents' file viewers commonly return at most 20 KB."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_the_agent_guide_fits_one_read():
    # Eval 4 (C): the 20.4 KB guide came back truncated on the first read.
    assert len((DOCS / "AGENT_GUIDE.md").read_bytes()) < 20 * 1024


def test_the_guide_links_resolve():
    # Eighth evaluation, L23: agents given the guide, COOKBOOK and ERRORS could not
    # follow its pointer into RUST_AGENT.md; M7: it links a page per language.
    guide = (DOCS / "AGENT_GUIDE.md").read_text()
    names = (
        "ERRORS.md",
        "REMOTE.md",
        *(f"lang/{n}.md" for n in ("python", "java", "c-cpp", "rust")),
    )
    for name in names:
        assert f"]({name})" in guide
        assert (DOCS / name).is_file()
    assert "RUST_AGENT.md" not in guide


def _anchor(heading):
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def test_the_language_pages_links_resolve():
    # M7: each page points into the COOKBOOK by section; every link must land.
    for page in sorted((DOCS / "lang").glob("*.md")):
        for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text()):
            path, _, anchor = target.partition("#")
            linked = (page.parent / path).resolve()
            assert linked.is_file(), (page.name, target)
            if anchor:
                headings = re.findall(r"^#+ (.+)$", linked.read_text(), flags=re.M)
                assert anchor in {_anchor(h) for h in headings}, (page.name, target)


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
    assert (
        "A `for` or `while` loop's own variable (`for (int i = 0; …)`) is withheld too while the body calls, on one line or several"
        in cookbook
    )
