# Peeky for Python

This page lists what is different for Python targets, one rule per line. Each rule links to its
details, in the [Python cookbook](python-cookbook.md) or the [COOKBOOK](../COOKBOOK.md). Read
[AGENT_GUIDE.md](../AGENT_GUIDE.md) first: it has the steps and rules every language shares.

Peeky debugs Python with debugpy, Python's debugger.

## Choose the target

- Or give the command that runs the failing test, after `--test --`:
  `peeky probe . --at src/pkg/mod.py:LINE --capture a --test -- pytest -q tests/test_x.py::test_y`.
  `pytest …` and `python -m pytest …` are `--run-module pytest`, and `python script.py …` is
  `--run`, on the interpreter that command runs: `PATH`'s, or the script's `#!` line. Run it
  from the root. `python -m` with another module, and the interpreter's own options (`-W`), are
  refused, saying what to give instead ([ADR 0009](../adr/0009-first-question.md)).
- Give a `.py` file: `peeky probe app.py --at 12 --capture total`
  ([0](../COOKBOOK.md#0-setup)).
- Or give a project directory, and name the file in `--at`: `--at module.py:<line>`
  ([0](../COOKBOOK.md#0-setup)).
- To stop in a module that a script imports, name the script with `--run`:
  `peeky probe app/ --at lib/parse.py:42 --run main.py`
  ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- To stop in code that a pytest test runs, run pytest as a module. Name the test after `--`:
  `--run-module pytest -- -q tests/test_x.py::test_y`. It runs pytest in the root, as
  `python -m pytest` does, and no other module ([Python 0](python-cookbook.md#run-a-pytest-test)).
- The pytest that runs must be the one installed for the program's interpreter. A `pytest.py` in
  the root that `python -m pytest` would find first is refused, and so is none
  ([Python 0](python-cookbook.md#run-a-pytest-test)).
- Peeky gives pytest `--assert=plain`. pytest's assert rewriting changes a test file's code from
  the source Peeky reads: a line in a loop can stop twice per pass. So a failing assert shows less
  detail. With `-p no:assertion` after `--`, pytest installs no rewrite hook, and Peeky adds
  nothing ([Python 0](python-cookbook.md#run-a-pytest-test)).
- A `--run` script that runs pytest lets it rewrite asserts, and a warning says so. Use
  `--run-module pytest` instead ([Python 0](python-cookbook.md#run-a-pytest-test)).
- `--at` and `--run` name `.py` files inside the root. `--at` may name an installed package's
  file, such as one in the root's `.venv`: debugpy then stops in library code too, and the stack
  lists library frames ([Python 0](python-cookbook.md#stop-in-an-installed-package)).
- A project installed without `-e` runs its `site-packages` copy, so a breakpoint in its source
  never stops: the probe ends `no-stop`. Install it editable, `pip install -e .`
  ([Python 0](python-cookbook.md#stop-in-an-installed-package)).
- Pass program arguments after `--`, or with repeated `--arg`. Write `--arg=-v` for a value that
  starts with a dash ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).

## Choose the interpreter

- A program runs on its project's interpreter: `--python PATH`, else the root's `.venv` or `venv`,
  else the active virtual environment, else Peeky's own. `--python peeky` keeps Peeky's own
  ([Python 0](python-cookbook.md#which-interpreter-runs-the-program)).
- Only the root is looked in. A `.venv` above it is named in the output, not used. Give the project
  as the target, or pass `--python`
  ([Python 0](python-cookbook.md#which-interpreter-runs-the-program)).
- A shim, such as pyenv's, is asked in the root, and the Python it picks there runs the program
  ([Python 0](python-cookbook.md#which-interpreter-runs-the-program)).
- The output says which one ran: `python: …` in text, `python` in `--json`
  ([Python 0](python-cookbook.md#which-interpreter-runs-the-program)).
- It must be CPython 3.11 or later, and needs no debugpy. Peeky reads the program's code with it,
  at the program's optimization level (`PYTHONOPTIMIZE`)
  ([Python 0](python-cookbook.md#how-peeky-reads-the-code)).

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
- A string's value keeps its quotes: `'Évora'`. So in SQL, `value = 'Évora'` matches nothing. Write
  `value = '''Évora'''`, with the quotes. SQL's `text()` and `elements()` are NULL in Python: a
  class can make its `__repr__` and type name read like a `str`'s
  ([4.9](../COOKBOOK.md#49-a-lists-elements-and-a-strings-text)).
- `[id 6]` is debugpy's number for an object. At one hit, the same number means the same object.
  Across hits, a different number means a different object, but the same number may be a new
  object in a freed one's place. Never compare numbers across runs
  ([Python 4.6](python-cookbook.md#46-same-object-or-a-copy-java-python)).
- To list the module's globals, use `session inspect --globals`. They are read-only. Listing them
  runs every global's `__repr__` in the program
  ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- `get NAME` reads a module global only where Peeky proves that the function reads that name from
  its globals ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- In a loop, a local that this pass assigns below the stop still holds an older value. It is from
  an earlier pass, or from before the loop. Peeky shows it with a `note`: `this pass's code has not
  assigned it before this line, ...: the value is likely from an earlier pass`. `--set` refuses
  it. To read or change this pass's value, stop after the assignment
  ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- A probe reads a caller's local as `1:i`, beside the stopped function's values. The same rules
  apply: a local this pass assigns below the call shows that `note`
  ([2.10](../COOKBOOK.md#210-values-in-a-callers-frame)).
- debugpy lists only a long list's first 100 items. A higher index shows "beyond the debugger's
  visible window": the item is not missing ([guide](../AGENT_GUIDE.md#reading-probe-results)).
- `--until hits` ends the probe at the last hit, but the program runs on until Peeky ends it. So
  its `output` can include text written after that hit (`output_after_last_hit: true`). debugpy
  delays the program's exit by about 0.5 s, so the program has rarely ended by then, and
  `output_complete` is `false`. For the whole output, run to the exit, which is the default
  ([2.6](../COOKBOOK.md#26-stop-right-after-the-last-hit)).

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
  ([2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)). `--set-at LINE@all name=value`
  changes a local at every stop of the line ([2.8](../COOKBOOK.md#change-it-at-every-stop-all)).
  `name=@before` writes back a number, `True`, `False` or `None` as it was before the probe's
  change at the same hit ([Python 2.8](python-cookbook.md#restore-a-value-with-before)).
- To skip a statement in an experiment, flip its guard at one line and restore it at the next:
  `--set-at 14@all ready=False --set-at 17@all ready=@before`
  ([2.8](../COOKBOOK.md#skip-a-statement-restore-with-before)).

## See what a function returned

- `--capture return` is refused for Python, because debugpy would write into the program's
  globals. `session step --out` shows no return value either
  ([2.9](../COOKBOOK.md#29-what-a-function-returned)).
- Instead, capture what the caller stores. For `r = f(x)` on line 20, probe line 21 with
  `--capture r` ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## Try an edit before making it

- A candidate (a probe with `--old`/`--new`) runs from a private copy of the target root, with the
  edit applied. The copy is the working directory. Your files don't change
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- Nothing above the root is copied, and a warning says so. There is no warning when the root is a
  project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) that no repository
  above it holds. Nor when the edited script is self-contained: it imports only standard modules
  that open no file, and names nothing that could. A file of a module's name in its directory, the
  root or on `PYTHONPATH`, such as `json.py`, shadows that module, so the warning stays. So does a
  path entry Peeky can't list, such as one inside a zip archive
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- Text shows the warning once per root. `--json` always has it
  ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- If the program reads or imports anything above the root, give the project directory as the
  target ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- A candidate with `--run` or `--run-module` is refused. Probe the edited file directly, or check
  the edit with `verify` ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
  To run tests on the edit, use `verify --check-command` ([below](#verify-an-edit-with-your-tests)).
- An edit with a syntax error fails with `NO_STOP`. The output shows the `SyntaxError`
  ([6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it)).

## Verify an edit with your tests

- `verify --check-command 'python -m pytest tests/test_x.py -q'` runs your test command in place
  of a checker. It runs in a fresh copy of the root with the edit, and passes when the command exits
  0. Run it with `--baseline` first: it should fail on the bug
  ([6.7](../COOKBOOK.md#67-check-an-edit-with-the-projects-tests)).
- The command is trusted, not sandboxed. It runs with your permissions, and can write outside the
  copy, such as into the `.venv` the copy links to
  ([6.7](../COOKBOOK.md#67-check-an-edit-with-the-projects-tests)).
- `python` is the program's interpreter. Other programs are found on `PATH`, with the interpreter's
  directory first. Another Python, or a script that runs on one, is refused
  ([6.7](../COOKBOOK.md#what-it-runs)).
- No shell runs it, so shell syntax is refused. Put words such as a test id with `[` last, after
  `--check-command --`. `tox` and `nox` are refused: give the command they run
  ([6.7](../COOKBOOK.md#what-it-runs)).
- It gets 20 seconds, as a checker does. Its output keeps each stream's end, where pytest's summary
  is ([6.7](../COOKBOOK.md#what-it-reports)).
- For a command other than pytest, an edit is refused when a module the project supplies imports
  from outside the copy, as from a stale install in `site-packages`. Peeky looks it up as the
  command's own Python starts. Uninstall it, or install the project editable
  ([6.7](../COOKBOOK.md#what-it-imports)).
- Only a Python command Peeky starts can pass: `python -m …`, `-c`, a script, or a console
  script. Its report says what it loaded. A run that loaded your project's files or stale code, or
  reported nothing, is no verdict. So is one that started another program, unless it starts none
  itself, as `echo` or `cp`. Any other command, such as `make`, is no verdict
  ([6.7](../COOKBOOK.md#what-the-command-loaded)).
- A command Peeky stopped at its time or output bound is no verdict too
  ([6.7](../COOKBOOK.md#what-it-reports)).
- The result leads with `verdict: passed`, `failed` or `unknown`. `unknown` is no failure of the
  edit: Peeky can't tell, and `reason: no verdict: …` says why
  ([6.7](../COOKBOOK.md#what-it-reports)).
- `baseline:` says the tests fail on the bug only for pytest or unittest output that shows failed
  tests ([6.7](../COOKBOOK.md#what-it-reports)).
- Nothing above the root is copied, and a warning says so unless the root is a project's. The
  edited file being self-contained doesn't remove it: the tests may import anything
  ([6.7](../COOKBOOK.md#what-it-imports)).
