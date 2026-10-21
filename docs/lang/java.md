# Peeky for Java

What is particular to Java targets, one rule per line, each with the [COOKBOOK](../COOKBOOK.md) section that has the details. Read [AGENT_GUIDE.md](../AGENT_GUIDE.md) first: the loop and the rules every language shares are there.

## Targets

- A Java target needs `--config <file.json>`: the adapter bundle, the sources and the main class, and optionally `arguments` (`{"bundle": "work/java-adapter", "sources": ["shop/Main.java"], "main_class": "shop.Main"}`); `describe`'s commands repeat it ([7.3](../COOKBOOK.md#73-java-launched)).
- The JVM starts slowly: add `--background` to a probe or verify that may outlast your tool's timeout, then `peeky job wait <id>` ([8](../COOKBOOK.md#8-slow-programs-and-tool-timeouts)); `--until hits` skips waiting for the program to exit ([2.6](../COOKBOOK.md#26-stop-right-after-the-last-hit)).
- A JVM already running with `--debug-jvm` is attached to, never killed: no `--arg`, no candidates or `verify`, plain values, and a session's `--max-pause` defaults to 10 s ([7.4](../COOKBOOK.md#74-java-tests-already-running-gradle---debug-jvm-eg-venice)).

## Reading values

- Objects and arrays show their first fields or elements (`Merge$Interval@143 {end=10, start=1}`), an `ArrayList` its size and first elements; read members through `this` (`this.total`) ([7.3](../COOKBOOK.md#73-java-launched)).
- Object numbers (`@143`) follow the order the debugger first touched objects: `queries diff` treats them like addresses, and `object_ref(value)` in SQL tells the same object from a copy within one run (across runs the numbers differ) ([4.6](../COOKBOOK.md#46-same-object-or-a-copy-java-python)).
- A line several methods share (a lambda on its method's line) stops in only one of them, and the probe says which; Java rows have no `stop` ([7.3](../COOKBOOK.md#73-java-launched), [2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- A `step` from a block's last statement stops on its closing `}`, where the block's locals are gone; `step --into` and `--out` skip JDK classes ([7.3](../COOKBOOK.md#73-java-launched), [5.2](../COOKBOOK.md#52-stepping-and-running-to-a-line)).

## Changing values

- Locals of type `byte`, `short`, `int`, `long`, `boolean`, `float`, `double` and `String` can be set (a `char` cannot); the value must fit the type exactly (`int` 3000000000 is refused) ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value), [7.3](../COOKBOOK.md#73-java-launched)).
- Scalar fields and array elements too (`--set current.end=10`, `arr[-1]=9`); not final or static fields, boxed values (`Integer`) or an `ArrayList`'s elements ([7.3](../COOKBOOK.md#73-java-launched)).
- A String local, field or element takes a text in double quotes (`--set 'name="apple"'`): a new String, which other references to the old one do not see; what a variable is declared decides (`Object o = "x"` takes none) ([7.3](../COOKBOOK.md#73-java-launched)).
- A local the program never reads after the stop is refused (a lambda's captured copy, a constant javac compiled into its uses) ([7.3](../COOKBOOK.md#73-java-launched)).

## Returns

- `--capture return` gives one `(return)` row per call of the `--at` line's function, any type, as JDI reports it; a method that threw has none ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- `session step --out` shows with the stop what the method returned (`(return): long = 1`), or a withheld row saying why, as `--capture return` does (a method that threw included); nothing where another stop interrupted the step first, or the program ended. `session get return` reads it again ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## The debugger's own failures

- java-debug sometimes loses events (issue #27): its event thread dying, or an attached JVM's stop it never reports, ends with `DEBUGGER_ERROR` at once ([ERRORS](../ERRORS.md)); a lost exit event is inferred from no threads being left, with a warning that the exit code is unknown (nothing to do).
