# Peeky Cookbook: Python

**[AGENT_GUIDE.md](../AGENT_GUIDE.md) and [python.md](python.md) cover most tasks.** This cookbook
is for details, which they cite by section number.

This cookbook covers what is different for Python. The [COOKBOOK](../COOKBOOK.md) has the recipes
every language shares. Each section here has the number and title of the COOKBOOK section it adds
to. For a one-page summary of the Python rules, see [python.md](python.md).

Peeky debugs Python with debugpy, Python's debugger.

## Contents

- [Setup](#0-setup)
  - [Which interpreter runs the program](#which-interpreter-runs-the-program)
- [1.1 What Can I Ask?](#11-what-can-i-ask)
  - [See which module globals a function reads](#see-which-module-globals-a-function-reads)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
  - [Read a dict entry](#read-a-dict-entry)
- [2.8 Change a Value, Then Restore It](#28-change-a-value-then-restore-it)
  - [Change a value in one of two lambdas on a line](#change-a-value-in-one-of-two-lambdas-on-a-line)
  - [Restore a value with `@before`](#restore-a-value-with-before)
- [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python)
  - [Tell the same object from a copy](#tell-the-same-object-from-a-copy)
- [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish)
  - [Read a module global](#read-a-module-global)
  - [List the globals, and what that runs](#list-the-globals-and-what-that-runs)
  - [Read members that debugpy groups](#read-members-that-debugpy-groups)
  - [Read a value inside a loop](#read-a-value-inside-a-loop)
- [6.6 See What a Fix Does Before Making It](#66-see-what-a-fix-does-before-making-it)
  - [Where a Python candidate runs](#where-a-python-candidate-runs)
  - [What happens to repositories](#what-happens-to-repositories)
  - [What else is copied, linked or left out](#what-else-is-copied-linked-or-left-out)
  - [When the result warns that nothing above the root was copied](#when-the-result-warns-that-nothing-above-the-root-was-copied)

## 0. Setup

Read [COOKBOOK 0](../COOKBOOK.md#0-setup) first.

### Which interpreter runs the program

A Python program runs on its project's interpreter, so it imports what the project installs. Peeky
installed as a tool, with pipx or uv, can't import those itself. Peeky takes the first of these:

| Interpreter | When |
| --- | --- |
| `--python PATH` | Given. A name on `PATH` works too: `--python python3.12` |
| The root's `.venv/bin/python`, then `venv/bin/python` | The directory holds `pyvenv.cfg`, as a virtual environment does |
| `$VIRTUAL_ENV/bin/python` | A virtual environment is active, and it isn't Peeky's own |
| The interpreter running `peeky` | Otherwise, or with `--python peeky` |

- `probe`, `session start`, `verify` and `describe` take `--python`. A worker takes it as
  `peeky-worker --python`, and a client's `--python` is refused with `--worker`
  ([WORKER.md](../WORKER.md#start-a-python-worker)).
- Peeky looks in the root only, never above it. For `peeky probe src/app.py`, the root is `src`,
  so a `.venv` beside `src` isn't used. Give the project as the target instead:
  `peeky probe . --at src/app.py:12`. Or pass `--python .venv/bin/python`.
- The output names a `.venv` above the root that it didn't use. Where that is Peeky's own, as
  when Peeky runs from a checkout's `.venv`, the output says Peeky's own is that environment.
- A shim, such as pyenv's `python`, picks its Python from the directory it runs in. Peeky asks it
  in the root, where debugpy starts the program. debugpy then runs the file it picked, which the
  output names: `python: ~/.pyenv/shims/python (runs …/3.12.4/bin/python3.12), …`.
- A wrapper that changes how Python runs is refused, such as `exec python -O "$@"` or one that
  exports `PYTHONPATH`. debugpy runs the file it starts, which would lose those settings. Peeky asks
  that file too, run alone, and refuses when its flags (`sys.flags`) or import path (`sys.path`)
  differ. Pass that file as `--python`, and set the options in the environment.

The output says which interpreter ran the program:

```
python: /home/me/shop/.venv/bin/python, Python 3.12.4 (the root's .venv)
```

With `--json`, a probe, `session start`, `describe` and `verify` give `python`:

| Key | Holds |
| --- | --- |
| `interpreter` | The path chosen. A virtual environment's `python` is a link, which isn't followed |
| `executable` | The file it said it runs, where that differs, as a pyenv shim's pick does. debugpy runs that file |
| `version` | Its version, such as `3.12.4` |
| `optimize` | The program's optimization level, 0 to 2, from `PYTHONOPTIMIZE`. The text shows `-O` or `-OO` |
| `chosen` | `--python`, `.venv`, `venv`, `VIRTUAL_ENV` or `peeky` |
| `resolved` | Where the path's links lead, when they lead elsewhere. It is only reported |
| `unused` | A virtual environment above the root, named but not used |
| `environment` | Peeky's own virtual environment, where that is the one above the root |

A probe's `target_identity` holds the same. So a run on another interpreter is never compared as
the same program.

The interpreter must be CPython 3.11 or later. Peeky reads the program's code with it, and it reads
only what 3.11 and later compile. Another one is refused before anything runs, with
`UNSUPPORTED`:

```
[UNSUPPORTED] Peeky runs Python programs on CPython 3.11 or later, and /usr/bin/python3 (--python) is CPython 3.9.6: …
```

The project needs no debugpy. Peeky's own debugpy starts the program on the interpreter. debugpy's
compiled speedups are built for Peeky's Python version only. On another version, debugpy uses its
pure-Python tracer instead, which is slower.

#### How Peeky reads the code

Peeky reads some of a program's code without running it:

- which scope a name missing from the locals is read from;
- a loop's locals that this pass hasn't assigned yet;
- the lines and scopes that `describe` lists;
- a function's first line, which a `@before` restore watches;
- the lambdas, generator expressions and comprehensions on a line, which the debugger names alike.

Bytecode and line tables differ between Python versions, builds and optimization levels. So the
program's interpreter makes these readings, at the program's level. Peeky asks the interpreter for
it as the program runs, with the program's environment: `PYTHONOPTIMIZE=1` leaves out each
`assert`. Every reading compiles at that level, whatever Peeky's own is.

Peeky reads in its own process only where the interpreter is Peeky's own file, and reports
Peeky's compiler and optimization level. Otherwise a helper process of that interpreter reads them,
with Peeky's code by its path. A reading that depends on the environment, what an import finds,
is always the helper's. No reading made by one Python version is applied to another.

Peeky asks the interpreter once per launch, as the program runs: in the root, with the program's
environment. Its answer is a record of its own, after a newline and with a nonce of the call's.
So a `.pth` file's output, or an exit handler's, can't be taken for it. The helper's answers are
framed the same way. Peeky notes the file it runs, and each reading checks that file again. A
`.venv` made again at the same path is asked again at the next launch. The launch's readings and
their helper end with it.

If the interpreter can't read, Peeky claims nothing. A module global is withheld, with why. A
loop's local gets no note. Where a line's lambdas can't be read, a warning says their stops may be
counted together. A reading that failed isn't remembered: the next one asks again.

The interpreter and its environment are your project's code, as the program is. They get the same
environment as the program, without the worker's token. An environment's `.pth` files run in the
helper too.

## 1.1 What Can I Ask?

Read [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask) first.

### See which module globals a function reads

`describe` lists the module globals that a function's compiled code reads, within the limits
below, and marks them `(module global)`. Nothing in the program runs to find them. For this
`sessions.py`:

```python
MAX_GAP = 30


def sessionize(gaps):
    count = 0
    for gap in gaps:
        if gap > MAX_GAP:
            count += 1
    return count


print(sessionize([5, 40, 10, 31]))
```

```sh
peeky describe sessions.py
```

```
...
names in <module>: MAX_GAP, sessionize
names in sessionize (lines 4-9): count, gap, gaps, MAX_GAP (module global)
...
```

With `--json`, the function's scope lists them under `globals`. A probe that captures one marks its
row `[module global]`:

```sh
peeky probe sessions.py --at 7 --capture gap,MAX_GAP
```

```
hit  phase     line  function    selector  type  value
1    observed  7     sessionize  gap       int   5
1    observed  7     sessionize  MAX_GAP   int   30  [module global]
...
```

What counts as a module global the function reads:

- A global read by the function's generator expressions and comprehensions counts.
- Only a global the module binds by name counts. `MAX_GAP = 30` counts; `globals()["x"] = 1`
  doesn't.
- A read in code the compiler drops, such as an `if False:` block, doesn't count.

## 2.1 Fields and Indexes

Read [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes) first.

### Read a dict entry

Select a dict entry by its key: `counts['a']`, not `counts.a`. If you write a dot, the row says
which selector to use and lists the keys:

```sh
peeky probe examples/corpus/word_count/program.py --at 5 --capture counts.a
```

```
hit  phase     line  function     selector  type  value
1    observed  5     count_words  counts.a        <'a' not found: 'counts' (dict) is a mapping and 'a' is one of its keys; select it with ['a'], not .a (keys: 'a', 'b')>
...
```

## 2.8 Change a Value, Then Restore It

Read [COOKBOOK 2.8](../COOKBOOK.md#28-change-a-value-then-restore-it) first.

### Change a value in one of two lambdas on a line

debugpy gives two lambdas on one line the same name, `<lambda>`, and no column. So Peeky can't tell
their stops apart:

- `--set-at LINE@HIT` counts the stops of both lambdas together, and a warning says so.
  `LINE@all` makes the change at each stop, in either lambda.
- A change in one of them says `one of 2 on the line the debugger names alike`.

Lambdas on separate lines are counted separately.

### Restore a value with `@before`

`--set-at LINE@HIT name=@before` writes back what a local held before the probe's change at the
same hit ([COOKBOOK 2.8](../COOKBOOK.md#skip-a-statement-restore-with-before)). In Python:

- It writes back an `int`, a `float`, `True`, `False` or `None` whose text reads back as the same
  value. Writing it makes an equal value. An int past 256 or a float is a new object, which `is`
  tells apart. A text, a list or another object is refused.
- Peeky watches the first statement of the function holding the restore line, after its
  docstring, for each new call. At module level, it watches the module's first statement.
- A restore line in a generator or coroutine is refused before the run. Each resumption runs on
  part-way through it, so its calls can't be told apart. So is one in a lambda, a comprehension or
  a class body.

## 4.6 Same Object, or a Copy? (Java, Python)

Read [COOKBOOK 4.6](../COOKBOOK.md#46-same-object-or-a-copy-java-python) first.

### Tell the same object from a copy

Use identities to tell whether a variable is the list's own element, or a copy whose changes go
nowhere.

Values with an identity carry debugpy's integer object number. Numbers, strings and `None` have
none. The number is not a memory address. Text output shows it as `[id 6]`. In SQL, it is the
`identity` column. Java uses `object_ref` instead; see
[Java 4.6](java-cookbook.md#46-same-object-or-a-copy-java-python).

For example, is `tags`, a mutable default argument, the same list on every call?

```sh
peeky probe examples/corpus/mutable_default/program.py --at 3 \
  --capture 'tags,add_tag.__defaults__[0]' --hits 2
peeky sql "SELECT hit, selector, value, identity, scope FROM last_run"
```

```
hit  selector                 value       identity  scope
1    tags                     ['a']       6         NULL
1    add_tag.__defaults__[0]  ['a']       6         globals
2    tags                     ['a', 'b']  6         NULL
2    add_tag.__defaults__[0]  ['a', 'b']  6         globals
(4 rows)
```

Yes: `tags` is the function's default list, not a copy. Read identities this way:

- **At one hit,** equal identities mean the same object.
- **Across hits,** a different identity always means a different object. An equal identity means
  the same object, or a new object that took a freed object's place. Here the default lives as long
  as the function, and its contents grow.
- **Across runs,** never compare identities.
- **Numbers, strings and `None`** have no identity. The column is NULL.
- **The number alone** identifies the object. Its type is a separate column. An object keeps its
  number when its `__class__` is reassigned.

The `scope` column says where the selector's first name was read:

| `scope` | Where the name was read |
| --- | --- |
| NULL | the frame's own variables |
| `globals` | the module's globals ([5.1](#51-stop-look-change-finish)) |
| `unknown` | not recorded: an early build of schema 6 stored the row, and it could read globals without recording it |

## 5.1 Stop, Look, Change, Finish

Read [COOKBOOK 5.1](../COOKBOOK.md#51-stop-look-change-finish) first.

### Read a module global

Take a name that is missing from the locals. `session get NAME` and a probe's `--capture NAME`
read it from the module's globals only when Peeky proves that the function reads it from there.
Peeky proves it from the function's scoping in its source, with Python's `symtable` module. Only
then does it read the globals. The row is marked `[module global]`, and its `scope` is `globals`.

For this `limits.py`:

```python
LIMIT = 3


def check(n):
    return n > LIMIT


def shadow():
    print(LIMIT)
    LIMIT = 1


print(check(5))
shadow()
```

Stopped in `check` at line 5, `LIMIT` is the module global:

```
$ peeky session start limits.py --at 5
session default (python): state: stopped at limits.py:5 in check, hit 1 (paused 0.0 s)
$ peeky session get n LIMIT
n: int = 5
LIMIT: int = 3  [module global]
$ peeky session close
closed default
cleanup: terminated pid 8211
```

Stopped in `shadow` at line 9, `LIMIT` is a local with no value yet. Python would raise
`UnboundLocalError` here. So Peeky doesn't read the module's `LIMIT`, and `get` exits with status 1:

```
$ peeky session start limits.py --at 9
session default (python): state: stopped at limits.py:9 in shadow, hit 1 (paused 0.0 s)
$ peeky session get LIMIT
LIMIT: <not found: 'LIMIT' is a local of shadow with no value at this line (not yet assigned, or deleted): reading it here raises UnboundLocalError. A module global of that name is not what shadow reads here, so it is not read; visible: (none)>
note: this stop is in shadow at limits.py:9
peeky: no value for LIMIT (each row above says why), so the exit status is 1
$ peeky session close
closed default
cleanup: terminated pid 8263
```

Likewise, Peeky names a local of an enclosing function, a closure variable, a comprehension's
variable or a type parameter as what it is.

Sometimes Peeky can't prove anything:

- when it can't parse the source;
- when the source changed since the program started. It checks mtime and ctime, so a `cp -p`
  counts as a change.

The error then says the name may be a module global.

### List the globals, and what that runs

To list the module's globals, use `session inspect --globals`. They are read-only.

```
$ peeky session start limits.py --at 5
session default (python): state: stopped at limits.py:5 in check, hit 1 (paused 0.0 s)
$ peeky session inspect --globals
stopped at limits.py:5 in check, hit 1 (paused 0.07 s)
[Locals]
  n: int = 5
[Globals] (module globals, read-only)
  (special variables: debugpy's group; select its members by name)
  (function variables: debugpy's group; select its members by name)
  LIMIT: int = 3
$ peeky session close
closed default
cleanup: terminated pid 8237
```

At module level, the locals are the module's globals. So there is no separate `[Globals]` list.

Listing the globals runs every global's `__repr__` in the program. Listing the locals runs theirs
too. So Peeky lists the globals only when it must:

- with `--globals`;
- when `session get` or a probe reads a name that is missing from the locals and that the
  function reads from its globals.

Then all of them are listed and every repr runs, not only the one you asked for. A misspelled name
counts too, because a name the function doesn't bind is a global. For example, in
`--capture total,i`, if `i` is a module-level loop variable, every global's repr runs at each hit.

### Read members that debugpy groups

debugpy puts functions, classes and `__dunder__` names into groups, such as
`(function variables: debugpy's group; ...)`. Select their members by name, for example
`add_tag.__defaults__`. A local that holds a function works the same way:
`session set callback=None` changes it.

`[id 6]` after a value is the object's identity. See
[4.6](#46-same-object-or-a-copy-java-python).

### Read a value inside a loop

Stopped in a loop, a local that this pass assigns *below* the stop still holds an older value. The
value is from an earlier pass, or from before the loop. For example, a loop runs
`key = slug(title)` for each title. After `step --out` of `slug` on the second title, `key` is
still the first title's key.

Peeky shows such a value with a `note`, in `get`, `inspect` and probe rows. Don't trust a value
marked with a `note`, or anything read through that value:

```
key: str = 'hello-world'
  note: this pass's code has not assigned it before this line, and every path through the rest of the pass assigns it (line 21) before reading it: the value is likely from an earlier pass (or from before the loop at line 20)
```

Peeky also refuses to `set` it, because the function's code overwrites it before reading it. To
read or change this pass's value, stop after the line that assigns it.

#### How Peeky decides

Peeky reads the function's bytecode. The program's interpreter compiles the source, both plain and
with `-O`, and the two must agree
([How Peeky reads the code](#how-peeky-reads-the-code)).

The debugger reports a line, not an instruction. So the stop may be at any instruction of the line.
A breakpoint stops only where the line begins. All of the line's instructions must be in the same
innermost loop, a `for` or `while` around the line.

The note needs all three of these:

- No path from the loop's head to the line stores the local.
- No path from the line reads it or `del`s it before storing it. An exception's path counts.
- Every path through the rest of the pass that raises no exception stores it before the pass ends.
  A pass ends back at the loop's head, out of the loop, or at a `return` or `raise`.

A store only in an `except` block, behind a `continue` or `break`, or on one branch proves nothing.

An exception that a handler in the loop may swallow is a path too. Examples are a `with` whose
`__exit__` may return true, an `except` that falls through, and an `async for` that ends. If such a
path goes on in the pass without the store, there is no note. The note names the store on each
path: `lines 11, 13` for `x = 1` in a `try` and `x = 2` in its `except`.

#### When there is no note

There is no note:

- on a loop's header line, because its code also runs before and after the loop;
- on a line inside a `try` whose handler can skip the assignment before a later read: `key = str(x)`
  with `except ValueError: pass`, then `return key` after the loop;
- for a local that a closure shares;
- in module-level code;
- when the source changed since the program started;
- for the module global of that name, listed with `--globals`;
- in any function of a module that does one of the things below. Peeky checks the whole file, and
  a module counts whatever name it is imported as.

| The module may... | For example |
| --- | --- |
| run code other than its source | `exec`, `eval`, `compile`, `types.FunctionType`, `__code__`, `marshal` |
| reach a frame's locals | `locals()`; `vars()` or `dir()` other than with one plain argument; `sys._getframe`, `f_locals`, `f_back`; a tracer; the `inspect` or `builtins` module; `__builtins__`, `__import__`, `globals()`; a string naming a builtin, such as `getattr(b, "exec")` |

#### What the note can't see

The note says what the function's code does, not what actually ran. That is why it says "likely".
It can't see:

- code in another module that reaches into this frame, such as a helper that writes
  `sys._getframe(1).f_locals` (Python 3.13 writes through it);
- code that replaces this module's functions;
- a stop part-way through the assignment itself, such as in a `__del__` of the value it replaces.

## 6.6 See What a Fix Does Before Making It

Read [COOKBOOK 6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it) first.

### Where a Python candidate runs

A candidate is a probe with an edit (`--old`/`--new`). Like a compiled candidate, a Python
candidate runs from a private copy of the target root, with the edit applied. The copy is the
working directory. Files keep their bytes, modes and times. Directories keep their modes.

Whatever the script reaches through its own path is the copy, so all of it runs the edit:

- an import, also through `PYTHONPATH`, whose relative entries still mean your root;
- an editable install, whose paths into the root are moved to the copy;
- another process, such as `multiprocessing` or a subprocess of `__file__`.

Two import cases get a note:

- An editable install maps a package to a directory with another name. Peeky then looks up each
  name the root offers, as the program will import it.
- A name is still served from your tree, by an import hook ahead of every path, as meson-python's
  is. Such a hook may also rebuild in its build directory.

The program's writes stay in the copy, with two exceptions. Writes inside an environment directory,
and writes through a link that leaves the root, reach your originals.

### What happens to repositories

A repository (`.git`, `.hg`, `.svn`) is copied whole, following its links. So `git add`, a commit,
a rewritten index or a touched object stays in the copy.

- A `core.worktree` inside the root moves to the copy.
- Each linked worktree's entry moves too. An entry outside the root is listed at a path that
  doesn't exist, with a note.
- A git directory under another name is handled the same way: a bare repository, or `.bare` beside
  its worktrees.
- If the root itself is a bare repository, its worktree entries move, or the candidate is refused.
- The copy's index is refreshed for the copied files.
- If the copy's git configuration differs from yours, a note says so. A conditional include on the
  repository's path (`includeIf "gitdir:..."`) can cause this.

Some repositories are left out, with a note:

- one over 20,000 files or 256 MB;
- one that borrows objects from another (`alternates`, `--shared` clones);
- one whose `core.worktree` is outside the root;
- a `.git` file that names metadata outside the root, as a linked worktree's does.

### What else is copied, linked or left out

| In the target root | In the copy |
| --- | --- |
| Pure caches: `__pycache__`, `.mypy_cache`, `.ruff_cache` | left out |
| Environments: `.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `site-packages`, `.eggs` | directories of links to the original's entries, so the interpreter uses them from either tree |
| A directory nobody can open, such as another user's `pgdata` | an empty directory with its mode |
| A directory that can be entered but not listed | none: the candidate is refused |

### When the result warns that nothing above the root was copied

Nothing above the target root is copied. In general, reading the source can't tell whether a
program reaches there. So the result warns about it. For example, this `report.py` reads a file
next to it:

```python
def load(path):
    with open(path) as f:
        return [int(line) for line in f]


values = load("data.txt")
total = sum(values) * 2
print(total)
```

```sh
peeky probe report.py --at 8 --capture total --old 'sum(values) * 2' --new 'sum(values)'
```

```
warning: This Python candidate ran from a copy of .../demo/ only (no .git, pyproject.toml, setup.py or setup.cfg there). If the program reads, imports or runs anything above it, as git does to find the repository, give that directory as the target (`peeky probe <project> --at <path/to/file.py>:<line>`): nothing above was copied, so values may differ for that reason, not the edit's
```

If the program reads or imports anything above the root, give the project directory as the target:

```sh
peeky probe proj --at scripts/run.py:14 ...
```

There is no warning in two cases:

- The root is a project's: it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`, and no
  repository above it holds it. A package inside a monorepo still gets the warning.
- The edited script is self-contained. It imports only standard modules that open no file, start no
  process and reach no network. It also names nothing that could reach one. And no file shadows a
  module it imports: a module or package of that name (`json.py`, `json/`) in its own directory,
  the root or a `PYTHONPATH` entry comes first on the program's path.

Text output shows the warning once per root and state directory (`~/.peeky`, or
`PEEKY_STATE_DIR`), and never with `--quiet`. `--json` always has it.

A candidate with a `--run` entry script is refused. Probe the edited file directly instead, or
check the edit with `verify`.

What a self-contained script may use, and what it may not:

| What | Names |
| --- | --- |
| Standard modules it may import | `abc`, `array`, `bisect`, `cmath`, `collections`, `copy`, `dataclasses`, `decimal`, `enum`, `fractions`, `functools`, `heapq`, `itertools`, `json`, `math`, `numbers`, `random`, `re`, `statistics`, `string`, `textwrap`, `typing`, `unicodedata` |
| Parts of `sys` it may use | `argv`, the standard streams, `exit`, `maxsize`, the recursion limit |
| Names it may not use | `open`, `exec`, `eval`, `getattr`, `vars`, a frame's globals, `__file__`, any dunder other than `__name__` and `__init__`, a module's private names or submodules |
