# Peeky Cookbook: Java

**[AGENT_GUIDE.md](../AGENT_GUIDE.md) and [java.md](java.md) cover most tasks.** This cookbook is
for details, which they cite by section number.

This cookbook covers what is different for Java. The [COOKBOOK](../COOKBOOK.md) has the recipes
every language shares. Each section here has the number and title of the COOKBOOK section it adds
to. For a one-page summary of the Java rules, see [java.md](java.md).

Peeky debugs Java with java-debug, Microsoft's Java debugger. java-debug reads and changes the JVM
through JDI, the JDK's debugger API. For these words and a few more, see
[Words used here](java.md#words-used-here).

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
  - [See each name's declared type](#see-each-names-declared-type)
  - [See where a local is visible](#see-where-a-local-is-visible)
- [2.9 What a Function Returned](#29-what-a-function-returned)
  - [When a method threw](#when-a-method-threw)
- [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python)
  - [Tell the list's own element from a copy](#tell-the-lists-own-element-from-a-copy)
  - [Tell equal values from the same object](#tell-equal-values-from-the-same-object)
- [5.2 Stepping and Running to a Line](#52-stepping-and-running-to-a-line)
  - [What `--into` and `--out` skip](#what---into-and---out-skip)
- [7.3 Java (launched)](#73-java-launched)
  - [Set up a launched target](#set-up-a-launched-target)
  - [What Peeky checks about a Java build](#what-peeky-checks-about-a-java-build)
  - [What can be set](#what-can-be-set)
  - [Change a local](#change-a-local)
  - [Change a field or an array element](#change-a-field-or-an-array-element)
  - [Fields the JDK declares](#fields-the-jdk-declares)
  - [Arrays the JDK holds](#arrays-the-jdk-holds)
  - [Which lists' arrays are the program's](#which-lists-arrays-are-the-programs)
  - [When Peeky can't tell who holds an array](#when-peeky-cant-tell-who-holds-an-array)
  - [Changes refused before the run](#changes-refused-before-the-run)
  - [Values must fit the type](#values-must-fit-the-type)
  - [Set a char](#set-a-char)
  - [Set a String](#set-a-string)
  - [The declared type decides](#the-declared-type-decides)
  - [Locals the program never reads again](#locals-the-program-never-reads-again)
  - [Lines that several methods share](#lines-that-several-methods-share)
  - [Read Java values](#read-java-values)
- [7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)](#74-java-tests-already-running-gradle---debug-jvm-eg-venice)
  - [Attach to a Gradle test](#attach-to-a-gradle-test)
  - [What attaching changes](#what-attaching-changes)

## 1.1 What Can I Ask?

Read [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask) first.

### See each name's declared type

`describe` gives each name its declared type, such as `op (char)` or `results (StringBuilder)`.
Its `mutable:` line lists the kinds that `set` changes ([7.3](#73-java-launched)).

```sh
peeky describe examples/java --config examples/java/merge.json
```

```
language: java
...
names in MergeBug.main (demo/MergeBug.java:22-35): args (String[]), input (java.util.List), merged (java.util.List), current (demo.MergeBug$Interval), next (demo.MergeBug$Interval, lines 26-30)
mutable: root locals, fields (not final or static) and array elements (current.end, xs[0]) of type byte, short, int, long, char, boolean, float or double, and String ones; a char takes a char in single quotes ('+') or a code point (43), a String a text in double quotes (--set 'name="apple"'); never a boxed value (Integer), another object or an ArrayList's elements, nor a field a JDK class declares (a String's hash, an ArrayList's size) or an element of an array one holds. Field, char and String changes need an adapter bundle built by this version of scripts/setup_java_adapter.py. Rows mark what can change [settable]
probe: peeky probe examples/java --config examples/java/merge.json --at demo/MergeBug.java:<line> [--arg A ...] --capture <a,b.c> [--set name=value] [--set-hit N] [--set-at LINE@HIT|LINE@all name=value] [--hits N] [--json]
...
```

### See where a local is visible

A local declared only inside blocks, such as a loop's, also shows the lines where it is visible
([COOKBOOK 1.1](../COOKBOOK.md#block-lines)). For [examples/java](../../examples/java):

```sh
peeky describe examples/java --config examples/java/balance.json
```

```
...
names in BalanceBug.assign (demo/BalanceBug.java:19-32): workers (java.util.List), tasks (int[]), order (java.util.List), task (int, lines 21-30), best (demo.BalanceBug$Worker, lines 22-30), w (demo.BalanceBug$Worker, lines 23-26)
...
```

- The lines come from the class file's LocalVariableTable. A line is listed where its breakpoint
  sees the local: java-debug stops at the line's first instruction, and that is in the local's
  scope.
- So the line that declares a local isn't listed: its first instruction runs before the local
  exists. `best` is declared on line 21, and a stop there doesn't see it yet.
- Nor is a `for` header, whose first instruction starts the loop. `for (int b = 0; …)` on line 49
  gives `b (int, lines 50-53, …)`. A step can still stop part-way through the header, where `b`
  is visible.
- A block on one line, such as `{ int x = n + 1; use(x); }`, gives no line:
  `x (int, no breakpoint line)`. So does a one-line `for`.
- A name declared once, for the rest of the method, gets no lines, such as `order`. So does one
  whose scope runs to the method's end, such as an endless loop's counter.
- A name declared more than once gets every declaration's lines, one that runs to the method's
  end too. In a vote count that declares `ranking` in two loops:
  `ranking (int[], lines 51-53, 78-79)`.
- A name declared with different types is listed once per type, each with its own lines, or
  `no breakpoint line`. An `int value` in an `if` block, then a `String value` to the method's
  end: `value (int, line 31), value (String, line 34)`.

## 2.9 What a Function Returned

Read [COOKBOOK 2.9](../COOKBOOK.md#29-what-a-function-returned) first.

### When a method threw

Java's returned values come from JDI. JDI lists nothing for a method that threw. The `(return)`
row then says:

```
java-debug listed no returned value: the method returns nothing, or it ended by an exception
```

## 4.6 Same Object, or a Copy? (Java, Python)

Read [COOKBOOK 4.6](../COOKBOOK.md#46-same-object-or-a-copy-java-python) first.

### Tell the list's own element from a copy

Use object numbers to tell whether a variable is the list's own element, or a copy whose changes
go nowhere.

`examples/java/demo/BalanceBug.java` gives each task to the least-loaded worker. But a tie copies
the worker, so the load it adds is lost. The program prints `ORDER:a a a a`.

```sh
peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:29 \
  --capture 'best,workers[1]' --hits 2
peeky sql "SELECT b.hit, object_ref(b.value) AS best, object_ref(w.value) AS list_a,
  object_ref(b.value) = object_ref(w.value) AS same FROM last_run b
  JOIN last_run w ON w.hit = b.hit AND w.selector = 'workers[1]' WHERE b.selector = 'best'"
```

```
hit  best                   list_a                 same
1    BalanceBug$Worker@56   BalanceBug$Worker@103  0
2    BalanceBug$Worker@107  BalanceBug$Worker@103  0
(2 rows)
```

At both hits, `best` is a different object from the list's worker `a`. Read object numbers this way:

- `object_ref(value)` is the object that a Java value is.
- **Within one run,** equal numbers mean the same object.
- **Across runs,** the numbers differ, and `queries diff` masks them. Compare objects only inside
  a run.

To test the hypothesis, give the list's worker the load it should have had:
`--set 'workers[1].load=5' --set-hit 1`. The output turns from `a a a a` into `a b b b`.

### Tell equal values from the same object

Where the code compares objects with `==`, such as two `Integer`s or two Strings, capture both
sides and read `object_ref`. `==` sees equal values in different objects as different. For
example: `Integer@60 {value=1001}` and `Integer@106 {value=1001}`.

To test the hypothesis, set what the comparison produced, not the objects. That is a `boolean`
local that the code reads next: `--set-at 56@4 gleich=true`. The program then runs as if the two
compared equal.

## 5.2 Stepping and Running to a Line

Read [COOKBOOK 5.2](../COOKBOOK.md#52-stepping-and-running-to-a-line) first.

### What `--into` and `--out` skip

`step --into` and `step --out` skip JDK classes. For stepping, these are the packages `java.*`,
`javax.*`, `jdk.*`, `sun.*` and `com.sun.*`. So `step --into` on a line that calls `Math.min`
stays in your code, and it reaches lambda bodies.

- A plain `step` at the end of a method that JDK code called, such as a `Runnable` or a
  comparator, stops outside your sources, where nothing is read. Run `continue` from there.
- An adapter bundle built before Peeky could step is refused, with a hint to rebuild it.

## 7.3 Java (launched)

### Set up a launched target

A launched Java target needs `--config <file.json>`. The file names the adapter bundle, the
sources and the main class. `arguments` is optional. An attached target's config differs: see
[7.4](#74-java-tests-already-running-gradle---debug-jvm-eg-venice). The commands that `describe`
prints repeat the `--config`.

For example, `examples/java/pagination.json`. The bundle path is relative to the config file:

```json
{"bundle": "../../work/java-adapter", "sources": ["demo/PaginationBug.java"], "main_class": "demo.PaginationBug", "arguments": []}
```

- `sources` are relative to the target's directory. Declare every `.java` file the program needs.
  Maven or Gradle projects, and external classpaths, aren't supported.
- Use a standard package-relative layout. A source-root prefix works.
- Peeky reads non-preview class files up to Java 21. CI tests Java 17.
- Arguments may hold whitespace other than CR and LF, backslashes and single quotes, and may be
  empty. Embedded double quotes, CR or LF, and NUL are refused: the JDI connector's quoted-string
  contract can't carry them. Peeky takes at most 100 arguments, each at most 8,192 characters.

Build the adapter bundle once, with JDK 17 and Python 3.11 or later. Run it from the repository
root or the unpacked source archive: the wheel doesn't have the script.

```sh
python scripts/setup_java_adapter.py --output "$PWD/work/java-adapter"
```

- The script builds java-debug from its pinned, checksummed source, and Peeky's launcher. It
  applies Peeky's patches to java-debug before compiling, and lists them in the bundle's
  `adapter.json`, under `patches`.
- One patch, `eventhub-log-guard`, keeps java-debug's event thread alive. Without it, that thread
  could die and stop reporting every stop and exit
  ([JAVA_ATTACH.md](../JAVA_ATTACH.md#root-cause-java-debugs-event-thread-died)).
- Peeky checks each patch in the compiled classes, not in the list: the patched `EventHub.class`
  calls `Logger.isLoggable`. A bundle without it is refused with `UNSUPPORTED` and the rebuild
  command. Rebuild an older bundle into a new directory.

### What Peeky checks about a Java build

Each launch copies the declared sources into a new workspace. It compiles them with debug
information, and with annotation processing off. It reads the source, class and line mappings
from the class files.

- The build's record covers the original and copied sources, the compiled classes, the tools and
  their version probes, and the controlled environment.
- A Java session also records the adapter bundle, the generated source map, the main class and
  the arguments.
- Launch, inspection, changes, breakpoint changes and `continue` check these before they run. A
  failed check ends the session, and cleans up its processes and workspace.

These checks bind the known local build and launch inputs. They aren't a cryptographic proof of
every class a hostile JVM loads, nor a sandbox. The program, the compiler and the debugger run as
the worker's user.

Not supported:

- conditional or log breakpoints: breakpoints are unconditional line breakpoints;
- expression evaluation, and hot code replacement;
- automatic `toString` and logical-structure evaluation, which are off;
- remote absolute-path rebinding, arbitrary class loaders, and production Kubernetes.

### What can be set

No kind of variable can always be set. Each row gives the conditions, and links to the full rule.

| What | Can it be set? |
| --- | --- |
| A local of type `byte`, `short`, `int`, `long`, `boolean`, `float`, `double` or `char` | Only if the program reads it after the stop ([rule](#locals-the-program-never-reads-again)) |
| A local declared `String` | Only to a text in double quotes, and only if the program reads it after the stop ([rule](#set-a-string)) |
| A field of one of those types, or declared `String` | Only if it is not final or static, and no JDK class declares it ([rule](#fields-the-jdk-declares)). Also only if no static field hides its name, such as a subclass's, and no other instance field shares it ([rule](#change-a-field-or-an-array-element)). A `String` field takes only a text ([rule](#set-a-string)) |
| An element of an array of one of those types, or of a `String[]` | Only if the array isn't held by a JDK class's field, directly or inside another array that such a field holds ([rule](#arrays-the-jdk-holds)). The backing array of the program's own `ArrayList`, `Vector`, `ArrayDeque` or `Arrays.asList` list doesn't count as the JDK's ([rule](#which-lists-arrays-are-the-programs)). A `String[]` element takes only a text ([rule](#set-a-string)) |
| A local, field or element declared another type, such as `Object` or `CharSequence` | No, even if it holds a String ([rule](#the-declared-type-decides)) |
| A boxed value, such as `Integer` | No |
| An object, such as an `ArrayList`'s element | No. Change one of its fields instead: `merged[0].end=10` |

These rules also apply:

- The value must fit the type exactly ([rule](#values-must-fit-the-type)).
- Field, char and String changes need an adapter bundle built by this version of
  `scripts/setup_java_adapter.py`.

Rows mark what can be changed: `[settable]`. A row of a type that `set` changes, which can't be
changed there, says why instead:

- `[not settable: not read again after this stop]`
  ([rule](#locals-the-program-never-reads-again));
- `[not settable: a constant javac compiles into its uses]`;
- `[not settable: declared Object, not String]` ([rule](#the-declared-type-decides));
- `[not settable: final]`, or `static`: a probe's table shows it on each selector's first row
  only, since it holds at every stop. `--json` has it on every row;
- `[not settable: in a caller's frame]`: only the stopped frame's values change;
- `[not settable: declared by java.lang.String (the JDK's own state)]`
  ([rule](#fields-the-jdk-declares));
- `[not settable: its array is in a list held by …]` ([rule](#arrays-the-jdk-holds)).

An element that Peeky can't promise either way says `[maybe settable: …]`. Then `set` checks again
when the change is made ([rule](#arrays-the-jdk-holds)). At a line breakpoint, that is every
element that no JDK class holds:
`[maybe settable: other threads, the JVM's own included, are running; set checks again]`. A
program with one thread of its own gets it too: the JVM runs service threads of its own, such as
the finalizer and the signal dispatcher. An object or array row has no mark: its type says it
can't be set.

### Change a local

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
status: completed  hits: 1  seconds: 1.187  max pause: 137.8 ms
output:
  PAGE:[10, 20, 30]
```

### Change a field or an array element

`examples/java/demo/MergeBug.java` merges intervals, but it overwrites `current.end` with a
smaller end. To test "if `current.end` kept 10, the merge would be right", change the field at the
second hit:

```sh
peeky probe examples/java --config examples/java/merge.json --at demo/MergeBug.java:26 \
  --capture current,next --hits 2 --set current.end=10 --set-hit 2
```

```
hit  phase      line  function                 selector  type               value
1    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
1    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@58 {end=5, start=2}
2    observed   26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=5, start=1}
2    observed   26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@66 {end=14, start=12}
2    after-set  26    MergeBug.main(String[])  current   MergeBug$Interval  MergeBug$Interval@57 {end=10, start=1}
2    after-set  26    MergeBug.main(String[])  next      MergeBug$Interval  MergeBug$Interval@66 {end=14, start=12}
set current.end: 5 -> 10
status: completed  hits: 2  seconds: 1.061  max pause: 68.6 ms
warning: Interventions are applied once, at hit 2; other hits observe without re-applying.
output:
  MERGED:[[1,10], [12,14]]
```

The output changed from `[[1,5], [12,14]]` to `[[1,10], [12,14]]`. So the merge was right except
for `end`.

- `this.count=0`, `arr[0]=1` and `arr[-1]=9` work the same way, where the
  [table above](#what-can-be-set) allows them.
- An eligible program-owned instance field changes through a Java local, parameter or `this`
  reference, subject to the holder and field checks of the [table above](#what-can-be-set). So
  `--set-at 15@all db.count=0`, on a `settle(Debounce db, int level)`'s early `return false;`,
  resets the count each time the input agrees with the accepted level
  ([COOKBOOK 2.8](../COOKBOOK.md#change-it-at-every-stop-all)).
- `--set-at LINE@HIT name=@before` writes back an `int`, `long`, `short`, `byte`, `boolean` or
  `char` as it was before the probe's change at the same hit
  ([COOKBOOK 2.8](../COOKBOOK.md#skip-a-statement-restore-with-before)). Never a `float` or
  `double`, which java-debug may show rounded, or a String or other object. `continue;` has no
  code of its own: restore at the next line that has. A field or element is written back only
  in the object or array its change wrote, by java-debug's id (`Box@27`), and a local only to
  the declaration its change wrote.
- In a session, `session set current.end=10` changes a field.
- Peeky reads the value back from the object after the change.
- A field is refused unless its name means exactly one instance field. It is refused if a static
  field hides it, such as a subclass's. It is also refused if another instance field has the same
  name. java-debug would write the other field.
- A static field that this field itself hides, such as a superclass's, doesn't matter.
- Field changes need an adapter bundle built by this version of `scripts/setup_java_adapter.py`.
  With an older one, `set` refuses them and says to rebuild it.

### Fields the JDK declares

A field that a JDK class declares is never changed, inherited or not. Other code relies on these
fields. A String's cached `hash` must match its text for every `HashMap` holding it. An
`ArrayList`'s `size` must match its elements. So a change could corrupt the program's state behind
its back.

The adapter names each field's declaring class, and whether it is the JDK's. The row says so:

```sh
peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:30 \
  --capture best.name,best.name.hash --hits 1 --until hits
```

```
hit  phase     line  function                       selector        type    value
1    observed  30    BalanceBug.assign(List,int[])  best.name       String  "a"  [not settable: final]
1    observed  30    BalanceBug.assign(List,int[])  best.name.hash  int     0  [not settable: declared by java.lang.String (the JDK's own state)]
...
```

With an adapter bundle that doesn't name a field's class, Peeky changes no field at all.

A change at a stop is refused, naming the class:
`` `merged.size` cannot be changed: it is declared by java.util.ArrayList, a JDK class ``. Where
the class files show it, the change is refused before the run
([below](#changes-refused-before-the-run)):

```sh
peeky probe examples/java --config examples/java/merge.json --at demo/MergeBug.java:26 \
  --capture merged --set merged.size=0
```

```
error [MUTATION_REJECTED]: `merged.size` cannot be changed at demo/MergeBug.java:26, at any stop of this build: `merged` is declared java.util.List, a JDK type, and no class of the program that may be one declares a field `size`, so any such field is declared by a JDK class. Peeky never changes a JDK class's fields (a String's cached hash, an ArrayList's size): other code relies on them, so a change could corrupt the program's state behind its back. Refused before the program runs: nothing ran
hint: Observe the value instead, or change the source with a candidate edit (`--old`/`--new`) and verify it; rows marked [settable] can be changed here.
```

What counts as a JDK class:

- A class in a JDK module, such as `java.base`.
- Also a class in `jdk.jshell` or `jdk.compiler`, though the application class loader loads them.
- On Java 8, a class that the bootstrap or extension class loader loaded.
- Never a class judged by its package alone. A class of the program in `javax.foo` is the
  program's.

### Arrays the JDK holds

An element of an array that a JDK class's field holds can't be changed either, under any name. The
only exception is the backing array of the program's own `ArrayList`, `Vector`, `ArrayDeque` or
`Arrays.asList` list: see
[Which lists' arrays are the program's](#which-lists-arrays-are-the-programs).

- **Under its path,** such as a String's bytes, `a.bezeichnung.value[0]`. Here the class files
  show whose array it is, so the change is refused before the run:

  ```sh
  peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:30 \
    --capture best.name --hits 1 --set 'best.name.value[0]=98'
  ```

  ```
  error [MUTATION_REJECTED]: `best.name.value[0]` cannot be changed at demo/BalanceBug.java:30, at any stop of this build: `best.name` is declared java.lang.String, a JDK type, and no class of the program that may be one declares a field `value`, so any such field is declared by a JDK class; `best.name.value[0]` is in an array that field holds. ...
  ```

- **Under another name,** such as a local or a field of the program's that refers to the same
  array. The program may have taken it through reflection or `Unsafe`. When the change is made,
  the adapter reads what refers to the array:
  `the array is held by java.lang.String.value, a JDK class's field`.
- **Inside another array** that such a field holds, whatever that array's type. An `Object[]` is
  no proof of whose its elements are. A JDK cache keeps arrays in them. A
  `HashMap<String, Object[]>`'s entry holds one:
  `inside an array held by java.util.HashMap$Node.value`.

An element's `[settable]` mark follows the same search. Before Peeky marks an array's elements, it
asks the adapter what holds the array, whatever name reaches it. In this vote count, `main` passes
`Collections.unmodifiableList(ballots)`, so each ballot's array is in a list that the view holds:

```sh
peeky session get ranking
```

```
ranking: int[] = int[3]@59 [1, 2, 3]
  ranking[0]: int = 1  [not settable: its array is in a list held by java.util.Collections$UnmodifiableList.list (the JDK's own state)]
  ranking[1]: int = 2  [not settable: its array is in a list held by java.util.Collections$UnmodifiableList.list (the JDK's own state)]
  ranking[2]: int = 3  [not settable: its array is in a list held by java.util.Collections$UnmodifiableList.list (the JDK's own state)]
```

A change asks again, and is refused with the whole selector:

```
peeky: [MUTATION_REJECTED] The debugger refused to change `ranking[1]` (Peeky experimental Java adapter: the array is in a list held by java.util.Collections$UnmodifiableList.list, a JDK class's field; Peeky never changes the JDK's own state); it is unchanged
  hint: Observe the value, or edit the source (`--old`/`--new`) and verify it. The debugger itself refused this write, which no [settable] mark can promise.
```

The program's own array, such as `choice`, can be changed. But at a line breakpoint its elements
read `[maybe settable: other threads, the JVM's own included, are running; set checks again]`:

- java-debug stops only the thread that hit the breakpoint. The JVM's other threads run on.
- One of them could put the array in a list that a JDK class holds before the change is made.
- So Peeky reads what holds the array again at each listing, and keeps no verdict.
- A change suspends every thread of the JVM while it checks and writes the element. Then the
  threads run on, as before.
- If the adapter can't resume them, it disconnects from the JVM, which lets the threads run, and
  ends. The change fails, and the session closes with that reason (`DEBUGGER_ERROR`). An attached
  JVM runs on, and a launched one is ended.

An element is marked `[settable]` only at a stop where every thread is suspended, and no JDK class
holds its array. With a bundle built before this check, elements read `[maybe settable: …]`, which
says to rebuild it.

### Which lists' arrays are the program's

Only the backing arrays of these lists hold the program's own elements: `ArrayList`, `Vector`,
`ArrayDeque`, and the list that `Arrays.asList` returns. Even then, the list itself must be the
program's. Peeky judges the list the same way as an array, by what holds it.

- A list's iterators and sublists don't own it. But whoever holds one of them holds the list.
- A list that a `HashMap` holds is the map's: `in a list held by java.util.HashMap$Node.value`.
- So is a list whose iterator a `HashMap` holds, or whose sublist an unmodifiable view wraps:
  `in a list held, through an iterator or sublist, by ...`.
- The rows of the program's own `ArrayList<int[]>` can change: `rows[0][1]=9`, or `row[1]=9` for
  the same array. That holds inside a `for` over the list too.

### When Peeky can't tell who holds an array

Peeky refuses the change where it can't tell who holds the array:

- More than 64 objects refer to the array and the arrays holding it.
- The array is more than 4 arrays deep.
- The JVM can't list what refers to it.

To find what refers to the array, the JVM searches its heap. No request can interrupt that search.
So Peeky bounds its wait:

- It waits at most 2 s: `Peeky could not tell within 2000 ms ...`.
- It waits less where the session's pause budget leaves less. The budget is `--max-pause`, which
  is 10 s for an attached JVM.
- It keeps 1 s of the budget for the rest of the change:
  `only 0.6 s of the session's pause budget is left ...`. Then change the element at a fresh stop.
- Past that, it refuses. The search itself runs on in the JVM until it ends.

The same search runs when Peeky lists an array's elements, before it marks them. There it waits at
most 0.5 s.

- Where the referrers are too many or too deep, or the JVM can't list them, a change is refused
  too. The row says why: `[not settable: more than 64 objects refer to the array …]`.
- Where the wait runs out, a change may still be made. The row says
  `` [maybe settable: Peeky could not tell within 500 ms …; `set` searches again, for up to 2 s] ``.

### Changes refused before the run

Peeky checks each `--set` and `--set-at` of a plain local name before the program runs. It reads
the local variable tables in the class files. It checks each method that stops on the line. In a
class file, that is the first method that holds the line, as java-debug binds it. Of two overloads
on one line, it is the first declared.

These are refused before the run:

- A name that none of these methods has there. The error is `NOT_FOUND`, and it lists the locals
  there.
- A declared type that no change reaches: `results=1`, where `results` is declared
  `StringBuilder`.
- A value that the declared type can't hold: `left='+'` on a `long`.
- A local that the code never reads after the line's start
  ([below](#locals-the-program-never-reads-again)).

A field, such as `this.x`, and an element are judged at the stop. A refusal at a stop names the
type rule first, not the literal's shape: `op is char; only byte, short, …`.

The exception is a field that the class files show a JDK class declares. A probe's `--set` of it
is refused before the run, with `MUTATION_REJECTED` and exit status 2. So is an element of an array
that it holds. The class files show this when both of these are true:

- The value is declared a type that is not one of the program's classes, such as `a.bezeichnung`
  declared `java.lang.String`. Or it is declared a program class that extends such a type.
- No class of the program that the value may be declares the field. Such a class is a subclass,
  or one that extends another JDK class.

The program's own fields keep their rules, also in a class that extends a JDK class:
`basket.limit=8` in a `Basket extends ArrayList<String>`. So do fields of its objects reached
through a list: `merged[0].end=10`.

To see each name's declared type, run `describe` ([1.1](#11-what-can-i-ask)).

### Values must fit the type

A value must fit the variable's type exactly. Peeky refuses a value rather than silently changing
it. For example, `int` 3000000000 is refused. So is `010`, which `Long.decode` reads as octal.

### Set a char

This is how to write a `char` that [can be set](#what-can-be-set). Give a char literal in single
quotes, as Java writes one: `'+'`, `'\n'`, `'\''`, `'\u0041'`. Or give a code point from 0 to
65535: `43` is `'+'`, as in `char op = 43;`.
java-debug shows a char as itself: `set op: - -> +`.

This needs an adapter bundle built by this version of `scripts/setup_java_adapter.py`. An older
one refuses, naming the char rule and the rebuild.

### Set a String

Where a String local, field or element can be set ([What can be set](#what-can-be-set)), give a
text in double quotes: `--set 'name="apple"'`. java-debug makes a new String of it, and the
variable refers to that. Other references to the old
String keep it, such as `alias` after `String alias = name;`.

```sh
peeky probe examples/java --config examples/java/balance.json --at demo/BalanceBug.java:38 \
  --capture name --set 'name="z"'
```

```
hit  phase      line  function                   selector  type    value
1    observed   38    BalanceBug.main(String[])  name      String  "c"  [settable]
1    after-set  38    BalanceBug.main(String[])  name      String  "z"  [settable]
set name: "c" -> "z"
...
```

- java-debug shows a String between quotes without escaping it: `"new "quoted""`.
- This needs an adapter bundle built by this version of `scripts/setup_java_adapter.py`. An older
  one refuses texts and says to rebuild it.

### The declared type decides

What a variable is declared decides whether it takes a text, as java-debug's own check does.
Peeky reads declared types from the class files. These hold a String, but take no text. Their rows
say why: `[not settable: declared Object, not String]`.

- `Object o = "x"`
- `CharSequence cs`
- an `Object` field
- an `Object[]`'s element

### Locals the program never reads again

Peeky refuses to change a local that the program never reads after the stop, of any type. Its row
says `[not settable: not read again after this stop]`. Where Peeky can't follow the code, as at
another kind of stop, it makes the change. A note then says that a lambda or inner class that
captured the local before keeps the old value.

Peeky follows the method's bytecode from where the stop's line begins. A line breakpoint or a step
stops there. Peeky refuses when no path reads the local before storing into it again. For example:

- A local that a lambda or inner class captured before the stop. In
  `Supplier<String> lam = () -> cap;`, `lam` keeps its own copy.
- A constant that javac compiles into the code that uses it: `final int k = 5;`,
  `final String s = "x";`.

### Lines that several methods share

Several methods can share a line. Examples: a lambda body on its method's line, like
`.filter(item -> below(item))`, a static initializer's lambda, and nested lambdas. The line stops
in only one of them: java-debug picks the first in the class file. Peeky warns which one stops,
and which never do:

```
stops only in needsRestock there, never in lambda$needsRestock$0
```

To stop in another, put the breakpoint on a line of its own. Or run `session step --into` from
that line.

### Read Java values

In a launched Java program, a value that you capture or `session get` shows more:

| Value | Shows | Example |
| --- | --- | --- |
| An object | its first 8 fields | `Merge$Interval@143 {end=10, start=1}` |
| An array | its first 8 elements | `int[3]@144 [1, 2, 3]` |
| An `ArrayList` | its size and first 8 elements | `ArrayList@142 size=2 [...]` |
| An empty `ArrayList` | its raw fields | `size=0` |

- These are the debugger's reads of the value's own fields. No code runs in the program.
- A value with more than 8 fields or elements ends with `…`: `int[9]@57 [0, 1, 1, 1, 0, 1, 0, 1, …]`.
  Read the rest by name or index: `choice[8]`.
- Java `inspect` listings stay plain.
- An attached JVM shows plain values, to keep its pauses short.
- Read members through `this`: `this.total`.

Strings and boxed numbers:

- A `String` and a boxed number, such as `Integer`, `Long`, `Character` or `Boolean`, are values:
  `a.bezeichnung: String = "Schraube"`, `a.artikelNr: Integer = Integer@57 {value=1001}`.
- They show this way in a listing of their holder's fields, and of an `ArrayList`'s elements too.
- Their class's own fields, such as a String's `hash` and bytes, are not listed. A name still reads
  one, `a.artikelNr.value`. It is never settable.
- The object number tells two equal boxed values apart. `==` compares the objects.

Other details:

- `ArrayList` selectors index like arrays, within the list's `size`: `merged[0]`,
  `merged[-1].end`. This doesn't work if the backing array holds more than 512 slots.
- String and char fields show as Java literals: `name="say \"hi\""`, `quote='"'`. A String longer
  than 38 characters shows its first 37, then `…`.
- Object numbers, like `@143`, follow the order in which the debugger first touched the objects.
  So `queries diff` treats them like addresses in Java runs.
- A `step` from a block's last statement stops on its closing `}`, or on the loop's header. There
  the block's locals are gone, so read them before you step. A `get` of one then says that its
  block has ended ([COOKBOOK 5.2](../COOKBOOK.md#52-stepping-and-running-to-a-line)).

## 7.4 Java Tests Already Running (Gradle `--debug-jvm`, e.g. Venice)

### Attach to a Gradle test

To attach, Peeky connects to a JVM that is already running and waiting for a debugger. Here,
Gradle starts a Venice test with `--debug-jvm`:

```sh
./gradlew :internal:venice-test-common:integrationTests_32 \
  --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
```

To reproduce it, you need a Venice checkout at the commit the line numbers match, JDK 17, and on
Apple Silicon without Rosetta a native gRPC code generator. The
[Venice walkthrough](../JAVA_ATTACH.md#venice-walkthrough-verified-2026-10-14-apple-silicon-jdk-17)
lists them.

The config gives the JVM's `port` instead of a `main_class`. `class_roots` lists the directories
with the class files:

```json
{"bundle": "/abs/path/to/peeky/work/java-adapter", "port": 5005,
 "sources": ["internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java"],
 "class_roots": ["internal/venice-test-common/build/classes/java/integrationTest"]}
```

Then probe it as usual:

```sh
T=internal/venice-test-common/src/integrationTest/java/com/linkedin/venice/endToEnd/TestEmptyPush.java
peeky probe ~/workspace/venice --config attach.json --at $T:85 \
  --capture storeName,sendControlMessageDirectly --hits 2 --timeout 240
```

### What attaching changes

- Attaching never kills the JVM. Closing detaches.
- Provenance is labelled `attached`.
- `--arg` is refused, because the program is already running.
- Candidates and `verify` don't work: edits can't be verified this way, and `describe` lists
  neither command. Verify them with a launched target.
- Values show plain, to keep the JVM's pauses short.
- A session's `--max-pause` defaults to 10 s.

Details are in [JAVA_ATTACH.md](../JAVA_ATTACH.md), including breakpoints in production classes.
