"""Which names a stopped Python function reads from its module's globals (#104).

A name missing from a frame's locals is not necessarily a global: before its first
assignment a local is simply absent, and reading it raises UnboundLocalError
(`def f(): print(x); x = 1`). Only the compiler's own scoping, recomputed here with
`symtable` from the frame's source file, says which names the function reads from its
globals. Nothing is evaluated in the program.
"""

import ast
import hashlib
import importlib.util
import os
import symtable

MAX_SOURCE_BYTES = 4_000_000
MAX_NAMES = 4096
# Comprehensions run in the function's own frame from Python 3.12 (PEP 709).
_COMPREHENSIONS = frozenset({"listcomp", "setcomp", "dictcomp", "genexpr"})
_PARSED = {}


def _parse(path, loaded_by=None):
    """``((tree, symbol table), None)`` for a source file, or ``(None, reason)``. A probe
    inspects once per hit, so the last few files are kept, by content."""
    try:
        with open(path, "rb") as stream:
            changed = os.fstat(stream.fileno()).st_mtime
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        return None, f"its source could not be read ({type(exc).__name__})"
    if loaded_by is not None and changed > loaded_by:
        # Python compiled the file it loaded; the one on disk now may differ.
        return None, "its source file changed after the program started"
    if len(data) > MAX_SOURCE_BYTES:
        return None, "its source file is too large to analyze"
    key = (str(path), hashlib.sha256(data).hexdigest())
    if key in _PARSED:
        return _PARSED[key], None
    try:
        text = importlib.util.decode_source(data)
        parsed = (ast.parse(text, str(path)), symtable.symtable(text, str(path), "exec"))
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError, MemoryError):
        return None, "its source could not be parsed (was the file changed?)"
    while len(_PARSED) >= 8:
        _PARSED.pop(next(iter(_PARSED)))
    _PARSED[key] = parsed
    return parsed, None


def shadowed_names(path, function, line, listed=(), loaded_by=None):
    """``(names, None)``: the names that are not module globals in ``function`` stopped at
    ``line`` of ``path`` (its locals, parameters, cells and free variables, names its
    comprehensions bind, and the locals of enclosing functions); any other name it reads
    is a global. ``(None, reason)`` when that cannot be proven. ``listed`` are the names
    the debugger lists as the frame's locals; each must be one the source binds there.
    A file modified after ``loaded_by`` (a time.time()) proves nothing."""
    if not function.isidentifier():
        return None, f"{function} is not a function Peeky can find in the source"
    parsed, reason = _parse(path, loaded_by)
    if parsed is None:
        return None, reason
    tree, top = parsed
    spans = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
        and min([node.lineno, *(d.lineno for d in node.decorator_list)])
        <= line
        <= (node.end_lineno or node.lineno)
    ]
    if not spans and any(
        isinstance(node, ast.ClassDef)
        and node.name == function
        and node.lineno <= line <= (node.end_lineno or node.lineno)
        for node in ast.walk(tree)
    ):
        return None, f"{function} is a class body, which reads its own namespace first"
    if len(spans) != 1:
        return None, (
            f"{len(spans)} functions named {function} contain line {line}"
            if spans
            else f"no function named {function} contains line {line} (was the file changed?)"
        )
    found = []

    def visit(table, chain):
        if table.get_type() == "function" and table.get_name() == function:
            if table.get_lineno() == spans[0]:
                found.append((table, chain))
        for child in table.get_children():
            visit(child, (*chain, table))

    try:
        visit(top, ())
    except RecursionError:
        return None, "its source nests too deeply to analyze"
    if len(found) != 1:
        return None, f"the compiler's scopes for {function} could not be identified"
    table, chain = found[0]
    bound = {symbol.get_name() for symbol in table.get_symbols() if not symbol.is_global()}
    # A method's `__class__` cell (super()) is implicit; it is never a module global.
    bound.add("__class__")
    pending = [child for child in table.get_children() if child.get_name() in _COMPREHENSIONS]
    while pending:
        child = pending.pop()
        bound.update(s.get_name() for s in child.get_symbols() if not s.is_global())
        pending.extend(c for c in child.get_children() if c.get_name() in _COMPREHENSIONS)
    unknown = sorted(name for name in listed if name not in bound)
    if unknown:
        return None, (
            f"the debugger lists locals the source of {function} does not bind "
            f"({', '.join(unknown[:5])}): the file may have changed since it was loaded"
        )
    for outer in chain:
        # A name an enclosing function binds is that function's, not the module's: the
        # stopped function cannot see it unless it closes over it.
        if outer.get_type() not in ("module", "class"):
            bound.update(s.get_name() for s in outer.get_symbols() if not s.is_global())
    if len(bound) > MAX_NAMES:
        return None, f"{function} binds too many names to list"
    return sorted(bound), None
