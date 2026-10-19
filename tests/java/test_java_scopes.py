"""Java scopes for describe, from class files javac really writes (#91)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from peeky.java_metadata import _class_metadata

SOURCE = """package demo;
public class X {
  static int counter = init();
  int a = 1;
  enum Color { RED, GREEN }
  record P(int x) {}
  class Inner { int v; Inner(int v) { this.v = v; } }
  static int init() { return 3; }
  static { counter++; }
  X() { }
  X(int start) {
    a = start;
  }
  void run() { Runnable r = () -> System.out.println("x"); r.run(); }
  public static void main(String[] args) { new X().run(); }
  int below = 5;
  { below++; }
}
"""


def scopes(classes):
    found = {}
    for path in sorted(Path(classes).rglob("*.class")):
        methods = []
        _class_metadata(path.read_bytes(), scopes=methods)
        for method, descriptor, first, last, names in methods:
            found[f"{path.stem}.{method}{descriptor}"] = ([first, last], names)
    return found


@pytest.fixture
def compiled(tmp_path):
    javac = shutil.which("javac")
    if not javac:
        pytest.skip("javac is not installed")
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/X.java").write_text(SOURCE)

    def build(*flags):
        out = tmp_path / ("classes" + "".join(flags))
        subprocess.run(
            [javac, *flags, "-proc:none", "-d", str(out), "demo/X.java"], cwd=tmp_path, check=True
        )
        return out

    return build


def test_scopes_list_selectable_names_and_constructor_spans(compiled):
    found = scopes(compiled("-g"))
    # A constructor spans its own lines, not the field initializers (above or below
    # it) javac copies into it (review of #96).
    assert found["X.<init>()V"] == ([10, 10], ["this"])
    assert found["X.<init>(I)V"] == ([11, 13], ["this", "start"])
    # `this$0` is no selector; the inner class's own names are.
    assert found["X$Inner.<init>(Ldemo/X;I)V"] == ([7, 7], ["this", "v"])
    # javac -g leaves out the table only where there is nothing to name.
    assert found["X.init()I"] == ([8, 8], [])
    assert found["X.lambda$run$0()V"][1] == []
    assert found["X$P.<init>(I)V"][1] == ["this", "x"]
    assert found["X.main([Ljava/lang/String;)V"][1] == ["args"]


def test_classes_without_local_names_say_so(compiled):
    # Plain javac is -g:source,lines: methods with `this` or parameters have no names.
    with pytest.raises(ValueError, match="no local variable names.*compile with javac -g"):
        scopes(compiled())


IMPLICIT = """package demo;
public class H {
  int p = 1;
  static int s = 7;

  int m() {
    return p;
  }

  int q = 2;
  static int t = 8;

  Object anon() {
    return new Object() {
      int h = 3;
      public String toString() {
        return "x" + h;
      }
      int j = 4;
    };
  }
}
"""


def test_an_implicit_constructor_never_spans_the_methods_between_fields(tmp_path):
    # Review of #96: a class with no declared constructor ended on its last field.
    javac = shutil.which("javac")
    if not javac:
        pytest.skip("javac is not installed")
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/H.java").write_text(IMPLICIT)
    subprocess.run(
        [javac, "-g", "-proc:none", "-d", "out", "demo/H.java"], cwd=tmp_path, check=True
    )
    found = scopes(tmp_path / "out")
    assert found["H.<init>()V"][0] == [2, 3]
    # A static initializer too, between its static fields (review of #96).
    assert found["H.<clinit>()V"][0] == [4, 4]
    assert found["H$1.<init>(Ldemo/H;)V"][0] == [14, 15]
    assert found["H.m()I"][0] == [7, 7]
