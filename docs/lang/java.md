# Peeky for Java

This page lists what is different for Java targets, one rule per line. Each rule links to its
details, in the [Java cookbook](java-cookbook.md) or the [COOKBOOK](../COOKBOOK.md). Read
[AGENT_GUIDE.md](../AGENT_GUIDE.md) first: it has the steps and rules every language shares.

## Words used here

| Word | Meaning |
| --- | --- |
| java-debug | Microsoft's debugger for Java. Peeky drives it. |
| JDI | The Java Debug Interface: the JDK's API that java-debug uses to read and change a JVM. |
| adapter bundle | A directory with java-debug, built with Peeky's patches. `scripts/setup_java_adapter.py` builds it. |
| launch | Peeky starts the JVM itself, from the config's `main_class`. |
| attach | Peeky connects to a JVM that is already running and waiting for a debugger. |
| object number | The `@143` in `Merge$Interval@143`. It names one object within one run. |
| JDK class | A class of the Java runtime, such as `java.lang.String`. [Java 7.3](java-cookbook.md#73-java-launched) says which classes count. |

## Set up a Java target

- A launched Java target needs `--config <file.json>`. The file names the adapter bundle, the
  sources and the main class. `arguments` is optional. For example:
  `{"bundle": "work/java-adapter", "sources": ["shop/Main.java"], "main_class": "shop.Main"}`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- The commands that `describe` prints repeat the `--config`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Build the adapter bundle with `scripts/setup_java_adapter.py`. A bundle built before Peeky's
  java-debug patches is refused before anything runs: `UNSUPPORTED`, exit status 2, no table.
  Rebuild it, and point the config's `bundle` at the new one ([ERRORS](../ERRORS.md)).
- The JVM starts slowly. If a probe or `verify` may outlast your tool's timeout, add
  `--background`. Then run `peeky job wait <id>`
  ([8](../COOKBOOK.md#8-slow-programs-and-tool-timeouts)).
- Add `--until hits` to skip waiting for the program to exit
  ([2.6](../COOKBOOK.md#26-stop-right-after-the-last-hit)).
- To debug a JVM on another host, run the worker there with `--java-config`, or with
  `--java-attach-config` for an attached JVM. Then add `--worker URL`, and pass your own `--config`
  of the same kind ([REMOTE](../REMOTE.md#java-on-the-worker-host)).
- Through a worker, every file in the config's `sources` is compared with yours before the JVM
  launches or is attached. One that differs is refused by name
  ([REMOTE](../REMOTE.md#source-identity)).
- To debug a JVM already running with `--debug-jvm`, attach to it. Peeky never kills an attached
  JVM ([Java 7.4](java-cookbook.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice)).
  With an attached JVM:
  - The config gives `port` and `class_roots` instead of `main_class`.
  - `--arg` is refused.
  - Candidates and `verify` don't work. Try and verify edits with a launched target.
  - Values show plain, without their fields, to keep the JVM's pauses short.
  - A session's `--max-pause` defaults to 10 s.

## Read values

- An object shows its first fields: `Merge$Interval@143 {end=10, start=1}`. An array shows its
  first elements. An `ArrayList` shows its size and first elements
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Read a member through `this`: `this.total` ([Java 7.3](java-cookbook.md#73-java-launched)).
- A `String` and a boxed number show as values: `"Schraube"`, `Integer@57 {value=1001}`. Their
  class's own fields are not listed. A name still reads one: `a.artikelNr.value`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Write a name as javac keeps it, non-ASCII too: `größe.zähler`
  ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- `größe` in NFC and in NFD, two Unicode spellings, are two different locals. Select each by its
  own spelling. If two rows' names are equal under Unicode's NFKC form, `inspect` marks each row
  with how it is written ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- Object numbers follow the order in which the debugger first touched the objects. So
  `queries diff` treats them like addresses
  ([Java 4.6](java-cookbook.md#46-same-object-or-a-copy-java-python)).
- To tell the same object from a copy, compare `object_ref(value)` in SQL. Compare only within one
  run: across runs the numbers differ
  ([Java 4.6](java-cookbook.md#46-same-object-or-a-copy-java-python)).
- Several methods can share a line, such as a lambda on its method's line. The line stops in only
  one of them, and the probe says which ([Java 7.3](java-cookbook.md#73-java-launched)).
- Java rows have no `stop` number ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- A `step` from a block's last statement stops on the closing `}`. There the block's locals are
  gone, so read them before you step ([Java 7.3](java-cookbook.md#73-java-launched)).
- `step --into` and `step --out` skip the JDK's packages, such as `java.*` and `javax.*`
  ([Java 5.2](java-cookbook.md#52-stepping-and-running-to-a-line)).

## Change values

### What can be set

- A local can be set only if both of these hold
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value),
  [Java 7.3](java-cookbook.md#what-can-be-set)):
  - Its type is `byte`, `short`, `int`, `long`, `boolean`, `float`, `double`, `char` or `String`. A
    `String` local must be declared `String`.
  - The program reads it after the stop. So a lambda's captured copy is refused, and so is a
    constant that javac compiled into its uses
    ([Java 7.3](java-cookbook.md#locals-the-program-never-reads-again)).
- A field or an array element of those types can be set too, unless it is listed under
  [What can't be set](#what-cant-be-set). Examples: `--set current.end=10`, `arr[-1]=9`. A `String`
  field or element must be declared `String` too
  ([Java 7.3](java-cookbook.md#change-a-field-or-an-array-element)).
- A row is marked `[settable]` where `set` would change it there. A row of one of those types that
  isn't marked says why: `[not settable: not read again after this stop]`, `final`, or
  `in a caller's frame` ([Java 7.3](java-cookbook.md#what-can-be-set)).
- A `--set` that the class files rule out is refused before the JVM starts, with exit status 2.
  That is: no such local at the line, a type or value that doesn't fit, or a local never read after
  it ([Java 7.3](java-cookbook.md#changes-refused-before-the-run)).
- `describe` shows each local's declared type, and lists the kinds that `set` changes
  ([Java 1.1](java-cookbook.md#see-each-names-declared-type)).

### How to write the value

- The value must fit the type exactly. For example, `int` 3000000000 is refused
  ([Java 7.3](java-cookbook.md#values-must-fit-the-type)).
- For a `char`, give a char in single quotes, `--set "op='+'"`, or a code point, `op=43`
  ([Java 7.3](java-cookbook.md#set-a-char)).
- For a `String`, give a text in double quotes: `--set 'name="apple"'`. The variable gets a new
  String. Other references to the old String don't see it
  ([Java 7.3](java-cookbook.md#set-a-string)).
- What a variable is declared decides. `Object o = "x"` holds a String but takes no text
  ([Java 7.3](java-cookbook.md#the-declared-type-decides)).
- Field, char and String changes need an adapter bundle built by this version of
  `scripts/setup_java_adapter.py` ([Java 7.3](java-cookbook.md#what-can-be-set)).

### What can't be set

- Final and static fields, boxed values such as `Integer`, and an `ArrayList`'s elements. To change
  an element of a list, change one of its fields: `merged[0].end=10`
  ([Java 7.3](java-cookbook.md#what-can-be-set)).
- A field whose name a static field hides, such as a subclass's, or another instance field shares.
  java-debug would write the other field. A static field that this field itself hides, such as a
  superclass's, doesn't matter ([Java 7.3](java-cookbook.md#change-a-field-or-an-array-element)).
- A field that a JDK class declares, inherited or not. Examples: a String's `hash`, an `ArrayList`'s
  `size` ([Java 7.3](java-cookbook.md#fields-the-jdk-declares)).
- A JDK class is one in a JDK module, `jdk.jshell` too. On Java 8, it is one that the JDK's own
  class loaders loaded. A class's package never decides
  ([Java 7.3](java-cookbook.md#fields-the-jdk-declares)).
- Such a field's row names its class:
  `[not settable: declared by java.lang.String (the JDK's own state)]`. A change to it is refused,
  naming the class. Where the class files show the field, the refusal comes before the JVM starts
  ([Java 7.3](java-cookbook.md#fields-the-jdk-declares)).
- An element of an array that a JDK class's field holds, under any name. Nor an element of an array
  inside such an array, an `Object[]` too ([Java 7.3](java-cookbook.md#arrays-the-jdk-holds)).
- The only exception is the backing array of an `ArrayList`, `Vector`, `ArrayDeque` or
  `Arrays.asList` list, where the list itself is the program's. That array holds the program's own
  elements, so it doesn't count as the JDK's. For example, the rows of the program's
  `ArrayList<int[]>` can change: `rows[0][1]=9`
  ([Java 7.3](java-cookbook.md#which-lists-arrays-are-the-programs)).
- A list's iterators and sublists don't own it. But whoever holds one of them holds the list
  ([Java 7.3](java-cookbook.md#which-lists-arrays-are-the-programs)).
- Before it marks an element, Peeky asks the adapter what holds its array, under any name, as a
  change asks again ([Java 7.3](java-cookbook.md#arrays-the-jdk-holds)).
- Where a JDK class's field holds it, the row names the holder:
  `[not settable: its array is in a list held by java.util.Collections$UnmodifiableList.list (the JDK's own state)]`.
- A line breakpoint stops only its own thread. Another thread could make a JDK class hold the
  array before the change. So there, no element is promised:
  `[maybe settable: other threads are running; set checks again]`
  ([Java 7.3](java-cookbook.md#arrays-the-jdk-holds)).
- An element is marked `[settable]` only where every thread is suspended and nothing holds its
  array. Where the adapter can't tell in time, the row says `[maybe settable: …]`. So does a
  bundle built before this check: rebuild it.
- A change to an element suspends every thread while it checks and writes it
  ([Java 7.3](java-cookbook.md#arrays-the-jdk-holds)).
- To change an array element, Peeky asks the JVM what refers to the array. It waits at most 2 s for
  the answer, and stays within the session's pause budget, `--max-pause`
  ([Java 7.3](java-cookbook.md#when-peeky-cant-tell-who-holds-an-array)).

## See what a method returned

- `--capture return` gives a `(return)` row for each hit. It holds what that hit's call of the
  `--at` line's method returned, of any type, as JDI reports it
  ([2.9](../COOKBOOK.md#29-what-a-function-returned),
  [Java 2.9](java-cookbook.md#29-what-a-function-returned)).
- Two cases get no value, and the row says why:
  - A method that threw.
  - A hit whose line stops again at the same depth before its call returns, such as a loop's next
    pass. Java has no watch on the caller to prove that stop is in the same call. Probe the
    method's first line instead.
- `session step --out` shows, with its stop, what the method returned: `(return): long = 1`.
  Without a value, a withheld row says why, as for `--capture return`. That includes a method
  that threw ([2.9](../COOKBOOK.md#29-what-a-function-returned),
  [Java 2.9](java-cookbook.md#29-what-a-function-returned)).
- `step --out` shows no return if another stop interrupted the step first, or the program ended.
- `session get return` reads the returned value again.

## When java-debug itself fails

- java-debug sometimes loses events. If its event thread dies, Peeky ends with `DEBUGGER_ERROR`
  at once. The same happens if an attached JVM stops and java-debug never reports it. Start again
  ([ERRORS](../ERRORS.md)).
- If java-debug loses the exit event, Peeky infers the exit when no threads are left. A warning
  says the exit code is unknown. You need not do anything. The output is then not proven whole:
  `output_complete: false`, with `program_ended: true`
  ([2.6](../COOKBOOK.md#26-stop-right-after-the-last-hit)).
