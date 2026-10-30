"""Which names a stopped Python function reads from its module's globals.

A name missing from a frame's locals is not necessarily a global: before its first
assignment a local is simply absent, and reading it raises UnboundLocalError
(`def f(): print(x); x = 1`). Only the compiler's own scoping, recomputed here with
`symtable` from the frame's source file, says which names the function reads from its
globals. Nothing is evaluated in the program.

Each source is parsed once into a compact index (every function's line span and how
it binds each name); the syntax tree and symbol table are dropped, and the index cache
is bounded by size. Nothing is parsed until a name is missing from a frame's locals.

Also the lines and scopes `describe` lists (``breakable_lines``, ``function_scopes``).
Each of these is read by the program's own interpreter (python_interpreter).
"""

import ast
import dis
import importlib.util
import os
import symtable
import warnings
from collections import OrderedDict

# The syntax tree and symbol table of a source take a few hundred times its size while
# its index is built: larger sources prove nothing.
MAX_SOURCE_BYTES = 1_000_000
MAX_INDEX_BYTES = 16_000_000
# Comprehensions' own tables (Python 3.11); from 3.12 they run in the function's frame.
_COMPREHENSIONS = frozenset({"listcomp", "setcomp", "dictcomp", "genexpr"})
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_INDEX = OrderedDict()
_INDEX_BYTES = [0]


def _stored(target):
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def _bound_in_body(function):
    """(names a function's own code binds outside comprehension targets, names its
    comprehensions' `for` targets bind), from its syntax; nested scopes excluded, but
    their names (`def g`, `class C`) bind here."""
    outside, targets = set(), set()
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _NESTED):
            if not isinstance(node, ast.Lambda):
                outside.add(node.name)
            # Decorators, defaults and bases run here; bodies do not.
            pending.extend(getattr(node, "decorator_list", ()))
            pending.extend(getattr(node, "bases", ()))
            if args := getattr(node, "args", None):
                pending.extend([*args.defaults, *(d for d in args.kw_defaults if d)])
            continue
        if isinstance(node, _COMPREHENSION_NODES):
            for generator in node.generators:
                targets |= _stored(generator.target)
                pending.extend([generator.iter, *generator.ifs])
            pending.extend(
                child
                for child in (getattr(node, "elt", None), getattr(node, "key", None))
                if child is not None
            )
            if isinstance(node, ast.DictComp):
                pending.append(node.value)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            outside.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            outside.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            outside.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            outside.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            outside.add(node.rest)
        pending.extend(ast.iter_child_nodes(node))
    return outside, targets


def _build(text, path):
    """{"functions": {name: [entry]}, "classes": {name: [(first, last)]}}: each entry has
    the function's lines (decorators included) and, for every name it or a function
    around it binds, how: (kind, owner)."""
    tree = ast.parse(text, path)
    top = symtable.symtable(text, path, "exec")
    syntax, classes = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            last = node.end_lineno or node.lineno
            syntax.setdefault((node.name, node.lineno), []).append((first, last, node))
        elif isinstance(node, ast.ClassDef):
            last = node.end_lineno or node.lineno
            classes.setdefault(node.name, []).append((node.lineno, last))
    functions = {}

    def visit(table, chain):
        kind = str(table.get_type())
        name = table.get_name()
        if kind == "function" and name.isidentifier():
            places = syntax.get((name, table.get_lineno()), [])
            if len(places) == 1:
                first, last, node = places[0]
                functions.setdefault(name, []).append(
                    {"first": first, "last": last, **_bindings(table, chain, node)}
                )
            else:
                for first, last, _ in places:
                    # Two functions of one name on one line: neither is identified.
                    functions.setdefault(name, []).append(
                        {"first": first, "last": last, "ambiguous": True}
                    )
        for child in table.get_children():
            visit(child, (*chain, table))

    visit(top, ())
    return {"functions": functions, "classes": classes}


def _bindings(table, chain, node):
    outside, targets = _bound_in_body(node)
    comprehension_only = targets - outside
    bindings, globals_ = {}, set()
    for symbol in table.get_symbols():
        name = symbol.get_name()
        if symbol.is_global():
            # The function reads it from its globals, whatever a comprehension or a
            # generator expression in it binds under that name in its own scope.
            globals_.add(name)
            continue
        if name == "__class__" and symbol.is_free():
            bindings[name] = ("class cell used", None)
        elif symbol.is_parameter():
            bindings[name] = ("parameter", None)
        elif symbol.is_free():
            owner = next(
                (
                    outer.get_name()
                    for outer in reversed(chain)
                    if str(outer.get_type()) not in ("module", "class")
                    and name in outer.get_identifiers()
                    and outer.lookup(name).is_local()
                ),
                None,
            )
            bindings[name] = ("free", owner)
        elif name in comprehension_only:
            bindings[name] = ("comprehension", None)
        else:
            bindings[name] = ("local", None)
    # Names the frame may list: a comprehension that runs in the function's frame
    # (Python 3.12+) lists its variable there while it runs.
    own = set(bindings) | targets
    pending = [child for child in table.get_children() if child.get_name() in _COMPREHENSIONS]
    while pending:
        child = pending.pop()
        for symbol in child.get_symbols():
            name = symbol.get_name()
            if symbol.is_global() or name == ".0":
                continue
            own.add(name)
            if name not in globals_:
                bindings.setdefault(name, ("comprehension", None))
        pending.extend(c for c in child.get_children() if c.get_name() in _COMPREHENSIONS)
    # A method's `__class__` cell exists only when it uses super() or __class__.
    if "__class__" not in globals_:
        bindings.setdefault("__class__", ("class cell", None))
    for outer in reversed(chain):
        kind = str(outer.get_type())
        if kind in ("module", "class"):
            continue
        # A function around this one binds these: they are its, never the module's,
        # unless the function declares them global.
        relation = "type parameter" if kind.startswith("type") else "enclosing"
        for symbol in outer.get_symbols():
            name = symbol.get_name()
            if not symbol.is_global() and not name.startswith(".") and name not in globals_:
                bindings.setdefault(name, (relation, outer.get_name()))
    return {"bindings": bindings, "own": frozenset(own)}


def _size(index):
    return 64 + sum(
        96 + sum(48 + len(name) for name in entry.get("bindings", ()))
        for entries in index["functions"].values()
        for entry in entries
    )


def source_key(path, loaded_by=None):
    """``(key, None)``: what identifies a source file's current content (its path, inode,
    size and times), or ``(None, reason)`` where it cannot be read, is too large, or
    changed after ``loaded_by`` (a time.time()), so may not be the code running."""
    try:
        status = os.stat(path)
    except OSError as exc:
        return None, f"its source could not be read ({type(exc).__name__})"
    # A copy keeps an old mtime (`cp -p`), but any write, rename or touch moves ctime.
    if loaded_by is not None and max(status.st_mtime, status.st_ctime) > loaded_by:
        return None, "its source file changed after the program started"
    if status.st_size > MAX_SOURCE_BYTES:
        return None, "its source file is too large to analyze"
    return (
        str(path),
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    ), None


def read_source(path, key):
    """``(bytes, None)``: the source ``key`` (``source_key``) names, or ``(None, reason)``
    where it changed while read."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
            if os.fstat(stream.fileno()).st_ctime_ns != key[-1]:
                return None, "its source file changed while Peeky read it"
    except OSError as exc:
        return None, f"its source could not be read ({type(exc).__name__})"
    return data, None


def _index(path, loaded_by=None):
    """``(index, None)`` for a source file, or ``(None, reason)``."""
    key, reason = source_key(path, loaded_by)
    if key is None:
        return None, reason
    if key in _INDEX:
        _INDEX.move_to_end(key)
        return _INDEX[key][0], None
    data, reason = read_source(path, key)
    if data is None:
        return None, reason
    try:
        index = _build(importlib.util.decode_source(data), str(path))
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return None, "its source could not be parsed (was the file changed?)"
    size = _size(index)
    if size <= MAX_INDEX_BYTES:
        _INDEX[key] = (index, size)
        _INDEX_BYTES[0] += size
        while _INDEX_BYTES[0] > MAX_INDEX_BYTES:
            _, (_, dropped) = _INDEX.popitem(last=False)
            _INDEX_BYTES[0] -= dropped
    return index, None


def classify(path, function, line, names, listed=(), loaded_by=None):
    """How ``function``, stopped at ``line`` of ``path``, sees each of ``names``:
    ``({name: None or (kind, owner)}, None)``, None meaning the function reads that name
    from its module's globals; or ``(None, reason)`` when that cannot be proven.

    A kind says what binds the name instead: `local` (not yet assigned, or deleted),
    `parameter` (deleted), `comprehension` (its `for` target), `free` (a closure
    variable of ``owner`` not yet assigned there), `class cell used` / `class cell` (a
    method's implicit `__class__`), `enclosing` (a local of the function ``owner`` around
    it) or `type parameter` (of ``owner``, PEP 695). ``listed`` are the names the
    debugger lists as the frame's locals; each must be one the source binds there. A
    file changed after ``loaded_by`` (a time.time()) proves nothing."""
    if not function.isidentifier():
        return None, f"{function} is not a function Peeky can find in the source"
    index, reason = _index(path, loaded_by)
    if index is None:
        return None, reason
    spans = [e for e in index["functions"].get(function, ()) if e["first"] <= line <= e["last"]]
    if not spans and any(
        first <= line <= last for first, last in index["classes"].get(function, ())
    ):
        return None, f"{function} is a class body, which reads its own namespace first"
    if any(entry.get("ambiguous") for entry in spans):
        return None, f"the compiler's scopes for {function} could not be identified"
    if len(spans) != 1:
        return None, (
            f"{len(spans)} functions named {function} contain line {line}"
            if spans
            else f"no function named {function} contains line {line} (was the file changed?)"
        )
    entry = spans[0]
    unknown = sorted(name for name in listed if name not in entry["own"])
    if unknown:
        return None, (
            f"the debugger lists locals the source of {function} does not bind "
            f"({', '.join(unknown[:5])}): the file may have changed since it was loaded"
        )
    return {name: entry["bindings"].get(name) for name in names}, None


def _parse_error(path, exc):
    return ValueError(f"{path} does not parse as Python: {exc.msg} (line {exc.lineno})")


def breakable_lines(path):
    """The lines of a source the compiler gives code: where a breakpoint can stop."""
    try:
        code = compile(path.read_text(), str(path), "exec")
    except SyntaxError as exc:
        raise _parse_error(path, exc) from exc
    lines, stack = set(), [code]
    while stack:
        item = stack.pop()
        lines.update(line for _, _, line in item.co_lines() if line)
        stack.extend(c for c in item.co_consts if hasattr(c, "co_lines"))
    return sorted(lines)


MAX_SCOPES = 300


def function_scopes(path):
    """Names each Python function (and the module) can see, for choosing selectors.

    Static: lists parameters and assigned locals, not values or attributes, and the
    module globals a function reads that the module assigns (`globals`: a probe reads them,
    `[module global]`): names the module binds (`MAX_GAP = 30`, not
    `globals()["x"] = 1`) that the function's compiled code loads as globals (its generator
    expressions' and comprehensions' too), so not in code the compiler drops (`if False:`);
    nothing runs. Methods are qualified by class
    (`IPv6Address.is_private`); listed in source order, first MAX_SCOPES only.
    """
    text = path.read_text()
    try:
        tree = ast.parse(text, str(path))
        table = symtable.symtable(text, str(path), "exec")
    except SyntaxError as exc:
        raise _parse_error(path, exc) from exc
    # Names the module itself binds (`MAX_GAP = 30`), not its functions, classes or
    # imports, nor a name a target only reads (`globals()["x"] = 1`, `counts[0] = 1`):
    # what a function may read as a module global.
    assigned = set()
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign))
            else []
        )
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    assigned.add(name.id)
    # The globals each function's compiled code loads, by (name, first line) as the
    # compiler numbers it: a decorated function's code starts at its first decorator.
    try:
        with warnings.catch_warnings():
            # A program's own syntax warnings (`"\d"`) are not describe's to print.
            warnings.simplefilter("ignore")
            compiled = compile(text, str(path), "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        compiled = None
    # A generator expression's code is its own (a comprehension's too, before 3.12), but
    # it is the enclosing function's: its loads count there.
    loads, codes = {}, [(compiled, None)] if compiled is not None else []
    while codes:
        code, into = codes.pop()
        if into is None or code.co_name not in (
            "<genexpr>",
            "<listcomp>",
            "<setcomp>",
            "<dictcomp>",
        ):
            into = set()
            loads.setdefault((code.co_name, code.co_firstlineno), []).append(into)
        into.update(
            instruction.argval
            for instruction in dis.get_instructions(code)
            if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS")
        )
        codes.extend((each, into) for each in code.co_consts if hasattr(each, "co_code"))
    spans, starts = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            name = getattr(node, "name", "lambda")
            spans.setdefault((node.lineno, name), node.end_lineno)
            first = min([d.lineno for d in getattr(node, "decorator_list", ())] + [node.lineno])
            starts.setdefault(
                (node.lineno, name), ("<lambda>" if name == "lambda" else name, first)
            )
    found, stack = [], [(table, "")]
    while stack and len(found) < 20000:
        item, prefix = stack.pop()
        kind = item.get_type()
        name = item.get_name()
        qualified = f"{prefix}{name}"
        # Only functions and classes name a level; PEP 695 type-parameter scopes and the
        # module pass their prefix through unchanged.
        child_prefix = f"{qualified}." if kind in ("function", "class") else prefix
        stack.extend((child, child_prefix) for child in item.get_children())
        end = spans.get((item.get_lineno(), name))
        if kind == "module":
            found.append((0, "<module>", None, item))
        elif kind == "function" and end is not None:
            # Generator expressions and annotation scopes have no source span: skipped.
            found.append((item.get_lineno(), qualified, [item.get_lineno(), end], item))
    found.sort(key=lambda entry: entry[0])
    scopes = []
    for _, qualified, lines, item in found[:MAX_SCOPES]:
        names = sorted(
            s.get_name()
            for s in item.get_symbols()
            if s.is_parameter()
            or s.is_assigned()
            or (item.get_type() == "module" and s.is_imported())
        )
        # One code object of that name and first line, else none listed: two lambdas on
        # a line cannot be told apart.
        matched = loads.get(starts.get((item.get_lineno(), item.get_name())), [])
        read = (
            sorted(matched[0] & assigned)
            if item.get_type() != "module" and len(matched) == 1
            else []
        )
        scope = {
            "scope": qualified,
            "lines": lines,
            "names": (names + [name for name in read if name not in names])[:100],
        }
        if read:
            scope["globals"] = read
        scopes.append(scope)
    if len(found) > MAX_SCOPES:
        scopes.append(
            {"scope": "...", "lines": None, "names": [], "omitted": len(found) - MAX_SCOPES}
        )
    return scopes
