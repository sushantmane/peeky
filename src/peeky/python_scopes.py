"""Which names a stopped Python function reads from its module's globals (#104).

A name missing from a frame's locals is not necessarily a global: before its first
assignment a local is simply absent, and reading it raises UnboundLocalError
(`def f(): print(x); x = 1`). Only the compiler's own scoping, recomputed here with
`symtable` from the frame's source file, says which names the function reads from its
globals. Nothing is evaluated in the program.

Each source is parsed once into a compact index (every function's line span and how
it binds each name); the syntax tree and symbol table are dropped, and the index cache
is bounded by size. Nothing is parsed until a name is missing from a frame's locals.
"""

import ast
import importlib.util
import os
import symtable
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
    """(names a function's own code binds outside comprehension targets, names only a
    comprehension's `for` target binds there), from its syntax; nested scopes excluded,
    but their names (`def g`, `class C`) bind here."""
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
    return outside, targets - outside


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
    outside, comprehension_only = _bound_in_body(node)
    bindings = {}
    for symbol in table.get_symbols():
        name = symbol.get_name()
        if symbol.is_global():
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
    pending = [child for child in table.get_children() if child.get_name() in _COMPREHENSIONS]
    while pending:
        child = pending.pop()
        for symbol in child.get_symbols():
            if not symbol.is_global() and symbol.get_name() != ".0":
                bindings.setdefault(symbol.get_name(), ("comprehension", None))
        pending.extend(c for c in child.get_children() if c.get_name() in _COMPREHENSIONS)
    own = frozenset(bindings)
    # A method's `__class__` cell exists only when it uses super() or __class__.
    bindings.setdefault("__class__", ("class cell", None))
    for outer in reversed(chain):
        kind = str(outer.get_type())
        if kind in ("module", "class"):
            continue
        # A function around this one binds these: they are its, never the module's.
        relation = "type parameter" if kind.startswith("type") else "enclosing"
        for symbol in outer.get_symbols():
            if not symbol.is_global() and not symbol.get_name().startswith("."):
                bindings.setdefault(symbol.get_name(), (relation, outer.get_name()))
    return {"bindings": bindings, "own": own}


def _size(index):
    return 64 + sum(
        96 + sum(48 + len(name) for name in entry.get("bindings", ()))
        for entries in index["functions"].values()
        for entry in entries
    )


def _index(path, loaded_by=None):
    """``(index, None)`` for a source file, or ``(None, reason)``."""
    try:
        status = os.stat(path)
    except OSError as exc:
        return None, f"its source could not be read ({type(exc).__name__})"
    # A copy keeps an old mtime (`cp -p`), but any write, rename or touch moves ctime.
    if loaded_by is not None and max(status.st_mtime, status.st_ctime) > loaded_by:
        return None, "its source file changed after the program started"
    if status.st_size > MAX_SOURCE_BYTES:
        return None, "its source file is too large to analyze"
    key = (
        str(path),
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )
    if key in _INDEX:
        _INDEX.move_to_end(key)
        return _INDEX[key][0], None
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
            if os.fstat(stream.fileno()).st_ctime_ns != status.st_ctime_ns:
                return None, "its source file changed while Peeky read it"
    except OSError as exc:
        return None, f"its source could not be read ({type(exc).__name__})"
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
