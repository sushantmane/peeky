# Peeky for Python

What is particular to Python targets, one rule per line, each with the section that has the details, in the [Python cookbook](python-cookbook.md) or the [COOKBOOK](../COOKBOOK.md). Read [AGENT_GUIDE.md](../AGENT_GUIDE.md) first: the loop and the rules every language shares are there.

## Targets

- A `.py` file, or a project directory with `--at module.py:<line>` ([0](../COOKBOOK.md#0-setup)): `peeky probe app.py --at 12 --capture total`.
- A module a script imports, not runs: `--run script.py` names the script (`peeky probe app/ --at lib/parse.py:42 --run main.py`) ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).
- Program arguments go after `--` or in repeated `--arg` (`--arg=-v` for a value starting with a dash) ([2.5](../COOKBOOK.md#25-a-module-imported-by-a-script-with-arguments)).

## Reading values

- Python compiles names NFKC-normalized, and debugpy lists them so: `größe` written NFD in the source is selected as NFC `größe`, `ﬁx` as `fix`; another spelling is refused with the listed one ([2.1](../COOKBOOK.md#21-fields-and-indexes)).
- Dict entries are read by key: `order['id']`; `order.id` fails with a message that names `order['id']` and lists the keys ([Python 2.1](python-cookbook.md#21-fields-and-indexes)).
- A line stops once per pass (a `for` header once per pass and once more when the loop ends), so Python rows have no `stop` ([2.2](../COOKBOOK.md#22-every-iteration-of-a-loop)).
- Two lambdas on one line are named alike (`<lambda>`): their stops are counted together, and the warning says so ([Python 2.8](python-cookbook.md#28-change-a-value-then-restore-it)).
- `[id 6]` is debugpy's number for the object: at one hit, the same number is the same object; across hits a different number is a different object, but the same number may be a new object in a freed one's place; never compare numbers across runs ([Python 4.6](python-cookbook.md#46-same-object-or-a-copy-java-python)).
- Globals: `session inspect --globals` lists them, read-only; `get NAME` reads a global only where Peeky proves the function reads that name from its globals. Listing the globals runs every global's `__repr__` in the program ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- In a loop, a local the pass assigns below the stop holds an earlier pass's value: it is shown with a `note` (`this pass's code has not assigned it before this line, ...: the value is likely from an earlier pass`), and `--set` refuses it; read or change it after that assignment ([Python 5.1](python-cookbook.md#51-stop-look-change-finish)).
- debugpy lists a long list's first 100 items: a higher index is "beyond the debugger's visible window", not missing ([guide](../AGENT_GUIDE.md#reading-probe-results)).
- `--until hits` ends the probe at the last hit, but the program runs on until Peeky ends it: its `output` can include text written after that hit (`output_after_last_hit: true`). debugpy delays the program's exit by about 0.5 s, so it has rarely ended by the probe's end, and `output_complete` is then `false`; for the whole output, run to the exit (the default) ([2.6](../COOKBOOK.md#26-stop-right-after-the-last-hit)).

## Changing values

- `--set` takes Python literals: numbers, strings, bytes, `True`/`False`/`None` and lists, tuples, dicts and sets of them (`--set "words=['a', 'b']"`, `--set 'name="apple"'`). Expressions, names and calls are rejected, never evaluated ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- Only the stopped function's own variables: fields are refused before the program runs ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- A change rebinds the name in that frame: `--set 'seen=[]'` gives this call a new list, while the shared object it named (a default argument, a module global) keeps its contents ([2.3](../COOKBOOK.md#23-test-a-hypothesis-by-changing-a-value)).
- Changes at several lines or hits: repeat `--set-at LINE@HIT name=value` ([2.8](../COOKBOOK.md#28-change-a-value-then-restore-it)).

## Returns

- `--capture return` is refused for Python (debugpy would write into the program's globals), and `session step --out` shows no return. Capture what the caller stores: for `r = f(x)` on line 20, probe line 21 with `--capture r` ([2.9](../COOKBOOK.md#29-what-a-function-returned)).

## Candidates and verify

- A candidate (`--old`/`--new`) runs from a private copy of the target root with the edit applied, and that copy is the working directory; your files are unchanged ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- Nothing above the root is copied. A warning says so unless the root is a project's (it holds `.git`, `pyproject.toml`, `setup.py` or `setup.cfg`) that no repository above it holds, or the edited script is self-contained (it imports only standard modules that open no file, and names nothing that could); text shows it once per root, `--json` always. If the program reads or imports anything from above, give the project directory as the target ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- A candidate with `--run` is refused: probe the edited file directly, or check the edit with `verify` ([Python 6.6](python-cookbook.md#66-see-what-a-fix-does-before-making-it)).
- An edit with a syntax error fails with `NO_STOP`, with the `SyntaxError` in the output ([6.6](../COOKBOOK.md#66-see-what-a-fix-does-before-making-it)).
