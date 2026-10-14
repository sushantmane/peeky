"""Compiler-produced metadata fixtures; JDK lane only, no javap parsing."""

import shutil
import subprocess

import pytest

from peeky.java_metadata import generate_source_map


def test_javac_nested_anonymous_and_overlapping_lines(tmp_path):
    compiler = shutil.which("javac")
    if compiler is None:
        pytest.skip("JDK required")
    source = tmp_path / "src/demo/Main.java"
    source.parent.mkdir(parents=True)
    source.write_text("""package demo;
public class Main {
  static class Inner {
    static int value() { return 3; }
  }
  public static void main(String[] args) {
    Runnable job = new Runnable() {
      public void run() { System.out.println(Inner.value()); }
    };
    job.run();
  }
}
""")
    classes = tmp_path / "classes"
    subprocess.run([compiler, "-g", "-d", str(classes), str(source)], check=True, timeout=15)
    result = generate_source_map(classes, [source], tmp_path)["classes"]
    assert set(result) == {"demo.Main", "demo.Main$Inner", "demo.Main$1"}
    assert all(entry["path"] == str(source) for entry in result.values())
    assert 4 in result["demo.Main$Inner"]["lines"]
    assert 8 in result["demo.Main$1"]["lines"]
    assert 10 in result["demo.Main"]["lines"]


def test_javac_missing_debug_info_rejected(tmp_path):
    compiler = shutil.which("javac")
    if compiler is None:
        pytest.skip("JDK required")
    source = tmp_path / "Main.java"
    source.write_text("public class Main { public static void main(String[] args) {} }\n")
    classes = tmp_path / "classes"
    subprocess.run([compiler, "-g:none", "-d", str(classes), str(source)], check=True, timeout=15)
    with pytest.raises(ValueError, match="debug information"):
        generate_source_map(classes, [source], tmp_path)


def test_javac_interfaces_and_annotations_without_concrete_code(tmp_path):
    compiler = shutil.which("javac")
    if compiler is None:
        pytest.skip("JDK required")
    sources = {
        "Value.java": "interface Value { int get(); }\n",
        "Marker.java": "@interface Marker {}\n",
        "Main.java": "@Marker public class Main implements Value { public int get() { return 2; } }\n",
    }
    paths = []
    for name, text in sources.items():
        path = tmp_path / name
        path.write_text(text)
        paths.append(path)
    classes = tmp_path / "classes"
    subprocess.run(
        [compiler, "-g", "-d", str(classes), *(str(p) for p in paths)], check=True, timeout=15
    )
    result = generate_source_map(classes, paths, tmp_path)["classes"]
    assert result["Value"]["lines"] == []
    assert result["Marker"]["lines"] == []
    assert result["Main"]["lines"] == [1]
