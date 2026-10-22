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
    # #207: the first five minutes send each language to its page, which links its cookbook.
    first = guide.split("## First 5 minutes")[1].split("\n## ")[0]
    for lang in ("python", "java", "c-cpp", "rust"):
        assert f"](lang/{lang}.md)" in first, lang


def _anchor(heading):
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


LANGS = ("python", "java", "c-cpp", "rust")


def _without_fences(text):
    return re.sub(r"^\s*```.*?^\s*```[^\n]*$", "", text, flags=re.M | re.S)


def _headings(path):
    return re.findall(r"^#+ (.+)$", _without_fences(path.read_text()), flags=re.M)


def test_every_relative_link_in_the_docs_resolves():
    # M7: each language page points into the cookbooks by section; #207 moved sections
    # between files. Every relative link, and its anchor, must land.
    anchors = {}
    checked = 0
    for page in sorted(DOCS.rglob("*.md")):
        text = re.sub(r"`[^`\n]*`", "", _without_fences(page.read_text()))
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"[a-z]+:", target):
                continue
            path, _, anchor = target.partition("#")
            linked = (page.parent / path).resolve() if path else page
            assert linked.exists(), (str(page.relative_to(DOCS)), target)
            if anchor and linked.suffix == ".md":
                if linked not in anchors:
                    anchors[linked] = {_anchor(h) for h in _headings(linked)}
                assert anchor in anchors[linked], (str(page.relative_to(DOCS)), target)
            checked += 1
    assert checked > 300


def test_each_language_has_a_page_and_a_cookbook_numbered_as_the_cookbook():
    # #207: a language's own recipes are in docs/lang/<lang>-cookbook.md, each section under
    # the number and title of the COOKBOOK section it adds to; the page links it.
    cookbook = set(_headings(DOCS / "COOKBOOK.md"))
    for lang in LANGS:
        assert f"]({lang}-cookbook.md)" in (DOCS / f"lang/{lang}.md").read_text()
        sections = [h for h in _headings(DOCS / f"lang/{lang}-cookbook.md") if h[0].isdigit()]
        assert sections, lang
        for heading in sections:
            assert heading in cookbook, (lang, heading)


def test_every_part_of_the_cookbooks_fits_one_read():
    # #207: the 181 KB COOKBOOK could not be read in pieces. Each part between two headings,
    # and each language's summary page whole, fits one 20 KB read.
    for path in [DOCS / "COOKBOOK.md", *sorted((DOCS / "lang").glob("*.md"))]:
        parts = re.split(r"^#+ ", path.read_text(), flags=re.M)
        assert max(len(part.encode()) for part in parts) < 20 * 1024, path.name
    for lang in LANGS:
        assert len((DOCS / f"lang/{lang}.md").read_bytes()) < 20 * 1024, lang


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
    python = " ".join((DOCS / "lang/python-cookbook.md").read_text().split())
    assert "Then all of them are listed and every repr runs" in python
    c_cpp = " ".join((DOCS / "lang/c-cpp-cookbook.md").read_text().split())
    assert "a `for` loop's counter while its body calls, on one line or several" in c_cpp
