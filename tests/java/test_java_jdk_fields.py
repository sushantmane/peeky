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
        # An ArrayList's preview is unchanged (COOKBOOK 7.3); its boxed elements show values.
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
                "no class of the program declares a field `hash`",
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
