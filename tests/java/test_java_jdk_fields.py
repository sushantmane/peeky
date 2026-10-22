"""A JDK class's own fields are never offered for a change (#204), through real java-debug:
a String's cached hash, an ArrayList's size, inherited or not; the program's own fields
stay settable, and a String or boxed number shows as a value."""

import json
import os
import re
from pathlib import Path

import pytest

from peeky.cli import main

STOCK = """package shop;

import java.util.ArrayList;

public class Stock {
    static final class Item {
        final String name;
        final Integer count;
        int stock;

        Item(String name, Integer count, int stock) {
            this.name = name;
            this.count = count;
            this.stock = stock;
        }
    }

    static final class Basket extends ArrayList<String> {
        int limit = 3;
    }

    // Fields named like the JDK's, in a class no value below can be.
    static final class Other {
        int size;
        int value;
    }

    public static void main(String[] args) {
        Item item = new Item(args[0], Integer.valueOf(1001), 4);
        ArrayList<Integer> items = new ArrayList<>();
        items.add(4);
        items.add(5);
        Basket basket = new Basket();
        basket.add("nut");
        System.out.println("before"); // OBSERVE
        System.out.println("AFTER " + item.name + " " + item.stock + " " + items.size() + " "
            + basket.size() + " " + basket.limit + " " + item.name.hashCode());
    }
}
"""


def java_hash(text):
    """String.hashCode() of ``text`` (ASCII), as a Java int."""
    value = 0
    for char in text:
        value = (31 * value + ord(char)) & 0xFFFFFFFF
    return value - 2**32 if value >= 2**31 else value


@pytest.fixture
def stock(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "stock"
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "Stock.java").write_text(STOCK)
    config = root / "stock.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["shop/Stock.java"],
                "main_class": "shop.Stock",
                "arguments": ["Bolt"],
            }
        )
    )
    line = next(i for i, text in enumerate(STOCK.splitlines(), 1) if "OBSERVE" in text)
    return root, config, f"shop/Stock.java:{line}"


def probe(capsys, stock, *options):
    root, config, at = stock
    argv = ["probe", str(root), "--config", str(config), "--at", at, *options]
    code = main([*argv, "--json", "--no-save", "--quiet"])
    return code, json.loads(capsys.readouterr().out)


def main_error(capsys, assignment):
    """The refusal of a `session set`."""
    assert main(["session", "set", assignment]) == 2, assignment
    return capsys.readouterr().err


def get(capsys, selector):
    assert main(["session", "get", selector, "--json"]) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)[0]


@pytest.mark.timeout(300)
def test_a_session_never_offers_a_jdk_classs_fields_and_keeps_the_programs(stock, capsys):
    root, config, at = stock
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        # A String and a boxed number are values: their class's fields are not listed.
        listed = {child["name"]: child for child in get(capsys, "item")["children"]}
        assert listed["name"]["value"] == '"Bolt"' and "expandable" not in listed["name"]
        assert re.fullmatch(r"Integer@[0-9]+ \{value=1001\}", listed["count"]["value"])
        assert "expandable" not in listed["count"]
        assert listed["stock"]["settable"] is True
        name = get(capsys, "item.name")
        assert name["value"] == '"Bolt"' and "children" not in name
        # Read by name, they are never settable, and say whose they are.
        for selector, owner in (
            ("item.name.hash", "java.lang.String"),
            ("item.name.hashIsZero", "java.lang.String"),
            ("item.count.value", "java.lang.Integer"),
            ("items.size", "java.util.ArrayList"),
            ("basket.size", "java.util.ArrayList"),
        ):
            row = get(capsys, selector)
            assert row["settable"] is False and owner in row["not_settable"], row
        assert get(capsys, "item.count.value")["value"] == "1001"
        assert get(capsys, "basket.limit")["settable"] is True
        # An ArrayList's preview is unchanged (java-cookbook 7.3); its boxed elements show values.
        items = get(capsys, "items")
        assert re.fullmatch(
            r"ArrayList@[0-9]+ size=2 \[Integer@[0-9]+, Integer@[0-9]+\]", items["value"]
        )
        shown = [(c["selector"], re.sub("@[0-9]+", "@N", c["value"])) for c in items["children"]]
        assert shown == [("items[0]", "Integer@N {value=4}"), ("items[1]", "Integer@N {value=5}")]
        for assignment, owner in (
            ("item.name.hash=5", "declared by java.lang.String, a JDK class"),
            ("item.name.hashIsZero=true", "declared by java.lang.String, a JDK class"),
            ("item.name.value[0]=66", "a field the JDK class java.lang.String declares"),
            ("items.size=0", "declared by java.util.ArrayList, a JDK class"),
            ("items.modCount=0", "declared by java.util.AbstractList, a JDK class"),
            ("basket.size=0", "declared by java.util.ArrayList, a JDK class"),
        ):
            assert main(["session", "set", assignment]) == 2, assignment
            error = capsys.readouterr().err
            assert "[MUTATION_REJECTED]" in error and owner in error, error
        assert main(["session", "set", "item.stock=9"]) == 0
        assert "set item.stock: 4 -> 9" in capsys.readouterr().out
        assert main(["session", "set", "basket.limit=8"]) == 0
        assert "set basket.limit: 3 -> 8" in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        assert f"AFTER Bolt 9 2 1 8 {java_hash('Bolt')}" in output
    finally:
        main(["session", "close"])


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "assignment, texts",
    [
        (
            "item.name.hash=5",
            [
                "`item.name` is declared java.lang.String, a JDK type",
                "no class of the program that may be one declares a field `hash`",
            ],
        ),
        ("items.size=0", ["`items` is declared java.util.ArrayList, a JDK type"]),
        (
            "basket.size=0",
            [
                "`basket` is declared shop.Stock$Basket, which extends the JDK class java.util.ArrayList"
            ],
        ),
        ("item.name.value[0]=66", ["`item.name.value[0]` is in an array that field holds"]),
    ],
)
def test_a_jdk_classs_field_is_refused_before_the_run(stock, capsys, assignment, texts):
    code, result = probe(capsys, stock, "--capture", "item", "--set", assignment)
    assert code == 2 and result.get("refused_before_launch") is True, result
    assert result["hits"] == 0 and result["error"]["code"] == "MUTATION_REJECTED"
    for text in [*texts, "Peeky never changes a JDK class's fields", "nothing ran"]:
        assert text in result["error"]["message"], result["error"]


@pytest.mark.timeout(180)
def test_the_programs_own_fields_stay_settable_beside_the_jdks(stock, capsys):
    capture = "item.stock,basket.limit,item.name.hash,basket.size"
    code, result = probe(
        capsys, stock, "--capture", capture, "--set", "item.stock=9", "--set", "basket.limit=8"
    )
    assert code == 0, result.get("error")
    assert f"AFTER Bolt 9 2 1 8 {java_hash('Bolt')}" in result["output"]
    observed = {row["selector"]: row for row in result["rows"] if row["phase"] == "observed"}
    assert {selector: observed[selector]["settable"] for selector in capture.split(",")} == {
        "item.stock": True,
        "basket.limit": True,
        "item.name.hash": False,
        "basket.size": False,
    }
    assert "java.lang.String" in observed["item.name.hash"]["not_settable"]
    assert "java.util.ArrayList" in observed["basket.size"]["not_settable"]


ALIAS = """package shop;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import javax.foo.Box;
import jdk.jshell.JShell;
import sun.misc.Unsafe;

public class Alias {
    static final class Holder {
        byte[] bytes;
        int count = 1;
    }

    public static void main(String[] args) throws Exception {
        Field theUnsafe = Unsafe.class.getDeclaredField("theUnsafe");
        theUnsafe.setAccessible(true);
        Unsafe unsafe = (Unsafe) theUnsafe.get(null);
        String text = new String(args[0]);
        int before = text.hashCode();
        long offset = unsafe.objectFieldOffset(String.class.getDeclaredField("value"));
        byte[] alias = (byte[]) unsafe.getObject(text, offset);
        Holder holder = new Holder();
        holder.bytes = alias;
        byte[] own = {1, 2, 3};
        Box box = new Box();
        // A JDK module the application class loader loads.
        JShell shell = JShell.builder().executionEngine("local").build();
        // An array inside an Object[] a HashMap holds, and one an ArrayList holds.
        Map<String, Object[]> cache = new HashMap<>();
        int[] cached = {4, 5};
        cache.put("k", new Object[] {cached});
        List<int[]> rows = new ArrayList<>();
        int[] row = {6, 7};
        rows.add(row);
        // A list a HashMap's entry holds: its elements are the map's state.
        Map<String, List<int[]>> lists = new HashMap<>();
        List<int[]> listed = new ArrayList<>();
        int[] deep = {3};
        listed.add(deep);
        lists.put("k", listed);
        // Lists the JDK holds through a view: an iterator a HashMap holds, and an
        // unmodifiable view of a sublist.
        List<int[]> kept = new ArrayList<>();
        int[] keptRow = {2};
        kept.add(keptRow);
        Map<String, Iterator<int[]>> cursors = new HashMap<>();
        cursors.put("k", kept.iterator());
        List<int[]> sliced = new ArrayList<>();
        int[] slicedRow = {1};
        sliced.add(slicedRow);
        List<int[]> frozen = Collections.unmodifiableList(sliced.subList(0, 1));
        // An iterator over `rows` is alive at the stop: a view, not a holder.
        for (int[] each : rows) {
            System.out.println("before " + each.length); // OBSERVE
        }
        System.out.println("AFTER " + text + " " + (text.hashCode() == before) + " " + own[0]
            + " " + holder.count + " " + box.value + " " + box.cells[0] + " " + cached[0]
            + " " + row[0] + " " + row[1] + " " + deep[0] + " " + keptRow[0] + slicedRow[0]
            + " " + frozen.size() + cursors.size() + " " + shell.eval("1 + 1").get(0).value());
        shell.close();
    }
}
"""

# A class of the program in a package that only looks like the JDK's.
BOX = """package javax.foo;

public class Box {
    public int value = 1;
    public int[] cells = {1, 2};
}
"""


@pytest.fixture
def alias(tmp_path, monkeypatch):
    bundle = os.environ.get("PEEKY_JAVA_ADAPTER")
    if not bundle:
        pytest.skip("set PEEKY_JAVA_ADAPTER to a setup_java_adapter.py output directory")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "alias"
    for path, text in (("shop/Alias.java", ALIAS), ("javax/foo/Box.java", BOX)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    config = root / "alias.json"
    config.write_text(
        json.dumps(
            {
                "bundle": str(Path(bundle).resolve()),
                "sources": ["shop/Alias.java", "javax/foo/Box.java"],
                "main_class": "shop.Alias",
                "arguments": ["Bolt"],
            }
        )
    )
    line = next(i for i, text in enumerate(ALIAS.splitlines(), 1) if "OBSERVE" in text)
    return root, config, f"shop/Alias.java:{line}"


@pytest.mark.timeout(300)
def test_an_array_a_jdk_field_holds_is_refused_under_any_name(alias, capsys):
    # A String's bytes, reached through Unsafe: a root local and a field of the program's
    # name the same array, which the launcher finds by what refers to it.
    root, config, at = alias
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        for assignment in ("alias[0]=78", "holder.bytes[0]=78"):
            assert main(["session", "set", assignment]) == 2, assignment
            error = capsys.readouterr().err
            assert "[MUTATION_REJECTED]" in error, error
            assert "held by java.lang.String.value, a JDK class's field" in error, error
        # The program's own array and field still change.
        assert main(["session", "set", "own[0]=9"]) == 0
        assert "set own[0]: 1 -> 9" in capsys.readouterr().out
        assert main(["session", "set", "holder.count=5"]) == 0
        capsys.readouterr()
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        # The text and its cached hash are what they were.
        assert "AFTER Bolt true 9 5 1 1 4 6 7 3 21 11 2" in output
    finally:
        main(["session", "close"])


@pytest.mark.timeout(300)
def test_a_program_class_in_a_jdk_looking_package_is_the_programs(alias, capsys):
    # `javax.foo.Box` is the program's: the JDK's own class loaders did not load it.
    root, config, at = alias
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        listed = {child["name"]: child for child in get(capsys, "box")["children"]}
        assert listed["value"]["settable"] is True and "not_settable" not in listed["value"]
        # An element is never promised while other threads run (#261 review); `set` checks.
        cell = get(capsys, "box.cells[0]")
        assert cell["settable"] is False and "not_settable" not in cell, cell
        assert cell["maybe_settable"] == "other threads are running; set checks again", cell
        assert main(["session", "set", "box.value=2"]) == 0
        assert "set box.value: 1 -> 2" in capsys.readouterr().out
        assert main(["session", "set", "box.cells[0]=7"]) == 0
        assert "set box.cells[0]: 1 -> 7" in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)["output"]
        assert "AFTER Bolt true 1 1 2 7 4 6 7 3 21 11 2" in output
    finally:
        main(["session", "close"])


@pytest.mark.timeout(300)
def test_a_jdk_module_the_application_loader_loads_is_the_jdks(alias, capsys):
    # PR #210 review round 2: jdk.jshell loads through the application class loader; its
    # module, not its loader, makes it the JDK's.
    root, config, at = alias
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        for selector in ("shell.nextKeyIndex", "shell.closed"):
            row = get(capsys, selector)
            assert row["settable"] is False and "jdk.jshell.JShell" in row["not_settable"], row
        assert main(["session", "set", "shell.nextKeyIndex=0"]) == 2
        error = capsys.readouterr().err
        assert "declared by jdk.jshell.JShell, a JDK class" in error, error
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert (
            "AFTER Bolt true 1 1 1 1 4 6 7 3 21 11 2"
            in json.loads(capsys.readouterr().out)["output"]
        )
    finally:
        main(["session", "close"])


@pytest.mark.timeout(300)
def test_an_array_inside_an_object_array_the_jdk_holds_is_the_jdks(alias, capsys):
    # PR #210 review round 2: an Object[] is no proof of whose its arrays are. One a
    # HashMap's entry holds is the JDK's state; a list's backing array holds the program's.
    root, config, at = alias
    try:
        assert main(["session", "start", str(root), "--config", str(config), "--at", at]) == 0
        capsys.readouterr()
        assert main(["session", "set", "cached[0]=9"]) == 2
        error = capsys.readouterr().err
        assert "inside an array held by java.util.HashMap$Node.value" in error, error
        assert main(["session", "set", "deep[0]=9"]) == 2
        error = capsys.readouterr().err
        assert "in a list held by java.util.HashMap$Node.value" in error, error
        # A list whose iterator or sublist the JDK holds is the JDK's state too (review
        # round 3): the view is followed to whoever holds it.
        for assignment, holder in (
            ("keptRow[0]=9", "java.util.HashMap$Node.value"),
            ("slicedRow[0]=9", "java.util.Collections$Unmodifiable"),
        ):
            error = main_error(capsys, assignment)
            assert "in a list held, through an iterator or sublist, by " + holder in error, error
        # The loop's iterator over `rows` views the list; the program's rows still change.
        assert main(["session", "set", "each[1]=8"]) == 0
        assert "set each[1]: 7 -> 8" in capsys.readouterr().out
        row = get(capsys, "rows[0][0]")
        assert row["maybe_settable"] == "other threads are running; set checks again", row
        assert main(["session", "set", "rows[0][0]=9"]) == 0
        assert "set rows[0][0]: 6 -> 9" in capsys.readouterr().out
        assert main(["session", "continue", "--to-exit"]) == 0
        capsys.readouterr()
        assert main(["session", "report", "--json"]) == 0
        assert (
            "AFTER Bolt true 1 1 1 1 4 9 8 3 21 11 2"
            in json.loads(capsys.readouterr().out)["output"]
        )
    finally:
        main(["session", "close"])
