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


def _slugs(headings):
    """GitHub's anchors for a page's headings, in order, as github-slugger makes them.

    Backticks and other punctuation are dropped, spaces become hyphens, and a slug
    already taken gets the next free `-1`, `-2` suffix.
    """
    seen = {}
    for heading in headings:
        text = re.sub(r"\s+#+\s*$", "", heading.strip())
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        base = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        slug = base
        while slug in seen:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        seen[slug] = 0
        yield slug


LANGS = ("python", "java", "c-cpp", "rust")


def _without_fences(text):
    return re.sub(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", "", text, flags=re.M | re.S)


def _headings(path):
    return re.findall(r"^ {0,3}#{1,6} (.+)$", _without_fences(path.read_text()), flags=re.M)


def _broken_links(pages):
    """Each relative link, outside code, whose file or anchor does not exist."""
    anchors = {}
    broken = []
    checked = 0
    for page in pages:
        text = re.sub(r"`[^`\n]*`", "", _without_fences(page.read_text()))
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"[a-z]+:", target):
                continue
            checked += 1
            path, _, anchor = target.partition("#")
            linked = (page.parent / path).resolve() if path else page.resolve()
            if not linked.exists():
                broken.append((page.name, target))
            elif anchor and linked.suffix == ".md":
                if linked not in anchors:
                    anchors[linked] = set(_slugs(_headings(linked)))
                if anchor not in anchors[linked]:
                    broken.append((page.name, target))
    return broken, checked


def test_every_relative_link_in_the_docs_resolves():
    # M7: each language page points into the cookbooks by section; #207 moved sections
    # between files. Every relative link, and its anchor, must land.
    broken, checked = _broken_links(sorted(DOCS.rglob("*.md")))
    assert not broken
    assert checked > 300


def test_anchors_are_githubs_slugs():
    # Review of #216: a set of slugs missed GitHub's suffixes for repeated headings
    # (TESTING.md's "C investigation…" and "C++ investigation…" share one slug).
    headings = [
        "What `set` Checks",
        "7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)",
        "11. From pdb / gdb / lldb to Peeky",
        "Strings with a NUL Byte (C++)",
        "snake_case and [a link](x.md) ##",
        "C investigation slice (2026-10-14)",
        "C++ investigation slice (2026-10-14)",
        "C investigation slice (2026-10-14)",
        "a",
        "a",
        "a-1",
    ]
    assert list(_slugs(headings)) == [
        "what-set-checks",
        "74-java-tests-already-running-gradle---debug-jvm-eg-venice",
        "11-from-pdb--gdb--lldb-to-peeky",
        "strings-with-a-nul-byte-c",
        "snake_case-and-a-link",
        "c-investigation-slice-2026-10-14",
        "c-investigation-slice-2026-10-14-1",
        "c-investigation-slice-2026-10-14-2",
        "a",
        "a-1",
        "a-1-1",
    ]
    testing = list(_slugs(_headings(DOCS / "TESTING.md")))
    assert "c-investigation-slice-2026-10-14" in testing
    assert "c-investigation-slice-2026-10-14-1" in testing


def test_the_link_check_finds_a_missing_anchor_or_file(tmp_path):
    page = tmp_path / "a.md"
    page.write_text(
        "# Setup\n\n## C slice (1)\n\n## C++ slice (1)\n\n```sh\n# not a heading\n```\n\n"
        "[ok](#setup) [ok](#c-slice-1-1) [bad](#c-slice-1-2) [bad](#not-a-heading) "
        "[bad](b.md) [ok](a.md#c-slice-1) [web](https://example.com/#x) `[code](#nowhere)`\n"
    )
    broken, checked = _broken_links([page])
    assert checked == 6
    assert broken == [("a.md", "#c-slice-1-2"), ("a.md", "#not-a-heading"), ("a.md", "b.md")]


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
    assert "Reading one, or a misspelled name, lists all globals, running all reprs" in " ".join(
        guide.split()
    )
    python = " ".join((DOCS / "lang/python-cookbook.md").read_text().split())
    assert "Then all of them are listed and every repr runs" in python
    c_cpp = " ".join((DOCS / "lang/c-cpp-cookbook.md").read_text().split())
    assert "a `for` or `while` loop's variable, as in `for (int i = 0; …)`, is withheld" in c_cpp
    assert "too while the body calls, on one line or several" in c_cpp


def test_errors_md_lists_every_code_with_the_retryable_flag_peeky_sends():
    # #212: ERRORS.md's code table has a Retryable column; it must say what Peeky sends.
    from peeky.errors import _RULES

    table = (DOCS / "ERRORS.md").read_text()
    rows = dict(re.findall(r"^\| `([A-Z_]+)`[^|]*\|[^|]*\|[^|]*\| (yes|no) \|$", table, flags=re.M))
    expected = {}
    for _, code, _, retryable in _RULES:
        expected.setdefault(code, set()).add(retryable)
    # Sent outside the rules: a build's failure, a probe's no-stop, the worker's token check,
    # and the fallbacks for an unexpected exception.
    for code in ("BUILD_FAILED", "NO_STOP", "UNAUTHORIZED", "INTERNAL_ERROR", "DEBUGGER_ERROR"):
        expected.setdefault(code, set()).add(False)
    assert set(rows) == set(expected)
    for code, flags in expected.items():
        assert len(flags) == 1, code
        assert rows[code] == ("yes" if flags.pop() else "no"), code
