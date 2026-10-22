# Peeky for Python

This page lists what is different for Python targets, one rule per line. Each rule links to its
details, in the [Python cookbook](python-cookbook.md) or the [COOKBOOK](../COOKBOOK.md). Read
[AGENT_GUIDE.md](../AGENT_GUIDE.md) first: it has the steps and rules every language shares.

Peeky debugs Python with debugpy, Python's debugger.

## Choose the target

- Give a `.py` file: `peeky probe app.py --at 12 --capture total`
  ([0](../COOKBOOK.md#0-setup)).
- Or give a project directory, and name the file in `--at`: `--at module.py:<line>`
  ([0](../COOKBOOK.md#0-setup)).
- To stop in a module that a script imports, name the script with `--run`:
  `peeky probe app/ --at lib/parse.py:42 --run main.py`
  ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- Pass program arguments after `--`, or with repeated `--arg`. Write `--arg=-v` for a value that
  starts with a dash ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).

## Read values

- Select a dict entry by its key: `order['id']`. If you write `order.id`, the row names
  `order['id']` and lists the keys ([Python 2.1](python-cookbook.md#21-fields-and-indexes)).
- Write a name as debugpy lists it. Python stores names in Unicode's NFKC form, so `größe` written
  NFD in the source is selected as NFC `größe`, and `ﬁx` as `fix`. Another spelling is refused,
  and the error shows the listed one ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- A line stops once per pass of a loop. A `for` header stops once per pass, and once more when the
  loop ends. So Python rows have no `stop` number
  ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- Two lambdas on one line get the same name, `<lambda>`. Their stops are counted together, and a
  warning says so ([Python 2.8](python-cookbook.md#28-change-a-value-then-restore-it)).
- `[id 6]` is debugpy's number for an object. At one hit, the same number means the same object.
  Across hits, a different number means a different object, but the same number may be a new
  object in a freed one's place. Never compare numbers across runs
  ([Python 4.6](python-cookbook.md#46-same-object-or-a-copy-java-python)).
- To list the module's globals, use `session inspect --globals`. They are read-only. Listing them
  runs every global's `__repr__` in the program
  ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- `get NAME` reads a module global only where Peeky proves that the function reads that name from
  its globals ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- In a loop, a local that this pass assigns below the stop holds an earlier pass's value. Peeky
  shows it with a `note`: `this pass's code has not assigned it before this line, ...: the value is
  likely from an earlier pass`. `--set` refuses it. To read or change this pass's value, stop after
  the assignment ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- debugpy lists only a long list's first 100 items. A higher index shows "beyond the debugger's
  visible window": the item is not missing ([guide](../AGENT_GUIDE.md#reading-probe-results)).

## Change values

- `--set` takes Python literals: numbers, strings, bytes, `True`, `False`, `None`, and lists,
  tuples, dicts and sets of them. For example: `--set "words=['a', 'b']"`, `--set 'name="apple"'`
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- Expressions, names and calls are rejected. Peeky never evaluates them
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- Only the stopped function's own variables can change. A field is refused before the program runs
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- A change rebinds the name in that frame. `--set 'seen=[]'` gives this call a new list. The shared
  object that `seen` named keeps its contents, such as a default argument or a module global
  ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- To change values at several lines or hits, repeat `--set-at LINE@HIT name=value`
  ([2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)).

## See what a function returned

- `--capture return` is refused for Python, because debugpy would write into the program's
  globals. `session step --out` shows no return value either.
- Instead, capture what the caller stores. For `r = f(x)` on line 20, probe line 21 with
  `--capture r` ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## Try an edit before making it

- A candidate (a probe with `--old`/`--new`) runs from a private copy of the target root, with the
  edit applied. The copy is the working directory. Your files don't change
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- Nothing above the root is copied, and a warning says so. There is no warning when the root is a
  project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) that no repository
  above it holds. Nor when the edited script is self-contained: it imports only standard modules
  that open no file, and names nothing that could
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- Text shows the warning once per root. `--json` always has it
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- If the program reads or imports anything above the root, give the project directory as the
  target ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- A candidate with `--run` is refused. Probe the edited file directly, or check the edit with
  `verify` ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- An edit with a syntax error fails with `NO_STOP`. The output shows the `SyntaxError`
  ([6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it)).
