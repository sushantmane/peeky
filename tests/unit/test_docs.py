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


def _contents(path):
    """A page's contents list, as (text, anchor) links in order, and its `##`/`###`
    headings other than the list's own, as (text, anchor) pairs."""
    text = _without_fences(path.read_text())
    listed = re.search(r"^## (?:Table of )?Contents\n(.*?)^## ", text, flags=re.M | re.S)
    links = re.findall(r"\[((?:[^\]`]|`[^`]*`)+)\]\(#([^)]+)\)", listed.group(1))
    headings = _headings(path)
    levels = re.findall(r"^ {0,3}(#{1,6}) .+$", text, flags=re.M)
    shown = [
        (heading, slug)
        for heading, slug, level in zip(headings, _slugs(headings), levels)
        if level in ("##", "###") and not re.match(r"(Table of )?Contents$", heading)
    ]
    return links, shown


def test_each_contents_list_matches_the_headings():
    # #212: the rewrites renamed and added headings; COOKBOOK's table of contents and each
    # language cookbook's list name every `##` and `###` heading, in order, by its anchor.
    for path in [DOCS / "COOKBOOK.md", *sorted((DOCS / "lang").glob("*-cookbook.md"))]:
        links, shown = _contents(path)
        assert [anchor for _, anchor in links] == [slug for _, slug in shown], path.name
        for (text, _), (heading, _) in zip(links, shown):
            assert re.sub(r"^\d+\. ", "", heading) == text, (path.name, heading)
        # No two headings share a slug, so no anchor needs GitHub's `-1` suffix.
        bases = [next(_slugs([heading])) for heading in _headings(path)]
        assert len(set(bases)) == len(bases), path.name


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
    # Sent outside the rules: a build's failure, a probe's no-stop or crash, the worker's token check,
    # and the fallbacks for an unexpected exception.
    for code in (
        "BUILD_FAILED",
        "NO_STOP",
        "PROGRAM_CRASHED",
        "UNAUTHORIZED",
        "INTERNAL_ERROR",
        "DEBUGGER_ERROR",
    ):
        expected.setdefault(code, set()).add(False)
    assert set(rows) == set(expected)
    for code, flags in expected.items():
        assert len(flags) == 1, code
        assert rows[code] == ("yes" if flags.pop() else "no"), code


def test_the_docs_peeky_prints_exist():
    # #212: a printed hint names a doc by path and anchor (`docs/lang/rust-cookbook.md#…`), by a
    # bare name under docs/ (`see ERRORS.md`), or a cookbook section by number (`rust-cookbook
    # 6.6`, `COOKBOOK 6.6`); each must resolve.
    source = "\n".join(
        path.read_text() for path in sorted((DOCS.parent / "src/peeky").rglob("*.py"))
    )
    paths = re.findall(r"(?<![\w/.-])([\w/.-]+\.md)(?:#([\w-]+))?", source)
    assert {"docs/lang/rust-cookbook.md", "ERRORS.md"} <= {path for path, _ in paths}
    for path, anchor in paths:
        page = DOCS.parent / path if path.startswith("docs/") else DOCS / path
        assert page.is_file(), path
        assert not anchor or anchor in set(_slugs(_headings(page))), (path, anchor)
    sections = re.findall(r"\b((?:python|java|c-cpp|rust)-cookbook|COOKBOOK) (\d+\.\d+)\b", source)
    assert sections
    for page, number in sections:
        path = DOCS / ("COOKBOOK.md" if page == "COOKBOOK" else f"lang/{page}.md")
        assert any(h.startswith(number + " ") for h in _headings(path)), (page, number)


# #212: the docs are written in short sentences. A sentence here is prose outside code blocks,
# tables, headings and quotes, split at a full stop, colon or semicolon and at each list item;
# inline code counts as one word. History records (PROGRESS, evaluations, reviews, ADRs, and
# pages marked historical) are kept as written.
LONGEST_SENTENCE = 40


def _sentences(text):
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    for block in re.split(r"\n\s*\n", text):
        if block.lstrip().startswith(("|", "#", ">", "<")):
            continue
        prose = " ".join(line.strip() for line in block.splitlines())
        prose = re.sub(r"`[^`]*`", "X", prose)
        prose = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", prose)
        for item in re.split(r"(?:^|\s)(?:[-*]|\d+\.)\s", prose):
            for sentence in re.split(r"(?<=[.!?:;])\s+", item):
                if sentence.split():
                    yield sentence


def _long_sentences(paths):
    return [
        (path.name, len(sentence.split()), sentence[:80])
        for path in paths
        for sentence in _sentences(path.read_text())
        if len(sentence.split()) > LONGEST_SENTENCE
    ]


def test_the_docs_are_written_in_short_sentences():
    pages = [DOCS.parent / "README.md", DOCS.parent / "SECURITY.md", *sorted(DOCS.glob("*.md"))]
    pages += sorted(DOCS.glob("lang/*.md"))
    pages = [
        page
        for page in pages
        if page.name != "PROGRESS.md" and "> **Historical:**" not in page.read_text()[:2000]
    ]
    assert len(pages) > 20
    assert _long_sentences(pages) == []


def test_the_sentence_check_finds_a_long_sentence(tmp_path):
    words = " ".join(["word"] * LONGEST_SENTENCE)
    page = tmp_path / "a.md"
    page.write_text(
        f"# {words} heading\n\n{words}. Short one.\n\n- {words} more\n- ok\n\n"
        f"| {words} more |\n\n```\n{words} more\n```\n\nUse `{words}` here, {words}.\n"
    )
    # The list item (41 words) and the last line (43, with `…` as one word) are too long; the
    # 40-word sentence, the heading, the table and the code block are not checked or pass.
    found = _long_sentences([page])
    assert [count for _, count, _ in found] == [LONGEST_SENTENCE + 1, LONGEST_SENTENCE + 3]


def test_every_ssh_tunnel_the_docs_give_exits_when_its_port_is_taken():
    # Pre-release security review: without ExitOnForwardFailure, ssh only warns when the
    # local port is taken, and the client sends its token to whatever process holds it.
    root = DOCS.parent
    pages = [root / "README.md", root / "CONTRIBUTING.md", *sorted(DOCS.rglob("*.md"))]
    pages += sorted((root / "skills").rglob("*.md"))
    commands = [
        (page.name, line.strip())
        for page in pages
        for line in page.read_text().splitlines()
        if re.search(r"\bssh\b[^`]*\s-L\s*\d", line)
    ]
    assert len(commands) >= 3, commands
    assert [c for c in commands if "-o ExitOnForwardFailure=yes" not in c[1]] == []
