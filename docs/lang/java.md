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

- A Java target needs `--config <file.json>`. The file names the adapter bundle, the sources and
  the main class. `arguments` is optional. For example:
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
- To debug a JVM already running with `--debug-jvm`, attach to it. Peeky never kills an attached
  JVM ([Java 7.4](java-cookbook.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice)).
  With an attached JVM:
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

- These locals can be set: `byte`, `short`, `int`, `long`, `boolean`, `float`, `double`, `char`
  and `String`. The program must read the local after the stop
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value),
  [Java 7.3](java-cookbook.md#73-java-launched)).
- The value must fit the type exactly. For example, `int` 3000000000 is refused
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Set a `char` with a char in single quotes, `--set "op='+'"`, or with a code point, `op=43`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Scalar fields and array elements can be set too: `--set current.end=10`, `arr[-1]=9`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Final and static fields, boxed values such as `Integer`, and an `ArrayList`'s elements can't be
  set ([Java 7.3](java-cookbook.md#73-java-launched)).
- A field that a JDK class declares is never settable, inherited or not. Examples: a String's
  `hash`, an `ArrayList`'s `size` ([Java 7.3](java-cookbook.md#73-java-launched)).
- A JDK class is one in a JDK module, `jdk.jshell` too. On Java 8, it is one that the JDK's own
  class loaders loaded. A class's package never decides
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- Such a field's row names its class:
  `[not settable: declared by java.lang.String (the JDK's own state)]`. A change to it is refused,
  naming the class. Where the class files show the field, the refusal
  comes before the JVM starts ([Java 7.3](java-cookbook.md#73-java-launched)).
- Field changes need an adapter bundle built by this version of `scripts/setup_java_adapter.py`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- An element of an array that a JDK class's field holds can't be set, under any name. Nor can an
  element of an array inside such an array, an `Object[]` too
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- The only exception is the backing array of `ArrayList`, `Vector`, `ArrayDeque` and
  `Arrays.asList`. Its elements can be set where the list, and any iterator or sublist of it, is
  the program's ([Java 7.3](java-cookbook.md#73-java-launched)).
- A row's `[settable]` mark goes by its path only. A local or field that refers to a String's
  bytes may be marked, but the change is refused when it is made
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- To change an array element, Peeky asks the JVM what refers to the array. It waits at most 2 s
  for the answer, and stays within the session's pause budget, `--max-pause`
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- A String local, field or element takes a text in double quotes: `--set 'name="apple"'`. It gets
  a new String. Other references to the old String don't see it
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- What a variable is declared decides. `Object o = "x"` holds a String but takes no text
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- A local that the program never reads after the stop is refused. Examples: a lambda's captured
  copy, and a constant that javac compiled into its uses
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- `describe` shows each local's declared type, and lists the kinds that `set` changes
  ([Java 7.3](java-cookbook.md#73-java-launched)).
- A `--set` that the class files rule out is refused before the JVM starts, with exit status 2.
  That is: no such local at the line, a type or value that doesn't fit, or a local never read
  after it ([Java 7.3](java-cookbook.md#73-java-launched)).

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
  says the exit code is unknown. You need not do anything.
