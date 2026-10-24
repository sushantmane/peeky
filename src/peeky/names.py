"""Function names as people read them."""

import re

_RUST_FRAME = re.compile(r"::h[0-9a-f]{16}$")
_RUST_ESCAPES = {
    "$LT$": "<",
    "$GT$": ">",
    "$RF$": "&",
    "$BP$": "*",
    "$C$": ",",
    "$SP$": "@",
    "$LP$": "(",
    "$RP$": ")",
}


def unicode_escape(match):
    """A legacy `$u<hex>$` escape as its character; an impossible one stays as written."""
    code = int(match[1], 16)
    return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else match[0]


def display_function(name):
    """A debugger frame name for display. Rust frames (`…::h<16 hex>`) lose their hash
    and legacy escapes, so `top::f::_$u7b$$u7b$closure$u7d$$u7d$::h0123…` reads
    `top::f::{{closure}}`, as `describe` shows it. A C++ signature names the standard
    library's types as code does (`trim_left(std::string const&)`, not
    `std::__1::basic_string<char, std::__1::char_traits<char>, ...>`), its parameters
    kept, so overloads stay apart. Other languages are unchanged (Java's
    `lambda$run$0` keeps its `$`). lldb-dap on macOS marks an optimized frame
    `… [opt]`; Linux's never does, so the mark is dropped."""
    name = str(name or "")
    if name.endswith(" [opt]") and _RUST_FRAME.search(name[: -len(" [opt]")]):
        name = name[: -len(" [opt]")]
    if not _RUST_FRAME.search(name):
        return _standard_names(name) if "std::" in name else name
    parts = []
    for part in _RUST_FRAME.sub("", name).split("::"):
        if part.startswith("_$"):
            part = part[1:]
        if "$" in part:
            part = part.replace("..", "::")
            for escape, character in _RUST_ESCAPES.items():
                part = part.replace(escape, character)
            part = re.sub(r"\$u([0-9a-f]{1,6})\$", unicode_escape, part)
        parts.append(part)
    return "::".join(parts)


# The standard libraries' inline namespaces (libc++'s `std::__1`, libstdc++'s
# `std::__cxx11`), which a demangled name spells out and code never does.
_INLINE = re.compile(r"\bstd::(?:__1|__ndk1|__cxx11)::")
# Operators whose names hold brackets (`operator<<`, `operator()`), kept out of the way of
# template and parameter brackets while a name is read.
_OPERATOR = re.compile(r"\boperator\s*(?:<<=|>>=|<=>|<<|>>|<=|>=|->\*|->|<|>|\(\)|\[\])")
_CHARACTERS = {
    "char": "",
    "wchar_t": "w",
    "char8_t": "u8",
    "char16_t": "u16",
    "char32_t": "u32",
}
_STREAMS = re.compile(
    r"std::basic_(ios|streambuf|istream|ostream|iostream|stringbuf|istringstream|"
    r"ostringstream|stringstream|filebuf|ifstream|ofstream|fstream)"
)
_SEQUENCES = ("vector", "deque", "list", "forward_list")
_ORDERED = ("set", "multiset", "map", "multimap")
_HASHED = ("unordered_set", "unordered_multiset", "unordered_map", "unordered_multimap")


def _arguments(text, at):
    """The template arguments of the list opening at ``text[at]`` (`<`), and where it
    ends; ValueError where the brackets do not balance."""
    depth, start, found = 0, at + 1, []
    for index in range(at, len(text)):
        char = text[index]
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
            if depth == 0:
                if char != ">":
                    raise ValueError(text)
                found.append(text[start:index].strip())
                return found, index + 1
        elif char == "," and depth == 1:
            found.append(text[start:index].strip())
            start = index + 1
    raise ValueError(text)


def _same(a, b):
    return re.sub(r"\s+", "", a) == re.sub(r"\s+", "", b)


def _template(name, arguments):
    """A standard template's arguments without the defaults code leaves out, or the
    alias code uses (`std::string`) for the whole name: (name, arguments or None)."""
    base = name.rsplit("::", 1)[-1]
    if not name.startswith("std::") or not arguments:
        return name, arguments
    unit = arguments[0]
    traits, allocator = f"std::char_traits<{unit}>", f"std::allocator<{unit}>"
    if unit in _CHARACTERS and name in ("std::basic_string", "std::basic_string_view"):
        rest = arguments[1:]
        if all(_same(a, b) for a, b in zip(rest, (traits, allocator))):
            return f"std::{_CHARACTERS[unit]}{name[len('std::basic_') :]}", None
    if unit in ("char", "wchar_t") and (stream := _STREAMS.fullmatch(name)):
        if all(_same(a, b) for a, b in zip(arguments[1:], (traits, allocator))):
            return f"std::{'w' if unit == 'wchar_t' else ''}{stream[1]}", None
    arguments = list(arguments)
    if name == "std::unique_ptr" and len(arguments) == 2:
        if _same(arguments[1], f"std::default_delete<{unit}>"):
            arguments.pop()
        return name, arguments
    if base not in (*_SEQUENCES, *_ORDERED, *_HASHED) or name != f"std::{base}":
        return name, arguments
    keyed = base.endswith("map") and len(arguments) >= 2
    # A demangler writes the pair's key `K const`; a person, `const K`.
    elements = (
        (f"std::pair<const {unit}, {arguments[1]}>", f"std::pair<{unit} const, {arguments[1]}>")
        if keyed
        else (unit,)
    )
    defaults = [[f"std::allocator<{element}>" for element in elements]]
    if base in _ORDERED:
        defaults.append([f"std::less<{unit}>"])
    elif base in _HASHED:
        defaults += [[f"std::equal_to<{unit}>"], [f"std::hash<{unit}>"]]
    for default in defaults:
        if len(arguments) > (2 if keyed else 1) and any(_same(arguments[-1], d) for d in default):
            arguments.pop()
    return name, arguments


def _templates(text):
    """``text`` with each standard template written as code writes it."""
    shown, at = [], 0
    while (opening := text.find("<", at)) >= 0:
        named = re.search(r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*$", text[at:opening])
        arguments, end = _arguments(text, opening)
        arguments = [_templates(argument) for argument in arguments]
        name = named[0] if named else ""
        shown.append(text[at : opening - len(name)])
        name, kept = _template(name, arguments)
        shown.append(name if kept is None else f"{name}<{', '.join(kept)}>")
        at = end
    shown.append(text[at:])
    return "".join(shown)


def _protected(text):
    """``text`` with bracketed operator names replaced by placeholders, and a function
    that puts them back."""
    saved = []

    def keep(match):
        saved.append(match[0])
        return f"\x00{len(saved) - 1}\x00"

    def restore(value):
        return re.sub(r"\x00([0-9]+)\x00", lambda found: saved[int(found[1])], value)

    return _OPERATOR.sub(keep, text), restore


def _standard_names(name):
    """A C++ frame name with the standard library's types as code writes them, or the
    name unchanged where its brackets cannot be read."""
    text, restore = _protected(_INLINE.sub("std::", name))
    try:
        return restore(_templates(text))
    except ValueError:
        return name


def standard_type(name):
    """A C++ type name as code writes it (`std::vector<std::string>`, not libc++'s
    `std::__1::vector<std::__1::basic_string<char, ...>, ...>`), as probe tables show it."""
    return _standard_names(name) if "std::" in name else name


def _top_level(text):
    """Each character of ``text`` with the depth of brackets around it."""
    depth = 0
    for char in text:
        if char in ")]}>":
            depth -= 1
        yield char, depth
        if char in "([{<":
            depth += 1


def short_function(name):
    """A frame name without its parameters and return type, for a table's narrow
    column: `trim_left` for `trim_left(std::__1::basic_string<...> const&)`,
    `same<int>` for `int same<int>(int)`, `geo::Box::area` for
    `geo::Box::area(int) const`. Any other name as ``display_function`` shows it."""
    shown = display_function(name)
    optimized = " [opt]" if shown.endswith(" [opt]") else ""
    text, restore = _protected(shown[: len(shown) - len(optimized)])
    text = re.sub(r"(?:\s*(?:\b(?:const|volatile|noexcept|override|final)\b|&&|&))*\s*$", "", text)
    if not text.endswith(")"):
        return shown
    depths = list(_top_level(text))
    if any(depth < 0 for _, depth in depths) or depths[-1][1] != 0:
        return shown
    opening = max(i for i, (char, depth) in enumerate(depths) if char == "(" and depth == 0)
    head = text[:opening]
    spaces = [i for i, (char, depth) in enumerate(depths[:opening]) if char == " " and depth == 0]
    if (operator := re.search(r"\boperator\b", head)) is not None:
        spaces = [i for i in spaces if i < operator.start()]
    start = spaces[-1] + 1 if spaces else 0
    if any(char == "." and depth == 0 for char, depth in depths[start:opening]):
        # Java's `Main.run(int)`: its own short signature.
        return shown
    found = restore(head[start:]).strip()
    unnamed = found.startswith("(anonymous namespace)")
    if (
        not found
        or found.startswith("(")
        and not unnamed
        or found.endswith(")")
        and "operator" not in found
    ):
        return shown
    return found + optimized


def function_labels(names):
    """{frame name: what a table shows for it}: its ``short_function``, except where two
    different frames would read alike (overloads, `twice(int)` and
    `twice(std::string const&)`), which keep their signatures as ``display_function``
    shows them, or else in full."""
    distinct = list(dict.fromkeys(name for name in names if isinstance(name, str)))
    tiers = (short_function, display_function, str)
    level = dict.fromkeys(distinct, 0)
    while True:
        labels = {name: tiers[level[name]](name) for name in distinct}
        seen = {}
        for name, label in labels.items():
            seen.setdefault(label, []).append(name)
        clashing = [n for group in seen.values() if len(group) > 1 for n in group]
        clashing = [name for name in clashing if level[name] < len(tiers) - 1]
        if not clashing:
            return labels
        for name in clashing:
            level[name] += 1


def shortened_note(labels, instance=None):
    """The note under a table whose function column ``labels`` ({name: label}, see
    ``function_labels``) shows a name other than the one stored, or None: SQL compares
    the stored name, so `WHERE function = 'trim_left'` finds nothing. Its example is
    what ``short_function`` gives, which every overload of the name shares. ``instance``:
    an instance label the column shows (`cz::main::{closure#0}`, `cz::gen_apply<i32>`),
    which only the `instance` column matches: ``short_function`` gives every closure of a
    function, and every instance of a generic, one name."""
    changed = [name for name, label in labels.items() if label != name]
    if not changed and instance is None:
        return None
    note = (
        "note: function shows short frame names; the evidence store and --json keep the "
        "debugger's whole names, which SQL compares: use "
    )
    if changed:
        example = short_function(changed[0]).replace("'", "''")
        note += f"short_function(function) = '{example}' (any overload of it) or the whole name"
    if instance is None:
        return note
    shown = instance.replace("'", "''")
    return (
        note
        + ("; " if changed else "")
        + f"instance = '{shown}' for a closure or generic instance as shown (short_function "
        "merges a function's closures and a generic's instances)"
    )


def hits_named(hits):
    """Hit numbers as people read them: `hit 4`, `hits 4 and 6`, `hits 1 to 3, 5 and 7`
    (a run of three or more as `1 to 3`)."""
    numbers = sorted(set(hits))
    parts, start = [], 0
    for index, number in enumerate(numbers):
        if index + 1 < len(numbers) and numbers[index + 1] == number + 1:
            continue
        run = numbers[start : index + 1]
        parts += [f"{run[0]} to {run[-1]}"] if len(run) > 2 else [str(item) for item in run]
        start = index + 1
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"hit{'s' if len(numbers) > 1 else ''} {listed}"


def changes_named(ats):
    """Changes' `LINE@HIT`s as people read them, each line's hits together, in the order
    of each line's first: `line 49, hits 1 to 6; line 52, hit 2`."""
    lines = {}
    for at in ats:
        line, _, hit = str(at).partition("@")
        lines.setdefault(line, []).append(int(hit))
    return "; ".join(f"line {line}, {hits_named(hits)}" for line, hits in lines.items())
