# Peeky Cookbook: Python

What is particular to Python in Peeky's recipes. Read the [COOKBOOK](../COOKBOOK.md) for the recipes every language shares: each section here has the number and title of the COOKBOOK section it adds to. The one-page summary of Python's rules is [python.md](python.md).

## Contents

- [2.1 Fields and Indexes](#21-fields-and-indexes)
- [2.8 Change a Value, Then Restore It](#28-change-a-value-then-restore-it)
- [4.6 Same Object, or a Copy? (Java, Python)](#46-same-object-or-a-copy-java-python)
- [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish)
- [6.6 See What a Fix Does Before Making It](#66-see-what-a-fix-does-before-making-it)

## 2.1 Fields and Indexes

Adds to [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes).

- **Python:** Python dict entries are read by key; `order.id` fails with a message that names `order['id']` and lists the keys.

## 2.8 Change a Value, Then Restore It

Adds to [COOKBOOK 2.8](../COOKBOOK.md#28-change-a-value-then-restore-it).

- **Python lambdas:** Python's debugger names two lambdas on one line alike (`<lambda>`, with no column), so the warning says their stops are counted together, and a change in one says `one of 2 on the line the debugger names alike`.

## 4.6 Same Object, or a Copy? (Java, Python)

Adds to [COOKBOOK 4.6](../COOKBOOK.md#46-same-object-or-a-copy-java-python).

**What it answers:** Is this variable the list's own element, or a copy whose changes go nowhere?

**Python** values carry their object in the `identity` column instead of Java's `object_ref` ([Java 4.6](java-cookbook.md#46-same-object-or-a-copy-java-python)) (`[id 6]` in text): an integer, debugpy's number for the object, not an address. The number alone is the object's identity; its `type` is a separate column (an object whose `__class__` is reassigned keeps its number). Is `tags`, a mutable default, the same list on every call?

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
```

At one hit, equal identities are the same object: `tags` is the function's default list, not a copy. Across hits, a different identity always means a different object, but an equal one means the same object or a new one that took a freed object's place; here the default lives as long as the function, and its contents grow. Numbers, strings and `None` have no identity (NULL). Never compare identities across runs. `scope` is `globals` for a selector whose first name was read from the module's globals ([5.1](#51-stop-look-change-finish)), and `unknown` for a row an early build of schema 6 stored (it could read globals without recording it); NULL is the frame's own variables.

## 5.1 Stop, Look, Change, Finish

Adds to [COOKBOOK 5.1](../COOKBOOK.md#51-stop-look-change-finish).

- **Globals:** `session inspect --globals` also lists a Python module's `[Globals]`, read-only (at module level they are the locals, listed once). Listing the globals runs every global's `__repr__` in the program (listing locals runs theirs too), so they are listed only on request: with `--globals`, or when `session get` or a probe reads a name missing from the locals that the function reads from its globals. Then all of them are listed and every repr runs, not only the one asked for; a misspelled name counts too, since a name the function does not bind is a global (`--capture total,i`, where `i` is a module-level loop variable, runs every global's repr at each hit).
- **Module level:** At module level the locals are the module's globals, so there is no separate `[Globals]`.

`session get` reads a Python name missing from the locals from the module's globals (`[module global]`, `scope: globals`) only when Peeky proves, from the function's own scoping in its source (`symtable`), that the function reads that name from its globals; only then are the globals read. A source it cannot parse, or one changed since the program started (by mtime or ctime, so `cp -p` counts), proves nothing, and the error says the name may be such a global.

In `def shadow(): print(LIMIT); LIMIT = 1`, stopped at the `print`, `get LIMIT` says `LIMIT` is a local with no value there yet (Python raises `UnboundLocalError`), not the module's `LIMIT`; a local of an enclosing function, a closure variable, a comprehension's variable or a type parameter is named as such. debugpy files functions, classes and `__dunder__` names under groups; their members are selected by name (`add_tag.__defaults__`), and so is a local holding a function (`session set callback=None` changes it). `[id 6]` is the object's identity (see [4.6](#46-same-object-or-a-copy-java-python)).

**A pass's own value.** Stopped in a loop, a Python local that the pass assigns below the stop still holds an earlier pass's value, or one from before the loop: back in `group` with `step --out` on `key = slug(title)`, `key` is the previous title's key. Peeky shows the value with a `note`, in `get`, `inspect` and probe rows, and refuses to `set` it (the function's code overwrites it before reading it):

```
key: str = 'hello-world'
  note: this pass's code has not assigned it before this line, and every path through the rest of the pass assigns it (line 21) before reading it: the value is likely from an earlier pass (or from before the loop at line 20)
```

- **How:** Peeky reads the function's bytecode, compiled from its source as the program's interpreter compiles it (plain and as `-O` does; both must agree), and only where Peeky runs on an interpreter that compiles alike (the same implementation, exact version and build, and bytecode magic number). The debugger gives a line, not an instruction, so the stop may be at any instruction of the line (a breakpoint's stop only where the line begins), and all must be in the same innermost loop (a `for` or `while` around the line). No path from the loop's head to the line may store the local, none from the line may read or `del` it before storing it (an exception's path counts), and every path through the rest of the pass that runs no exception must store it before the pass ends (back at the loop's head, out of the loop, a `return` or a `raise`): a store only in an `except` block, behind a `continue` or `break`, or on one branch proves nothing. An exception that a handler in the loop may swallow (a `with` whose `__exit__` may return true, an `except` that falls through, an `async for` that ends) is a path too: one that goes on in the pass without the store takes the note away, and the note names the store on each path (`lines 11, 13` for `x = 1` in the `try` and `x = 2` in its `except`).
- **No note:** a loop's header line (its code also runs before and after the loop); a line inside a `try` whose handler can skip the assignment for a later read (`key = str(x)` with `except ValueError: pass`, then `return key` after the loop); a local a closure shares; module-level code; a source changed since the program started; and any function of a module (the whole file) that may run other code than its source says (`exec`, `eval`, `compile`, `types.FunctionType`, `__code__`, `marshal`) or reach a frame's locals (`locals()`, `vars()` or `dir()` other than with one plain argument, `sys._getframe`, `f_locals`, `f_back`, a tracer, the `inspect` or `builtins` module, `__builtins__`, `__import__`, `globals()`, a string naming a builtin such as `getattr(b, "exec")`), whatever a module is imported as. The module global of that name (`--globals`) is never noted.
- **Limits:** the note states what the function's code does, not what ran: code in other modules that reaches into this frame (a helper writing `sys._getframe(1).f_locals`, which Python 3.13 writes through) or replaces this module's functions is not seen, hence "likely". Nor is a stop part-way through the assignment itself (in a `__del__` of the value it replaces).

## 6.6 See What a Fix Does Before Making It

Adds to [COOKBOOK 6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it).

- **Python:** like a compiled candidate, it runs from a private copy of the target root with the edit applied, and that copy is the working directory. Files keep their bytes, modes and times, and directories their modes.
- **What it reaches:** Whatever the script reaches through its own path, an import (also through `PYTHONPATH`, whose relative entries keep meaning your root, or an editable install, whose paths into the root are moved to the copy; one mapping a package to a directory of another name gets a note, and each name the root offers is then looked up as the program will import it: one still served from your tree, by an import hook ahead of every path as meson-python's is, gets a note, and such a hook may rebuild in its build directory), or another process (multiprocessing, a subprocess of `__file__`) is the copy, so all of it runs the edit. The program's writes stay in the copy, except writes inside an environment directory or through a link that leaves the root, which reach the originals:
  - A repository (`.git`, `.hg`, `.svn`) is copied whole, following its links, so `git add`, a commit, a rewritten index or a touched object stays in the copy. A `core.worktree` inside the root moves to the copy, and so does each linked worktree's entry: one outside the root is listed at a path that does not exist, with a note. So is any git directory under another name (a bare repository, `.bare` beside its worktrees); a bare repository as the root itself gets its worktree entries moved, or the candidate is refused. The copy's index is refreshed for the copied files. If the copy's git configuration differs from yours (a conditional include on the repository's path, `includeIf "gitdir:..."`), a note says so.
  - Some repositories are left out, with a note:
    - one over 20,000 files or 256 MB;
    - one that borrows objects from another (`alternates`, `--shared` clones);
    - one whose `core.worktree` is outside the root;
    - a `.git` file naming metadata outside the root (a linked worktree's).
  - Pure caches (`__pycache__`, `.mypy_cache`, `.ruff_cache`) are left out. Environments (`.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `site-packages`, `.eggs`) are directories of links to the originals' entries: the interpreter uses them from either tree.
  - A directory nobody can open (another user's `pgdata`) is an empty one with its mode. One that can be entered but not listed is refused.

  Nothing above the target root is copied, and in general no reading of the source can tell whether a program reaches there. So the result notes it unless the root is a project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) and no repository above it holds it (a monorepo's package).

  Nor does it when the edited script is self-contained: it imports only standard modules that open no file, start no process and reach no network (`abc`, `array`, `bisect`, `cmath`, `collections`, `copy`, `dataclasses`, `decimal`, `enum`, `fractions`, `functools`, `heapq`, `itertools`, `json`, `math`, `numbers`, `random`, `re`, `statistics`, `string`, `textwrap`, `typing`, `unicodedata`; of `sys` only `argv`, the standard streams, `exit`, `maxsize` and the recursion limit), and names nothing that could reach one (`open`, `exec`, `eval`, `getattr`, `vars`, a frame's globals, `__file__` or another dunder but `__name__` and `__init__`, a module's private names or submodules).

  Text shows the note once per root and state directory, never with `--quiet`; `--json` always has it. If the program reads or imports anything from above, give the project directory as the target (`peeky probe proj --at scripts/run.py:14`). `--run` entry scripts are refused.
