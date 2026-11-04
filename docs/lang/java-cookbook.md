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
  - [Build the adapter](#build-the-adapter)
  - [Which JDK runs what](#which-jdk-runs-what)
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
  - [Let Peeky start the test](#let-peeky-start-the-test)
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
mutable: root locals, fields (not final or static) and array elements (current.end, xs[0]) of type byte, short, int, long, char, boolean, float or double, and String ones; a char takes a char in single quotes ('+') or a code point (43), a String a text in double quotes (--set 'name="apple"'); never a boxed value (Integer), another object or an ArrayList's elements, nor a field a JDK class declares (a String's hash, an ArrayList's size) or an element of an array one holds. Field, char and String changes need an adapter bundle built by this version of Peeky (`peeky setup-java`). Rows mark what can change [settable]
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

A launched Java target needs `--config <file.json>`. The file names the sources and the main
class. `arguments` is optional. An attached target's config differs: see
[7.4](#74-java-tests-already-running-gradle---debug-jvm-eg-venice). The commands that `describe`
prints repeat the `--config`.

For example, `examples/java/pagination.json`:

```json
{"sources": ["demo/PaginationBug.java"], "main_class": "demo.PaginationBug", "arguments": []}
```

- `bundle` is optional. Without it, Peeky uses the adapter bundle that `peeky setup-java` built
  ([Build the adapter](#build-the-adapter)). With it, Peeky uses the bundle it names, relative to
  the config file.
- `javac` and `java` are optional. They name the JDK that builds and runs the program, else the one
  on `PATH`. A config's `java` runs the adapter too ([Which JDK runs what](#which-jdk-runs-what)).
- `sources` are relative to the target's directory. Declare every `.java` file the program needs.
  Maven or Gradle projects, and external classpaths, aren't supported.
- Use a standard package-relative layout. A source-root prefix works.
- Peeky reads non-preview class files up to Java 21. CI tests Java 17.
- Arguments may hold whitespace other than CR and LF, backslashes and single quotes, and may be
  empty. Embedded double quotes, CR or LF, and NUL are refused: the JDI connector's quoted-string
  contract can't carry them. Peeky takes at most 100 arguments, each at most 8,192 characters.

### Build the adapter

Build the adapter bundle once, with JDK 17 or later on `PATH`. The adapter always runs on that
JDK's `java`. JDK 17 is the one to use if a project needs an older JDK
([Which JDK runs what](#which-jdk-runs-what)). It works from the wheel too:

```sh
peeky setup-java
```

```text
Building the Java adapter in /home/me/.peeky/java-adapter: downloading java-debug and its jars (pinned by SHA-256) and compiling them can take a minute.
Built the Java adapter at /home/me/.peeky/java-adapter/823ca782d95c0373461f-f80de9f7 in 2.8 s: java-debug 6a597f2bcccd with Peeky's patches (eventhub-log-guard).
Java configs without "bundle" use it.
```

- Setup downloads java-debug's source and five jars, each checked against a pinned SHA-256. It
  needs access to GitHub and Maven Central. The downloads have 300 s in all.
- It builds java-debug and Peeky's launcher in a staging directory. A failure installs nothing,
  and leaves no staging behind.
- `--json` prints `bundle`, `built`, `default` (whether it is Peeky's own bundle), and `adapter`,
  the bundle's `adapter.json`.
- `--output DIR` builds into a new directory instead, outside Peeky's own `java-adapter`. Name it
  as the config's `bundle`.
- In a source checkout, `python scripts/setup_java_adapter.py --output DIR` does the same without
  Peeky installed. CI uses it.

Without `--output`, setup installs Peeky's own bundle, in `java-adapter` in Peeky's state
directory: `$PEEKY_STATE_DIR`, else `~/.peeky`.

- Each build is a new version directory there, such as `823ca782d95c0373461f-f80de9f7`. A file
  there, `current`, names the version in use. Setup switches it in one step, once the new version
  is complete.
- A session uses the version `current` named when it started, to its end.
- One setup runs at a time. Another waits for it, for up to 10 minutes.
- Run it again after you upgrade Peeky. A version that is up to date is kept. `--force` builds a
  new one anyway.
- Setup never removes a version, since a session may still use it. It says how many older ones
  there are.
- `peeky setup-java --prune` removes every version but the current one. A session still using a
  removed version fails its next step with `PROVENANCE_CHANGED`. Start it again.
- `--prune` lists the versions it removed, and each it couldn't, with why. If any is left, it
  exits with status 2. `--json` has them in `pruned` and `failed`.
- Setup removes what an interrupted setup left in staging, by itself.

Peeky runs its own bundle only where no other user can change it. A bundle's `adapter.json`
lists its files' hashes, so it vouches only for itself.

- The state directory, `java-adapter` and the version must be directories you own, not links,
  with mode 0700. No directory above them may be writable by another user.
- `current` must be a file of yours, with mode 0600 exactly, that names a version.
- Otherwise Peeky refuses before anything runs: `UNSUPPORTED`, with
  `Peeky's own Java adapter can't be used: …`. The message names the path, and what to do. So
  does a path that can't be opened or read.
- Setup makes the state directory and `java-adapter` mode 0700 itself.
- Peeky never takes `/` or your home directory for its state directory, whatever their mode. With
  `PEEKY_STATE_DIR` set to one of them, it refuses.

A bundle records its recipe, and that recipe's fingerprint, in `adapter.json`. The recipe is
the text of each patch, and the hashes of the launcher, the dependency lock and the notices. It
is also the compiler flags, javac's version text, and the file `java` is: its real path and
SHA-256.

- Setup compiles with `--release 17`. So the bundle's classes run on JDK 17 or later. That is the
  contract with the JDK that runs it. An older JVM refuses the classes itself: the debugger fails
  to start, with `DEBUGGER_ERROR` and `Debugger adapter closed`.
- Before it runs any bundle, one a config names too, Peeky checks the bundle's own content. That
  is its files' hashes, and its recipe but for the JDK. It never runs the JDK to ask it. It
  checks that the bundle's `java` is the file setup recorded
  ([Which JDK runs what](#which-jdk-runs-what)).
- A bundle from another Peeky is refused before anything runs. That is `UNSUPPORTED`, with
  `… is not what this Peeky's setup builds (…)` and the rebuild command. A bundle built before
  Peeky recorded its `java` file is refused so too: run `peeky setup-java` again.
- Setup asks javac its version, and hashes `java`, each time it runs. It keeps a version only if
  the whole recipe is the one it would build now, the JDK's part included. Otherwise it builds a
  new one.
- The patches are applied to java-debug before compiling, and listed in `adapter.json`, under
  `patches`. The upstream licence and notices are in the bundle's `notices`.
- One patch, `eventhub-log-guard`, keeps java-debug's event thread alive. Without it, that thread
  could die and stop reporting every stop and exit
  ([JAVA_ATTACH.md](../JAVA_ATTACH.md#root-cause-java-debugs-event-thread-died)).
- Peeky also checks each patch in the compiled classes: the patched `EventHub.class` calls
  `Logger.isLoggable`. A bundle without it is refused with `UNSUPPORTED`, as one that predates
  Peeky's java-debug fixes.

### Which JDK runs what

The adapter and the program may run on different JDKs.

- The adapter runs on the `java` that `peeky setup-java` built its bundle with. The bundle's
  `adapter.json` records it, as `command`'s first word. Its recipe records the file that `java`
  is: its real path, with links followed, and its SHA-256.
- Where that `java` is a JDK's own (below), the recipe also records two files of its JDK. They are
  the `release` file and the JVM's library, `lib/server/libjvm`, each by path and SHA-256.
- The program builds with `javac` and runs on `java`. Without them in the config, both come from
  `PATH`: the project's JDK.
- So with JDK 11 first on `PATH`, the program builds and runs on JDK 11. The adapter runs on the
  bundle's JDK.
- An attached JVM runs on the JDK that started it. An attach config's `java` names only the
  adapter's.
- Each run says which `java` ran what, in a `java:` line, and in `--json` as `java`. That holds
  `program` and `adapter`, each with its `path` and `version` (below).

```text
java: the program on /usr/lib/jvm/java-11-openjdk-arm64/bin/java (11.0.32.1), the adapter on /usr/lib/jvm/java-17-openjdk-arm64/bin/java (17.0.20.1)
```

Before every operation, Peeky checks the bundle's `java`, whichever `java` runs it:

- It must still lead to the real path the recipe records. A link turned to another JDK, by
  `update-alternatives` for example, is refused.
- Its bytes, and those of the JDK files the recipe records, must still be the ones recorded. A JDK
  update, or a replaced file, is refused before it runs. Run `peeky setup-java` again then: setup
  builds anew for another `java`.
- Where `PATH` names the bundle's `java`, nothing more is checked, as before.

Where `java` on `PATH` is another one, Peeky picks the bundle's `java` itself. Then both `java`s
must be a JDK's own `bin/java`:

- a native executable, not a script: an ELF executable or shared object, or a Mach-O executable,
  universal or not. A Java class file, which starts as a universal file does, isn't one;
- in a JDK 9 or later, with its `release` file naming `JAVA_VERSION`, `lib/modules` and
  `lib/server/libjvm`. JDK 8 is refused there.

Peeky reads each JDK's version from its `release` file, and never from what a JVM prints.

- A wrapper, a shim or macOS's `/usr/bin/java` can print one JDK and run another, by
  `JAVA_HOME`. Such a `java` is refused, with `UNSUPPORTED`, and the message says why. It names
  the JDK a wrapper says it runs: put that JDK's `bin` directory first on `PATH` instead.
- The bundle's `java` must also be one whose JDK files setup recorded. Its JDK must name the same
  `release` and `lib/server/libjvm` files now.
- The bundle's `java`, its JDK's `release`, `lib/server/libjvm` and `lib/modules`, and each
  directory above them, must not be writable by a user other than you or root. Otherwise another
  user could change one after Peeky hashed it. A JDK under `/usr/lib/jvm`, owned by root, passes.
- The `version` shown comes from the `release` file. Where there is none, it is what the build's
  `java -version` printed, shown as `(says 17.0.19)`.

An adapter on JDK 19 or later launches programs only on JDK 19 or later. Its debugger starts the
JVM with an option, `includevirtualthreads`, that older JVMs refuse. Peeky refuses such a launch
before it starts, with `UNSUPPORTED`, by the JDKs' `release` files.

- So build the bundle with JDK 17 if a project needs an older JDK. A bundle built with JDK 17
  launches programs on JDK 11, 17 and 21.
- Setup takes `java` and `javac` from `PATH`, not from `JAVA_HOME`. Put the JDK's own `bin`
  directory first:

```sh
PATH=/usr/lib/jvm/java-17-openjdk-arm64/bin:$PATH peeky setup-java
```

A config's `java` runs both the adapter and the program. So it must be the bundle's own `java`.
Another is refused before anything runs, with `UNSUPPORTED`:

```text
error [UNSUPPORTED]: The Java adapter runs only on the java that its bundle was built with: /usr/bin/java (openjdk version "21.0.12.1" 2026-08-18), for the bundle at /root/.peeky/java-adapter/2904f5de2ae5ff7c7986-fb91e4f2. The config's java is /usr/lib/jvm/java-17-openjdk-arm64/bin/java, another one. Drop java from the config: then the adapter runs on /usr/bin/java, and the program on the java on PATH. Or set the config's java to /usr/bin/java. Or rebuild the bundle with the JDK 17 or later you want: put its bin directory first on PATH, then run `peeky setup-java`
hint: Nothing was launched or attached. Drop the config's java or set it to the one the message names, or rebuild with `peeky setup-java`.
```

The same message says when the bundle's `java` is gone, leads elsewhere or has changed. Then
rebuild the bundle.

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
- Field, char and String changes need an adapter bundle built by this version of Peeky, with
  `peeky setup-java`.

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
- Field changes need an adapter bundle built by this version of Peeky, with `peeky setup-java`.
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

This needs an adapter bundle built by this version of Peeky, with `peeky setup-java`. An older
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
- This needs an adapter bundle built by this version of Peeky, with `peeky setup-java`. An older
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

### Let Peeky start the test

Give the command that runs the failing test after `--test --`. Peeky runs it, attaches to the
JVM that runs the tests, probes, and lets the test end
([ADR 0009](../adr/0009-first-question.md#2-java-peeky-starts-the-test-finds-its-jvm-and-proves-it-attached-to-it)):

```sh
peeky probe . --at core/src/main/java/pkg/Foo.java:LINE --capture a,b \
  --test -- mvn test -o -pl core -Dtest='FooTest#bar'
# Gradle: --test -- ./gradlew test --tests pkg.FooTest
# A JVM of your own: --test -- java -cp target/classes:LIBS pkg.Main ARGS
# An annotation processor (Lombok): --test -- javac -cp LIBS -processor P -d out A.java
```

- **What Peeky adds.** JDWP's agent, suspended and on a loopback port the JVM picks, and a
  random `-Dpeeky.launch=` word, to the JVM that runs the tests only. Maven gets them through
  `-Dmaven.surefire.debug` (and failsafe's), so surefire's forked JVM has them, not Maven's.
  Gradle gets an init script that adds them to each `Test` task's `jvmArgs`, and changes
  nothing else.
  `java` gets them as its first options, and `javac` as `-J` words.
- **Which JVM.** The one of your user whose JVM options hold both words exactly, and that
  listens on one loopback port, read from the system (`/proc`, or `lsof` on macOS). Port 5005,
  and a JVM another run left waiting, play no part.
- **The proof.** Once the debugger has attached, and before the JVM runs, the JVM must hold a
  connection at its port whose other end is the debugger Peeky started. Otherwise nothing is
  read (`UNSUPPORTED`).
- **Classes.** Maven's `target/classes` and `target/test-classes`, or Gradle's
  `build/classes/<lang>/<set>`, in the `--at` file's module; for `java` and `javac`, the
  classpath's directories inside the root. The `--at` file is the attach's one source.
- **One test JVM at a time.** A build that runs a second test JVM beside the first (Gradle's
  `maxParallelForks`, Maven's `forkCount`) is refused, and stopped: Peeky attaches to one, and
  the line may run in another. Select one test class (`--tests NAME` for Gradle, `-Dtest=NAME`
  for Surefire, `-Dit.test=NAME` for Failsafe), or configure that test task or plugin with one
  fork.
- **The test runs on.** After the probe, Peeky waits for the build, then reports its exit code
  and last lines (`test` in `--json`); the whole log is in Peeky's state directory.
- **Time.** `--timeout` defaults to 300 s here, and covers the build, the probe and the build's
  end: the probe ends 15 s before the deadline, and at the deadline Peeky stops the build
  (SIGTERM, then SIGKILL 5 s later). Only that may outlast `--timeout`, by about 10 s at most. A
  `--timeout` of 35 s or less is refused.
- **Refused before anything runs:** a command with its own debug option (`--debug-jvm`,
  `-Dmaven.surefire.debug`, `-agentlib:jdwp`), `-DforkCount=0`, Ant and other tools (give the
  `java` command the test runs), and sessions: a session's test would outlive the command.
- **`NO_STOP`** also comes from a build that ended before any test JVM waited: a `-Dtest` filter
  that names no test, a compile error, or a pom whose surefire doesn't fork. The `test` lines say
  which.
- A second test JVM that waits for a debugger later (`forkEvery`, `reuseForks=false`) stops the
  build, with a warning: select one test class.
- A refusal after the test started (several ports, several JVMs at once, no classes where Peeky
  looks, an attach it can't prove) says `Nothing was read; Peeky stopped the test.`

### Attach to a Gradle test

To attach, Peeky connects to a JVM that is already running and waiting for a debugger. Here,
Gradle starts a Venice test with `--debug-jvm`:

```sh
./gradlew :internal:venice-test-common:integrationTests_32 \
  --tests 'com.linkedin.venice.endToEnd.TestEmptyPush.testEmptyPushWithZstdWithDictCompression' --debug-jvm
```

To reproduce it, you need a Venice checkout at the commit the line numbers match, JDK 17, and on
Apple Silicon, Rosetta 2. The
[Venice walkthrough](../JAVA_ATTACH.md#venice-walkthrough-verified-2026-10-14-apple-silicon-jdk-17)
lists them.

The config gives the JVM's `port` instead of a `main_class`. `class_roots` lists the directories
with the class files. Without `bundle`, it uses the adapter bundle `peeky setup-java` built:

```json
{"port": 5005,
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
