# Peeky Cookbook: Java

What is particular to Java in Peeky's recipes. Read the [COOKBOOK](../COOKBOOK.md) for the recipes every language shares: each section here has the number and title of the COOKBOOK section it adds to. The one-page summary of Java's rules is [java.md](java.md).

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
- [2.9 What a Function Returned](#29-what-a-function-returned)
- [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python)
- [5.2 Stepping and Running to a Line](#52-stepping-and-running-to-a-line)
- [7.3 Java (launched)](#73-java-launched)
  - [Reading Java Values](#reading-java-values)
- [7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)](#74-java-tests-already-running-gradle---debug-jvm-eg-venice)

## 1.1 What Can I Ask?

Adds to [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask).

- **Java:** each name has its declared type (`op (char)`, `results (StringBuilder)`), and `mutable:` lists the kinds `set` changes ([7.3](#73-java-launched)).

## 2.9 What a Function Returned

Adds to [COOKBOOK 2.9](../COOKBOOK.md#29-what-a-function-returned).

- **Java:** Java's returns come from JDI, which lists nothing for a method that threw: `java-debug listed no returned value: the method returns nothing, or it ended by an exception`.

## 4.6 Same Object, or a Copy? (Java, Python)

Adds to [COOKBOOK 4.6](../COOKBOOK.md#46-same-object-or-a-copy-java-python).

**What it answers:** Is this variable the list's own element, or a copy whose changes go nowhere?

`examples/java/demo/BalanceBug.java` gives each task to the least-loaded worker, but a tie copies the worker, so the load it adds is lost (`ORDER:a a a a`).

```sh
peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:29 \
  --capture 'best,workers[1]' --hits 2
peeky sql "SELECT b.hit, object_ref(b.value) AS best, object_ref(w.value) AS list_a,
  object_ref(b.value) = object_ref(w.value) AS same FROM last_run b
  JOIN last_run w ON w.hit = b.hit AND w.selector = 'workers[1]' WHERE b.selector = 'best'"
```

```
hit  best                   list_a                 same
1    BalanceBug$Worker@56   BalanceBug$Worker@100  0
2    BalanceBug$Worker@102  BalanceBug$Worker@100  0
```

`object_ref(value)` is the object a Java value is. Within one run, equal means the same object. Across runs the numbers differ (`queries diff` masks them), so compare objects only inside a run. To test the hypothesis, give the list's worker the load it should have had: `--set 'workers[1].load=5' --set-hit 1` turns `a a a a` into `a b b b`.

- **Identity, not value:** where the code compares objects with `==` (two `Integer`s, two Strings), capture both sides and read `object_ref`: equal values with different objects (`Integer@60 {value=1001}` and `Integer@106 {value=1001}`) are what `==` sees as different. To test the hypothesis, set what the comparison produced, a `boolean` local the code reads next (`--set-at 56@4 gleich=true`), not the objects: the program then runs as if they compared equal.

## 5.2 Stepping and Running to a Line

Adds to [COOKBOOK 5.2](../COOKBOOK.md#52-stepping-and-running-to-a-line).

- **Java:** For Java, `--into` and `--out` skip JDK classes (`java.*`, `javax.*`, `jdk.*`, `sun.*`, `com.sun.*`), so `step --into` on a line that calls `Math.min` stays in your code and reaches lambda bodies. A plain `step` at the end of a method that JDK code called (a `Runnable`, a comparator) stops outside your sources; `continue` from there. Java adapter bundles built before stepping are refused with a hint to rebuild them.

## 7.3 Java (launched)

**Common cases**

- A Java target needs `--config <file.json>` (the adapter bundle, sources and main class); `describe`'s commands repeat it.
- Objects and arrays show their first fields or elements, an `ArrayList` its size and first elements (an empty one, its raw fields); read members through `this` (`this.total`).
- Scalar fields and array elements can be set too (`--set current.end=10`), not final or static fields, boxed values or an `ArrayList`'s elements.
- A field a JDK class declares is never changed (a String's `hash`, an `ArrayList`'s `size`, inherited too), nor an element of an array one holds, under any name; a String and a boxed number show as values, their fields read by name (`n.value`).
- A char takes a char in single quotes or a code point (`--set "op='+'"`, `op=43`), with a current adapter bundle; an older one says to rebuild it.
- What a local's declared type rules out (`results=1` on a `StringBuilder`, `left='+'` on a `long`, a name no method on the line declares) is refused before the program runs; `describe` gives each name's declared type.
- A String local, field or element (declared String) takes a text in double quotes (`--set 'name="apple"'`): a new String, with a current adapter bundle; a local the program does not read after the stop is refused.
- A line several methods share (a lambda on its method's line) stops in only one of them; the probe says which.

`examples/java/pagination.json` (the bundle path is relative to the config file):

```json
{"bundle": "../../work/java-adapter", "sources": ["demo/PaginationBug.java"], "main_class": "demo.PaginationBug", "arguments": []}
```

```sh
peeky probe examples/java --config examples/java/pagination.json --at demo/PaginationBug.java:13 \
  --capture start,pageSize,items --set start=0
```

```
hit  phase      line  function                      selector  type   value
1    observed   13    PaginationBug.main(String[])  start     int    3  [settable]
1    observed   13    PaginationBug.main(String[])  pageSize  int    3  [settable]
1    observed   13    PaginationBug.main(String[])  items     int[]  int[8]@51 [10, 20, 30, 40, 50, 60, 70, 80]
...
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.54
output:
  PAGE:[10, 20, 30]
```

Fields and array elements can be changed too. `examples/java/demo/MergeBug.java` merges intervals but overwrites `current.end` with a smaller end. To test "if `current.end` kept 10, the merge would be right", change the field at the second hit:

```sh
peeky probe examples/java --config examples/java/merge.json --at demo/MergeBug.java:26 \
  --capture current,next --hits 2 --set current.end=10 --set-hit 2
```

```
hit  phase      line  function                 selector  type               value
1    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
1    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@58 {end=5, start=2}
2    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=5, start=1}
2    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@63 {end=14, start=12}
2    after-set  26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
2    after-set  26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@63 {end=14, start=12}
set current.end: 5 -> 10
output:
  MERGED:[[1,10], [12,14]]
```

The output changed from `[[1,5], [12,14]]` to `[[1,10], [12,14]]`: the merge was right except for `end`. `this.count=0`, `arr[0]=1` and `arr[-1]=9` work the same way, and `session set current.end=10` changes a field in a session. Captured fields and elements say `[settable]` when they can be changed. Final and static fields, boxed values (`Integer`) and an `ArrayList`'s elements cannot (change a field of one: `merged[0].end=10`); a String field or element takes a text (below). The value is read back from the object after the change. A field whose name a subclass's static field hides, or another field shares, is refused (java-debug would write the other field). Field changes need an adapter bundle built by this version of `scripts/setup_java_adapter.py`; with an older one, `set` refuses them and says to rebuild it.

- **The JDK's own fields:** a field a JDK class declares is never changed, inherited or not: a String's cached `hash` must match its text for every `HashMap` holding it, an `ArrayList`'s `size` its elements, so a change could corrupt the program's state behind its back. A JDK class is one in a JDK module (`java.base`; `jdk.jshell` and `jdk.compiler` too, though the application class loader loads them), or, on Java 8, one the bootstrap or platform class loader loaded; never judged by its package: a class of the program in `javax.foo` is the program's. The adapter names each field's declaring class and whether it is the JDK's; rows say `a.bezeichnung.hash: int = 0  [not settable: declared by java.lang.String (the JDK's own state)]`, and `set` refuses naming it (`'size' cannot be changed: it is declared by java.util.ArrayList, a JDK class`).
- **Arrays the JDK holds:** an element of an array a JDK class's field holds is never changed either, under any name: a String's bytes are refused as `a.bezeichnung.value[0]`, and as a local or a field of the program's that refers to the same array (one taken through reflection or `Unsafe`): when the change is made, the adapter reads what refers to the array (`the array is held by java.lang.String.value, a JDK class's field`). An array inside another array such a field holds counts too, whatever that array's type: an `Object[]` is no proof of whose its elements are (a JDK cache keeps arrays in them, and a `HashMap<String, Object[]>`'s entry holds one: `inside an array held by java.util.HashMap$Node.value`). Only the backing arrays of `ArrayList`, `Vector`, `ArrayDeque` and `Arrays.asList`'s list hold the program's own elements, where the list itself is the program's, judged the same way: its iterators and sublists do not own it, but whoever holds one holds the list. An `ArrayList<int[]>`'s rows change (`rows[0][1]=9`, or `row[1]=9` for the same array, inside a `for` over the list too); a list a `HashMap` holds is the map's (`in a list held by java.util.HashMap$Node.value`), and so is one whose iterator a `HashMap` holds, or whose sublist an unmodifiable view wraps (`in a list held, through an iterator or sublist, by ...`). It refuses where it cannot tell: more than 64 objects refer to the array and the arrays holding it, more than 4 arrays deep, or a JVM that cannot list them. The JVM searches its heap for them, which no request can interrupt: Peeky waits at most 2 s, less where the session's pause budget (`--max-pause`; 10 s for an attached JVM) leaves less, keeping 1 s of it for the rest of the change, and refuses past that (`Peeky could not tell within 2000 ms ...`, `only 0.6 s of the session's pause budget is left ...`; the search itself runs on in the JVM until it ends). A `[settable]` mark on an element goes by the selector's path only: an array the JDK holds under another name is refused when the change is made, not marked.
- **Before the run:** a probe's `--set` is refused where the class files show it: a field of a value declared a type that is not one of the program's classes (`a.bezeichnung` is declared `java.lang.String`), or of a program class extending one, that no class of the program which may be that value (a subclass, or one extending another JDK class) declares (MUTATION_REJECTED, exit 2). The program's own fields keep their rules, also in a class that extends a JDK class (`basket.limit=8` in a `Basket extends ArrayList<String>`), and so do fields of its objects reached through a list (`merged[0].end=10`).

A value must fit the variable's type exactly. For example, `int` 3000000000 and `010` (octal for `Long.decode`) are refused rather than silently changed.

- **Chars:** a char takes a char literal in single quotes, as Java writes one (`'+'`, `'\n'`, `'\''`, `'\u0041'`), or a code point from 0 to 65535 (`43`, as `char op = 43;` is `'+'`): `set op: - -> +` (java-debug shows a char as itself). This needs an adapter bundle built by this version of `scripts/setup_java_adapter.py`; an older one refuses, naming the char rule and the rebuild.
- **Refused before the run:** each `--set` and `--set-at` of a plain local name is judged against the class files' local variable tables before the program runs, in each method that stops on its line (the first in its class file that holds it, as java-debug binds it: of two overloads on one line, the first declared): a name none of them has there (`NOT_FOUND`, listing the locals there), a declared type no change reaches (`results` is declared `StringBuilder`), a value the declared type cannot hold (`left='+'` on a `long`), or a local the code never reads after the line's start (below). A field (`this.x`) and an element are judged at the stop. A refusal at a stop names the type rule first (`op is char; only byte, short, …`), not the literal's shape.

- **Strings:** A String local, field or element takes a text in double quotes, `--set 'name="apple"'`: java-debug makes a new String of it, and the variable refers to that; other references (`String alias = name;`) keep the old one. java-debug shows a String between quotes without escaping it (`"new "quoted""`). This needs an adapter bundle built with this version of `scripts/setup_java_adapter.py`; an older one refuses texts and says to rebuild it.
- **Declared types:** What a variable is declared decides, as java-debug's own check does: `Object o = "x"`, `CharSequence cs`, an `Object` field or an `Object[]`'s element holds a String but takes no text, and is not marked `[settable]` (Peeky reads declared types from the class files).
- **Locals never read again:** A local the program never reads after the stop is never changed, of any type, and is not marked `[settable]`. Peeky follows the method's bytecode from where the stop's line begins (a line breakpoint or a step stops there), and refuses when no path reads the local before storing into it again: a local a lambda or inner class captured before the stop (`Supplier<String> lam = () -> cap;`: `lam` keeps its own copy), and a constant javac compiles into the code that uses it (`final int k = 5;`, `final String s = "x";`). Where Peeky cannot follow the code (another kind of stop), the change is made with a note that a lambda or inner class that captured it before keeps the old value.
- **Shared lines:** A line that several methods share (a lambda body on its method's line, like `.filter(item -> below(item))`, a static initializer's lambda, nested lambdas) stops in only one of them: java-debug picks the first in the class file. Peeky warns which one stops and which never do (`stops only in needsRestock there, never in lambda$needsRestock$0`). To stop in another, put the breakpoint on a line of its own, or `session step --into` from that line.

### Reading Java Values

- In a launched Java program, a value you capture or `session get` shows more: an object its first fields inline (`Merge$Interval@143 {end=10, start=1}`), an array its first elements (`int[3]@144 [1, 2, 3]`), an `ArrayList` its size and first elements (`ArrayList@142 size=2 [...]`; an empty one shows its raw fields, `size=0`).
- A `String` and a boxed number (`Integer`, `Long`, `Character`, `Boolean`, ...) are values: `a.bezeichnung: String = "Schraube"`, `a.artikelNr: Integer = Integer@57 {value=1001}`, in a listing of their holder's fields and an `ArrayList`'s elements too. Their class's own fields (a String's `hash` and bytes) are not listed; a name still reads one (`a.artikelNr.value`), never settable. The object number tells two equal boxed values apart (`==` compares them).
- These are the debugger's reads of the value's own fields (no code runs in the program); Java `inspect` listings stay plain. An attached JVM shows plain values to keep its pauses short.
- `ArrayList` selectors index like arrays (`merged[0]`, `merged[-1].end`) within its `size`, unless its backing array holds more than 512 slots.
- Fields that are Strings and chars show as Java literals (`name="say \"hi\""`, `quote='"'`), a String cut after 38 characters with `…`.
- Java object numbers (`@143`) follow the order the debugger first touched objects, so `queries diff` treats them like addresses in Java runs.
- A Java `step` from a block's last statement stops on its closing `}`, where the block's locals are gone; read them before stepping.

## 7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)

```sh
./gradlew :internal:venice-test-common:integrationTests_32 \
  --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
```

```json
{"bundle": "/abs/path/to/peeky/work/java-adapter", "port": 5005,
 "sources": ["internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java"],
 "class_roots": ["internal/venice-test-common/build/classes/java/integrationTest"]}
```

```sh
T=internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java
peeky probe ~/workspace/venice --config attach.json --at $T:85 \
  --capture storeName,sendControlMessageDirectly --hits 2 --timeout 240
```

Attaching never kills the JVM; closing detaches. Provenance is labelled `attached`, edits cannot be verified this way, and `--arg` is refused because the program is already running. Details are in [JAVA_ATTACH.md](../JAVA_ATTACH.md), including breakpoints in production classes.
