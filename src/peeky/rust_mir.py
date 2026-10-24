"""Which owned Rust locals are initialized at a stop, from the compiler's own MIR (#53).

A dropped, moved or not-yet-assigned `Vec`/`String` local keeps a header that passes
every sanity check while its pointer reaches freed or reused memory, so its contents
are read only with a proof. rustc's MIR has the facts: every move (`move _n`), drop
(`drop(_n)`), assignment and storage marker is explicit, which LLVM IR cannot show
(it passes `ptr %x` for a move and a borrow alike).

Peeky builds the debugged binary crate with `-Zmir-opt-level=0` (through a rustc
wrapper, for that crate only). No MIR pass then turns a move into a copy, the dumped
`PreCodegen` MIR is exactly what codegen used, and its basic blocks keep their names in
the LLVM IR (`bb0` is `start`). The IR's debug locations say which blocks hold code of
the stop line, with macro-expanded code already attributed to the line that invoked
the macro.

The proof for a local at a stop: in every MIR block holding code of the stop line
(including blocks codegen merged into it), the local is initialized at every place that
code can be. That is, before each code-emitting statement spanning the line, or spanning
another file (external macro code takes the invoking line), following moves, drops and
assignments statement by statement from the block's entry state, which a must dataflow
over the whole CFG (unwind edges included) gives. A block whose code of the line no
statement explains (other than a checked caller's result store at its start) must keep
the local throughout. Anything not understood gives no proof: an unparsed statement or
terminator, a coroutine whose state is not understood, a debug binding through a
projection, a name bound twice, a body that cannot be identified positively, an IR
block of unknown origin, a stop in another file. Unsafe code that duplicates ownership
(`ptr::read`) is outside the proof.

A coroutine (an async body) is a body like any other once each local its state keeps
across a suspension is renamed a local of its own, and each suspension flows into the
block resuming it (#116). A function LLVM inlined everywhere (`#[inline(always)]`) has
no IR of its own: its MIR body is found by the lines of its inlined copies, and every
statement of the stop line is a place the stop can be.
"""

import bisect
import functools
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from peeky.rust_ir import ir_string
from peeky.textlines import split_lines

MAX_MIR_BYTES = 64 * 1024 * 1024
MAX_ITERATIONS = 10_000
# The dataflow's budget per reachable block, beyond MAX_ITERATIONS in all.
STEPS_PER_BLOCK = 50

_HEADER = re.compile(r"^fn (.+?)\((.*)\) -> .* \{$")
_ARGUMENT = re.compile(r"(?:^|, )_([0-9]+): ")
_LOCAL = re.compile(
    r"^ +let (?:mut )?_([0-9]+): (.*?); +// (?:return place )?in scope [0-9]+ at "
    r"([^:]+):([0-9]+):[0-9]+"
)
_DEBUG = re.compile(r"^ +debug (.+?) => (.+?); +// in scope ([0-9]+) at ([^:]+):([0-9]+):[0-9]+")
_BLOCK = re.compile(r"^    bb([0-9]+)(?: \(cleanup\))?: \{$")
# Greedy: the span comment is the line's last; a string constant may contain one too.
_STATEMENT = re.compile(r"^        (.+;) +// scope ([0-9]+) at (.+)$")
_SCOPE = re.compile(r"^ +scope ([0-9]+)(?: \(inlined .*\))? \{$")
_SPAN = re.compile(r"^([^:]+):([0-9]+):[0-9]+")
_FULL_SPAN = re.compile(r"^([^:]+):([0-9]+):([0-9]+): ([0-9]+):([0-9]+)")
_SPAN_END = re.compile(r"^[^:]+:[0-9]+:[0-9]+: ([0-9]+):[0-9]+")
# Statements that emit no code: a block's `()` value spans the whole block, and must not
# put its scope on the closing `}` (`} else if b.x > 0`, where only the outer one is).
_SPAN_ONLY = re.compile(
    r"(?:StorageLive|StorageDead|FakeRead|PlaceMention|AscribeUserType|Retag|Coverage|nop)\b"
    r"|_[0-9]+ = const \(\);|goto "
)
_WHOLE_LOCAL = re.compile(r"^_([0-9]+)$")
_WHOLE_ASSIGNMENT = re.compile(r"^_([0-9]+) = (.+);$")
_PART_ASSIGNMENT = re.compile(r"^\(+\*?_([0-9]+)[.: )].*? = (.+);$")
# Statements that never use a local, though they name it.
_NOT_A_USE = re.compile(r"^(?:StorageLive|FakeRead|PlaceMention|AscribeUserType|Retag|Coverage)\b")
# A local's mention in a statement (`move _11`, `(*_43)`); never part of a number or
# a name (`0_usize`, `bar_2`).
_MENTIONED = re.compile(r"(?<![\w])_([0-9]+)\b")
_RAW_POINTER = re.compile(
    r"\*const |\*mut |(?:(?:std|core)::ptr::)?NonNull<|(?:(?:std|core)::sync::atomic::)?AtomicPtr<"
)
_FN_POINTER = re.compile(r"(?:for<[^<>]*> )?(?:unsafe )?(?:extern \S+ )?fn\(")
# A raw pointer itself (`*const Engineer`), not a value holding one.
_RAW_ONLY = re.compile(r"\*(?:const|mut) ")
# A type that borrows: a reference, a raw pointer, one holding a lifetime, or a closure
# or coroutine (whose captures may be references).
_BORROWING = re.compile(r"&|\*const |\*mut |NonNull<|AtomicPtr<|'|\{closure@|\{coroutine@|\{async")


def _projected_type(text):
    """The type a binding through a projection names, `(_1.0: std::boxed::Box<Pt>)` ->
    `std::boxed::Box<Pt>`: split at the projection's own `: `, outside its parentheses
    (`(((*_36) as variant#3).0: T)`), since a type can hold `: ` itself
    (`{closure@src/main.rs:27:22: 27:35}`). None for anything else."""
    if text.startswith("(*(") and text.endswith("))"):
        # A capture by reference, dereferenced: `(*((*_1).0: &T))` binds a `T`.
        referent = _projected_type(text[2:-1])
        found = re.fullmatch(r"&(?:mut )?(.+)", referent or "")
        return found[1] if found else None
    if not (text.startswith("(") and text.endswith(")")):
        return None
    inner, depth = text[1:-1], 0
    for index, char in enumerate(inner):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and inner.startswith(": ", index):
            return inner[index + 2 :] or None
    return None


_MOVE = re.compile(r"\bmove\s+[(*]*_([0-9]+)\b")
_ASSIGN = re.compile(r"^_([0-9]+) = ")
_STORAGE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_([0-9]+)\);$")
_DROP = re.compile(r"^drop\([(*]*_([0-9]+)\b")
# Terminators that branch on a value at run time: an instruction at the line's address.
_BRANCHES = re.compile(r"^(?:assert\(!?(?:move|copy) |switchInt\((?:move|copy) )")
# A borrow of a local's own storage: no `*` before the local in the place.
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_DROP_PLACE = re.compile(r"^drop\((.+)\) -> ")
# A place of a local's own, no deref in it: a field (`(_1.1: Box<Pt>)`, `((_1.0: S).2:
# T)`) or an element (`_1[_5]`) of the local `_1` itself (#108).
_DIRECT_PLACE = re.compile(r"^\(*_([0-9]+)[.\[]")
# A constant stored in a local (`_20 = const false;`, a drop flag).
_CONSTANT_STORE = re.compile(r"^_([0-9]+) = const [^;]*;$")
# A constant stored through a pointer (`(*_17)[_7] = const 7_u32;`, of `b[0] = 7;`).
_CONSTANT_THROUGH = re.compile(r"^\(+\*[^=]* = const [^;]*;$")
_STORE = re.compile(r"^(\(+\*_[0-9]+\).*?|\(\*_[0-9]+\)) = ")
# A write through a pointer, and the local its place starts from: `_35` of `((*_35).1:
# T) = …`, `_6` of `(*((*_6).0: &mut u32)) = …` (#311).
_STORE_BASE = re.compile(r"^\(+\*[(*]*_([0-9]+)\b")
_BOX_TYPE = re.compile(r"^(?:(?:std|alloc)::boxed::)?Box<")
# `copy_nonoverlapping(dst = move _5, src = move _6, count = const 1_usize);`: a store
# through `dst` of what `src` points at.
_COPY_NONOVERLAPPING = re.compile(
    r"^copy_nonoverlapping\(dst = (?:copy|move) _([0-9]+), src = (?:copy|move) _([0-9]+),"
)
# What a callee may write through even where it reaches it only through a shared `&`
# (#311 review round 4): a raw pointer, `NonNull`, a cell or lock.
_SHARED_WRITABLE = re.compile(
    r"\*mut |\*const |NonNull<|Cell<|UnsafeCell<|Atomic|Mutex<|RwLock<|OnceLock<"
)
# A reference in a type: `&`, `&mut `, `&'a `, `&'static mut ` (group 1 for `mut`).
_REFERENCE = re.compile(r"&(?:'[\w?]+ )?(mut )?")
# A reference or raw pointer itself (`&mut [u32]`, `*mut u32`, `NonNull<T>`).
_POINTER_TYPE = re.compile(r"^(?:&|\*const |\*mut |(?:(?:std|core)::ptr::)?NonNull<)")
# `_23 = copy ((BOX.0: Unique<T>).0: NonNull<T>) as *const T (Transmute)`: BOX is a Box
# place (`_1`, `(_1.2: Box<T>)`, `((*_13).1: Box<Inner>)`).
_BOX_POINTER = re.compile(
    r"^_([0-9]+) = copy \(\((.+?)\.0: (?:std::ptr::|core::ptr::)?Unique<.*\bNonNull<.*\) "
    r"as \*(?:const|mut) .*\(Transmute\);$"
)
# A copy of a Box held in a place (`_17 = copy ((*_37).1: Box<Inner>)`) or of such a
# copy (`_18 = copy _17`): the built MIR derefs a nested Box through it.
_BOX_COPY = re.compile(
    r"^_([0-9]+) = copy (\(.*: (?:std::boxed::|alloc::boxed::)?Box<.*>\)|_[0-9]+);$"
)
# A drop through a pointer: `drop((*_1))`, `drop(((*_8).1: T))`.
_DROP_THROUGH = re.compile(r"^drop\(\(*\*\(*_([0-9]+)\b")
# A call's arguments (`f(move _7, copy _8) -> [return: ...]`).
_CALL_ARGUMENTS = re.compile(r"^(?:.*? = )?[^ (][^(]*?\((.*)\) -> ")
# Types a shared reference can still change through (#164), and the words of a type
# name: a primitive, or a path in the standard library.
_INTERIOR = re.compile(r"Cell<|Mutex<|RwLock<|Atomic|Once|Lazy|UnsafeCell<|Rc<|Arc<")
_TYPE_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")
_LIFETIME = re.compile(r"'[A-Za-z_]+")
_PRIMITIVE_WORDS = frozenset(
    "str bool char i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 mut const "
    "dyn".split()
)
_STD_PATHS = ("std::", "core::", "alloc::")
# Standard-library types that hold no pointer but what their type arguments hold
# (#164): with such arguments only, a value of them holds no borrow.
_FREE_PATHS = frozenset(
    f"{crate}::{path}"
    for crate, paths in (
        (
            "std",
            "string::String vec::Vec boxed::Box option::Option result::Result "
            "collections::VecDeque collections::HashMap collections::HashSet "
            "collections::BTreeMap collections::BTreeSet collections::hash::map::HashMap "
            "collections::hash::set::HashSet hash::RandomState collections::hash_map::RandomState "
            "rc::Rc sync::Arc cell::Cell cell::RefCell alloc::Global",
        ),
        (
            "alloc",
            "string::String vec::Vec boxed::Box collections::VecDeque "
            "collections::BTreeMap collections::BTreeSet rc::Rc sync::Arc alloc::Global",
        ),
        ("core", "option::Option result::Result cell::Cell cell::RefCell"),
    )
    for path in paths.split()
)


# Standard views whose drop does nothing with what they borrow (#164); `Drain`,
# `Splice`, `PeekMut` and guards are not among them.
_QUIET_PATHS = frozenset(
    f"{crate}::{path}"
    for crate in ("std", "core")
    for path in (
        "str::Chars str::CharIndices str::Bytes str::Split str::SplitWhitespace "
        "str::SplitN str::RSplit str::Lines str::Matches str::MatchIndices "
        "slice::Iter slice::Windows slice::Chunks fmt::Arguments fmt::rt::Argument "
        "iter::Enumerate iter::Rev iter::Skip iter::Take iter::Peekable "
        "collections::hash_map::Keys collections::hash_map::Values "
        "collections::hash_map::Iter ops::Range ops::RangeFrom ops::RangeTo "
        "option::Option result::Result"
    ).split()
)


def _pointer_free(kind, generics, free_types=frozenset()):
    """Whether a value of ``kind`` (a type as the MIR prints it) provably holds no
    pointer, so no borrow (#164): primitives, `String`, and arrays, tuples and the
    standard containers of these, and the crate's own types ``free_types`` names (whose
    fields the debug information shows hold none, see rust.RustSession._free_types). Not
    a reference, a raw pointer, a lifetime, a trait object (`dyn Fn()`: the MIR prints no
    lifetime for it), an opaque `impl` type, a closure or coroutine, a type parameter of
    ``generics`` (any, where they are not known), another type of the crate's, or a
    type not known."""
    if kind is None or generics is None or mentions(kind, generics):
        return False
    if re.search(r"[&*'{}]|\b(?:dyn|impl|fn|for)\b", kind):
        return False
    return all(
        word in _PRIMITIVE_WORDS or word in _FREE_PATHS or word in free_types
        for word in _TYPE_WORDS.findall(kind)
    )


# The standard library's views and guards that write through a `&mut` their types don't
# show (#311 review rounds 2 and 3), by the last segment of their path: an `Entry`, the
# `IterMut` family, `RefMut`, `Drain`, `ChunksMut` and the like. A view not listed only
# reads (`ChunksExact`, `btree_map::Iter`, `path::Components`), unless what it was made
# from shows a writable pointer (``Body._may_write_through``).
_MUTABLE_VIEWS = frozenset(
    "Entry OccupiedEntry VacantEntry RawEntryMut IterMut ValuesMut RefMut Drain DrainSorted "
    "ExtractIf Splice PeekMut CursorMut ChunksMut ChunksExactMut RChunksMut RChunksExactMut "
    "SplitMut SplitNMut RSplitMut RSplitNMut SplitInclusiveMut ChunkByMut ArrayChunksMut "
    "MutexGuard MappedMutexGuard RwLockWriteGuard MappedRwLockWriteGuard".split()
)


def _shared_reference(kind):
    """Whether ``kind`` is a shared reference (`&Hold<'_>`, `&&mut Vec<u32>`), through
    which safe code writes only into a cell or through a raw pointer (#311 review round
    4)."""
    found = kind is not None and _REFERENCE.match(kind)
    return bool(found) and not found[1]


def _unshared(kind):
    """``kind`` without what each shared reference in it refers to (#311 review round 4):
    `&Hold<'_>` is `&`, `(&&mut Vec<u32>, &mut u32)` is `(&, &mut u32)`. A callee writes
    through no `&mut` or mutable view it reaches only through a `&`."""
    out, at = [], 0
    while at < len(kind):
        found = _REFERENCE.match(kind, at) if kind[at] == "&" else None
        if not found or found[1]:
            end = found.end() if found else at + 1
            out.append(kind[at:end])
            at = end
            continue
        out.append("&")
        depth, at = 0, found.end()
        while at < len(kind):
            if kind.startswith("->", at):
                at += 2
                continue
            if kind[at] in "<([{":
                depth += 1
            elif kind[at] in ">)]}" or (kind[at] in ",;" and not depth):
                if not depth:
                    break
                depth -= 1
            at += 1
    return "".join(out)


def _may_write(kind, generics, shared=False):
    """Whether a value of the MIR type ``kind`` shows that a callee given it may write
    through a pointer it holds (#311 review): a raw pointer, `NonNull`, a cell or lock,
    wherever it is in the type (``_SHARED_WRITABLE``); a `&mut`, or one of the standard
    library's mutable views (``_MUTABLE_VIEWS``: `hash_map::Entry<'_, u32, u32>`,
    `slice::IterMut<'_, u32>`), that it does not reach only through a shared `&`
    (``_unshared``: `(&mut Vec<u32>, u32)`, not `&&mut Vec<u32>`), nor at all where
    ``shared`` (the value is itself reached only through one, round 4); a type parameter,
    or a type not known. Not any other type (round 3: `slice::ChunksExact<'_, u32>` only
    reads); a crate's type, a closure or another view writes only where what it was made
    from shows it (``Body._may_write_through``)."""
    if kind is None or generics is None or mentions(kind, generics):
        return True
    if _SHARED_WRITABLE.search(kind):
        return True
    if shared:
        return False
    kind = _unshared(kind)
    if any(found[1] for found in _REFERENCE.finditer(kind)):
        return True
    return any(
        word.startswith(_STD_PATHS) and word.rsplit("::", 1)[-1] in _MUTABLE_VIEWS
        for word in _TYPE_WORDS.findall(_LIFETIME.sub("", kind))
    )


def _quiet_drop(kind, generics):
    """Whether dropping a value of ``kind`` provably runs no code that reads what it
    borrows (#164): references and raw pointers (no drop at all), and the standard
    containers and views of them (`Vec<&str>`, `Option<&str>`, `std::str::Chars<'_>`),
    whose drop frees only what they own. Not a type of the crate's (it may implement
    `Drop`), a trait object, a closure, an `impl` type, a type parameter, a standard
    type whose drop works on what it borrows (`Drain`), or a type not known."""
    if kind is None or generics is None or mentions(kind, generics):
        return False
    if re.search(r"[{}]|\b(?:dyn|impl|fn|for)\b", kind):
        return False
    return all(
        word in _PRIMITIVE_WORDS or word in _FREE_PATHS or word in _QUIET_PATHS
        for word in _TYPE_WORDS.findall(_LIFETIME.sub("", kind))
    )


_NOT_A_CALL = re.compile(
    r"(?:drop|assert|switchInt|goto|falseEdge|falseUnwind|yield|return|resume|unreachable)\b"
)
# Types whose values hold no pointer, so are never an alias.
_POINTERLESS = re.compile(
    r"[iu](?:8|16|32|64|128|size)|f32|f64|bool|char|\(\)|(?:std|alloc)::string::String"
)
# A move or drop through a pointer: `move ((*_23).1: T)`, `drop(((*_23).1: T))`.
_THROUGH = re.compile(r"(?:\bmove\s+|^drop\()\(*\*_([0-9]+)\b")
_WRITABLE_BORROW = re.compile(r"&(?:mut |raw const |raw mut )\(*_([0-9]+)\b")
_DIRECT_BORROW = re.compile(r"&(?:mut |raw const |raw mut |fake \w+ )?\(*_([0-9]+)\b")
_ANY_LOCAL = re.compile(r"\b_([0-9]+)\b")
# `format_args!`'s calls, which return what holds the pointers they are given (an
# `Argument` holds `&s.a`), never a value read through them. The dump may shorten the
# call's path (`Arguments::<'_>::new::<7, 2>(…)`), but not the declared type of what
# it returns, which must be the standard library's too.
_FORMAT_CALL = re.compile(r"(?:(?:core|std)::fmt::(?:rt::)?)?Arguments?::<'_>::new\w*\b")
_FORMAT_TYPE = re.compile(r"(?:core|std)::fmt::(?:rt::)?Arguments?<'_>")
# A call that formats the `fmt::Arguments` it is given, as std's own `print!`,
# `println!`, `eprint!`, `eprintln!` and `format!` write it (#192 review 3), by its full
# path; MIR names `alloc::fmt::format` `format` alone where no other item is so named
# (``Body._format_reads`` takes that only where none of the crate's own functions is).
_FORMAT_SINK = re.compile(
    r"^_[0-9]+ = (std::io::_e?print|(?:alloc|std)::fmt::format|format)\((?:move|copy) _([0-9]+)\)"
    r" -> "
)
# A formatting argument made from a pointer (`core::fmt::rt::Argument::<'_>::
# new_display::<i64>(move _42)`), and the primitives whose formatting reads the value.
_FORMAT_ARGUMENT = re.compile(
    r"(?:(?:core|std)::fmt::rt::)?Argument::<'_>::new_\w+::<([^<>]+)>\((?:move|copy) _([0-9]+)\)"
)
_FORMAT_ARGUMENTS = re.compile(r"(?:(?:core|std)::fmt::)?Arguments::<'_>::new\w*(?:::<[^()]*>)?\(")
_FORMATTED_PRIMITIVE = re.compile(r"[iu](?:8|16|32|64|128|size)|f32|f64|bool|char")
_NOT_SHARED = re.compile(r"&(?:mut |raw |fake )")
_SUCCESSOR = re.compile(r"\bbb([0-9]+)\b")
_RETURN_EDGE = re.compile(r"\breturn: bb([0-9]+)\b")
_EXITS = re.compile(r"^(?:return|unreachable|resume|abort|terminate)\b")
_NO_CODE = re.compile(r"^(?:StorageLive|StorageDead|Deinit)\(_[0-9]+\);$|^nop;$")
_UNSUPPORTED = re.compile(r"^(?:yield|coroutine_drop|falseEdge|falseUnwind|tailcall|asm!)\b")


@dataclass
class Block:
    statements: list = field(default_factory=list)  # (text, file, line)
    scopes: list = field(default_factory=list)  # each statement's MIR scope
    # each statement's (start column, end line, end column), as the MIR prints them
    # (characters, 1-based), or None
    spans: list = field(default_factory=list)
    terminator: str = ""
    successors: tuple = ()
    returns: int | None = None  # a call's return edge, the only edge that sets its result


@dataclass
class Body:
    path: str
    arguments: frozenset
    file: str | None
    blocks: dict  # {number: Block}
    # {name: [(local or None, file, line, type of a projection or None, scope)]}
    bindings: dict
    supported: bool = True
    # Lines of every span in the body's own file (its own code, and the definitions of
    # same-file macros it expands).
    lines: frozenset = frozenset()
    types: dict = field(default_factory=dict)  # {local: its type as MIR prints it}
    # {(name, file, line, scope): place} for bindings through a projection (a closure's
    # capture `(*((*_1).0: &mut T))`, an async local).
    places: dict = field(default_factory=dict)
    # {(name, file, line): [(end line, end column)]}: where each binding's span ends, as
    # the MIR prints it (characters, 1-based, past the end). A `?`'s desugared bindings
    # end right after the `?` (`residual` spans only it, `val` the expression it tries);
    # a binding the program writes spans its name.
    binding_ends: dict = field(default_factory=dict)
    scopes: dict = field(default_factory=lambda: {0: None})  # {scope: its parent}
    # {(file, line): the scopes of the statements with a span on that line}
    line_scopes: dict = field(default_factory=dict)
    inlined_scopes: set = field(default_factory=set)
    # A coroutine's (#116): the local pointing at its state (`_34 = copy (_1.0: &mut
    # {async fn body of f()})`; -1 if not found), the locals standing for what its state
    # saves across a suspension (`(((*_34) as variant#3).0: &Pt)`), and {suspending
    # block: the block resuming it}, which the dispatch at bb0 jumps to on a later poll.
    coroutine: int | None = None
    # The type parameters of a generic body (IrFunction.generics, set when an IR function
    # is matched to it): a type named like a primitive (`i8`) may be one of them. None
    # until then (a twin a macro generates): any may be.
    generics: tuple | None = None
    saved: frozenset = frozenset()
    resumes: dict = field(default_factory=dict)
    _lent_cache: frozenset | None = None
    _written_fields: frozenset = frozenset()
    _into: dict | None = None
    _declared_names: dict | None = None
    _assigned: dict | None = None
    _entry: dict | None = None
    _direct: set | None = None
    _writable: set | None = None
    _preds: dict | None = None
    _box_temps: dict | None = None
    _box_paths: dict | None = None
    _replaced: dict | None = None
    _alias_cache: dict | None = None
    _alias_edges: dict | None = None
    # The ``generics`` the alias graph was built for: it is rebuilt when they change.
    _alias_generics: tuple | None = ()
    _dropped: set | None = None
    _offset: int | None = None
    _assigned_flows: dict | None = None
    # Per-body caches of what parsing fixes: ``lexical``'s and ``scope_chain``'s answers,
    # each block's assignment events (``_assign_events``), which blocks hold code of a
    # line (``line_code``).
    _lexicals: tuple | None = None
    _chains: dict | None = None
    _assign_cache: dict | None = None
    _line_blocks: dict | None = None
    _copies: dict | None = None
    _live: dict | None = None
    _copy_reads: tuple | None = None
    # (the ``generics`` it was found for, {pointer: ``_stored_into``'s locals}) (#311).
    _stored_cache: tuple | None = None
    # {(local, formatting): {block: (holds, points) on entry}, or None past the budget}
    # (#183: ``copies_at``).
    _copy_flows: dict | None = None
    # {(local, projection): {block: (read, own) on entry}, or None} (#300:
    # ``read_copies_at``), and the locals a writable borrow is taken of.
    _read_flows: dict | None = None
    _writable_borrows: frozenset | None = None
    # {(local, projection): ``_exposed``} (#300).
    _exposed_cache: dict | None = None
    # {local: ``_pointers_to``'s locals} (#192).
    _pointer_cache: dict | None = None
    # ``_format_reads``'s calls and the borrows they format (#192 review 3).
    _format_cache: tuple | None = None
    _lent_from: dict | None = None
    _maybe: dict | None = None
    _moved_cache: set | None = None
    # #156 review: each local's statements (``_mentions``), and the frozen owners of a
    # reference (``_borrows_frozen``) and iterators only `next` borrows
    # (``_only_iterated``), each found once.
    _mention_index: dict | None = None
    _frozen_cache: dict | None = None
    _iterated_cache: dict | None = None

    @property
    def name(self):
        return _segments(self.path)[-1]

    @property
    def parent(self):
        parts = _segments(self.path)
        return parts[-2] if len(parts) > 1 else None

    def predecessors(self):
        if self._preds is None:
            found = {}
            for number, block in self.blocks.items():
                for target in block.successors:
                    found.setdefault(target, set()).add(number)
            self._preds = found
        return self._preds

    def lexical(self, scope):
        """The DWARF lexical block of MIR ``scope``: rustc gives one only to a scope that
        declares a variable (or is inlined); a statement elsewhere is in its parent's.
        0 is the function's own."""
        shape = (len(self.bindings), len(self.scopes), len(self.inlined_scopes))
        if self._lexicals is None or self._lexicals[0] != shape:
            declared = {binding[4] for found in self.bindings.values() for binding in found}
            self._lexicals, self._chains = (shape, declared, {}), None
        _, declared, found = self._lexicals
        if scope not in found:
            at, answer = scope, 0
            for _ in range(4096):
                if at in (None, 0) or at in declared or at in self.inlined_scopes:
                    answer = at or 0
                    break
                at = self.scopes.get(at)
            found[scope] = answer
        return found[scope]

    def local(self, name, declared=None, visible=None):
        """The MIR local an lldb-dap variable shows: `name`, or `name @ file:line` when
        several are in scope. Of a name bound more than once, the one binding in the
        ``visible`` scopes, if there is one. None without a single whole-local binding."""
        bindings = self.bindings.get(name, [])
        if declared is not None:
            file, line = declared
            bindings = [b for b in bindings if Path(b[1]).name == file and b[2] == line]
        bindings = self.in_scopes(bindings, visible)
        if len(bindings) != 1 or bindings[0][0] is None:
            return None
        return bindings[0][0]

    def _assignments(self):
        """{local: [what is assigned to it]}, whole (`_5 = &(*_43);`, calls) or in part
        (`(_3.0: *const Pt) = move _7;`)."""
        if self._assigned is None:
            assigned = {}
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    # A write through it (`((*_1).0: i64) = ...`) changes what it points
                    # at, not where.
                    found = _WHOLE_ASSIGNMENT.match(text) or (
                        None if re.match(r"\(+\*_", text) else _PART_ASSIGNMENT.match(text)
                    )
                    if found:
                        assigned.setdefault(int(found[1]), []).append(found[2])
            self._assigned = assigned
        return self._assigned

    def scope_chain(self, scope):
        """The names of the variables each DWARF lexical block declares, sorted, from the
        block of MIR ``scope`` (see ``lexical``) out to the function's own (#115)."""
        self.lexical(0)
        if self._chains is None:
            self._chains = {}
        if scope not in self._chains:
            declared = self._declared()
            chain, at = [], self.lexical(scope)
            for _ in range(4096):
                if at in (None, 0):
                    break
                chain.append(declared.get(at, ()))
                at = self.lexical(self.scopes.get(at))
            chain.append(declared.get(0, ()))
            self._chains[scope] = tuple(chain)
        return self._chains[scope]

    def _declared(self):
        """{scope: the names of the variables it declares, sorted}."""
        if self._declared_names is None:
            found = {}
            for name, bindings in self.bindings.items():
                for binding in bindings:
                    found.setdefault(binding[4], []).append(name)
            self._declared_names = {scope: tuple(sorted(names)) for scope, names in found.items()}
        return self._declared_names

    def chain_scopes(self, block, function=None):
        """The MIR scopes whose bindings lldb-dap shows at a stop in ``block`` (#115): the
        scopes with its chain of names and those enclosing them, where the function's
        debug info and IR (``function``) have as many blocks with that chain as the body
        has scopes; else None."""
        if function is None or not function.scoped:
            return None
        declared = self._declared()
        matched = [
            scope
            for scope in {0, *declared}
            if (scope == 0 or scope == self.lexical(scope))
            and self.scope_chain(scope) == block.chain
        ]
        count = block.chains.get(block.chain)
        if not matched or count != len(matched) or function.chains.get(block.chain) != count:
            return None
        found = set()
        for scope in matched:
            for _ in range(4096):
                if scope is None:
                    break
                found.add(scope)
                scope = self.scopes.get(scope)
        return found

    def _in_block(self, found, block, line, function, breakpoint=False):
        """The places of ``found`` ({position: its statements, or None}) whose code is in
        the lexical block the stop's pc is in (#115), or all of them where that block is
        not told apart: every MIR scope a statement placed there is in must have as many
        DWARF blocks with its chain of names as the body has scopes with it, just one of
        them the stop's, and the IR (``function``) must have as many blocks with the
        stop's chain as the DWARF, just one with code of ``line``. Code no statement
        explains stays.

        A line breakpoint stops at the line's lowest address in that block, so at or
        before the IR's first instruction of the line there that surely leaves code
        (``_cut``): places past it go.

        Returns (the places kept, the MIR scope told apart or None)."""
        scopes = {
            self.lexical(_scope(self.blocks[number], index))
            for owners in found.values()
            if owners
            for number, index in owners
        }
        if (
            not scopes
            or scopes & self.inlined_scopes
            or function is None
            or not function.agrees(block, line)
        ):
            return found, None
        declared = self._declared()
        counts = {}
        for scope in {0, *declared}:
            if scope == 0 or scope == self.lexical(scope):
                chain = self.scope_chain(scope)
                counts[chain] = counts.get(chain, 0) + 1
        chains = {scope: self.scope_chain(scope) for scope in scopes}
        if any(block.chains.get(chain) != counts.get(chain) for chain in chains.values()):
            return found, None
        matched = [scope for scope, chain in chains.items() if chain == block.chain]
        if len(matched) != 1 or block.chains.get(block.chain) != counts.get(block.chain):
            return found, None
        kept = {
            position: owners
            for position, owners in found.items()
            if owners is None
            or any(self.lexical(_scope(self.blocks[n], i)) == matched[0] for n, i in owners)
        }
        if kept and breakpoint and (cut := self._cut(function, line, block.chain)) is not None:
            kept = {
                position: owners
                for position, owners in kept.items()
                if (order := self._address_order(position, function)) is None or order <= cut
            }
        if not kept:
            return found, None
        return kept, matched[0]

    def _cut(self, function, line, chain):
        """The last place (``_address_order``) a line breakpoint's stop in the lexical
        block with ``chain`` can be, or None if unknown (#115). LLDB stops at the line's
        lowest address in the block. The IR's first instruction of the line there that
        surely leaves code (a call, a store of bytes, a branch on a value) is at or after
        it: opt-level 0 keeps the IR's order for addresses. Codegen emits the MIR blocks
        merged into an IR block in order, each call terminator one call there or none
        (inlined), so that instruction is before the MIR block of the IR's next call
        ends, which is that many calls later in the MIR or a few more."""
        for place, label in enumerate(function.blocks):
            for number, named, calls in function.sure.get(label, ()):
                if number != line or named != chain:
                    continue
                found = re.fullmatch(r"bb([0-9]+)", label)
                if label != "start" and found is None:
                    return None
                head = int(found[1]) if found else 0
                ir = self._ir_chain(head, function) if head in self.blocks else None
                if ir is None or ir[0] != label:
                    return None
                members = ir[1]
                callers = [
                    member
                    for member in members
                    if _call(self.blocks[member], len(self.blocks[member].statements) - 1)
                    or _DROP.match(self.blocks[member].terminator)
                ]
                # A call LLVM inlined (or a drop with no glue) leaves no call of its own:
                # the IR's call after the instruction is then the MIR's at most that many
                # calls later.
                missing = len(callers) - function.calls.get(label, -1)
                if missing < 0 or missing > len(callers):
                    return None
                end = callers[calls + missing] if calls + missing < len(callers) else members[-1]
                return place, members.index(end), len(self.blocks[end].statements) - 1
        return None

    def _address_order(self, position, function):
        """Where ``position``'s code is in ``function``'s IR (the label's place, the MIR
        block's place among those merged into it, the statement's), or None."""
        chain = self._ir_chain(position[0], function)
        if chain is None:
            return None
        label, members = chain
        labels = {name: index for index, name in enumerate(function.blocks)}
        if label not in labels:
            return None
        return labels[label], members.index(position[0]), position[1]

    def stop_positions(
        self, blocks, line, file, foreign, facts=None, shared=None, within=None, function=None
    ):
        """[(block, index)]: where a stop on ``line`` of ``file`` can be in ``blocks``,
        before that statement runs; None if a block is missing. A statement is the
        line's when it starts on it (a call: spans it, `w\n.len()`'s is on `.len()`'s
        line), or spans another file or none when the line may invoke a macro
        (``foreign``). After a call or drop of the line is its target's start; code of
        the line that no statement of a block explains (a result store) is anywhere in
        that block, unless another block codegen merged into the same IR block
        (``shared``: {block: the blocks of its IR block}) has statements of the line.

        ``facts`` (a ``LineFacts``) says the stop is a line breakpoint's: LLDB stops at
        the line's lowest address in each DWARF lexical block, and the stop's column is
        known. Code of the line in a block every predecessor branches to from the line's
        own code of the same lexical block is never the lowest: rustc appends an IR
        block when first branched to, and opt-level 0 keeps that order. Only a call or
        branch the IR shows there, not inlined, counts as such code. Where the column
        cannot be a statement's (its own, a macro's on the line, or within a call), the
        stop is not before it.

        ``within`` (a ``StopBlock``): the DWARF lexical block the stop's pc is in, which
        keeps only the places in its MIR scope, where it and the IR's ``function`` tell
        that apart, and at a line breakpoint, only those up to ``_cut`` (#115)."""
        found = self._placed(blocks, line, file, foreign, facts, shared, function)
        if found is None:
            return None
        if within is not None:
            found, _ = self._in_block(found, within, line, function, facts is not None)
        if facts is None or facts.column is None:
            return sorted(found)
        kept = [
            position
            for position, owners in found.items()
            if owners is None or any(self._could_be(o, i, line, file, facts) for o, i in owners)
        ]
        return sorted(kept or found)

    def stop_scope(self, blocks, line, file, foreign, within, shared=None, function=None):
        """The MIR scope of the DWARF lexical block a stop on ``line`` is in (``within``,
        a ``StopBlock``), where the line's places tell it apart and no code of the line
        there goes unexplained (#115); else None. Its bindings and those of the scopes
        around it are the ones lldb-dap shows, where two scopes have the same names
        (two `for item in items` loops)."""
        found = self._placed(blocks, line, file, foreign, None, shared, function)
        if found is None:
            return None
        kept, scope = self._in_block(found, within, line, function)
        if scope is None or None in kept.values():
            return None
        return scope

    def _placed(self, blocks, line, file, foreign, facts=None, shared=None, function=None):
        """{position: [(block, index) of each statement whose code is there], or None
        for code of the line no statement explains} (see ``stop_positions``).

        A call spanning another file is taken for the line's (a macro it may invoke)
        unless the IR's ``function`` places every instruction of the block holding it
        (``IrFunction.call_lines``) and none on this line: `format!`'s call on line 4
        (`label = format!(…)`, its code there or inlined from there) returns into the
        block of line 5's `println!`, whose stop is past the store (#222)."""
        predecessors, found = self.predecessors(), {}
        explained = set()

        def elsewhere(number):
            # The block's call is another line's: every instruction of its IR block (code
            # inlined there by its call's site) says where it is, none is on this line,
            # and no `invoke` stores its result in the next block (#230 review: an
            # unlocated one could be anywhere).
            if function is None or self.blocks[number].statements[-1][1] in (file, None):
                return False
            lines = function.call_lines(number)
            return lines is not None and line not in lines

        def owned(number, index):
            text, spanned, spanned_line = self.blocks[number].statements[index]
            if _NO_CODE.match(text):
                return False
            if spanned != file:
                return spanned is None or foreign
            if spanned_line == line:
                return True
            span = _span(self.blocks[number], index)
            return (
                _call(self.blocks[number], index)
                and span is not None
                and spanned_line < line <= span[1]
            )

        def place(position, number, index):
            # The statement whose code is at ``position``: its scope and possible columns.
            if position not in found:
                found[position] = []
            if found[position] is not None:
                found[position].append((number, index))

        lines = {}
        for number in sorted(blocks):
            block = self.blocks.get(number)
            if block is None:
                return None
            indexes = [index for index in range(len(block.statements)) if owned(number, index)]
            # A predecessor's call of the line returning here stores its result here
            # (after an `invoke`, or an inlined body).
            stores = [
                other
                for other in sorted(predecessors.get(number, ()))
                if _target(self.blocks[other]) == number
                and _call(self.blocks[other], len(self.blocks[other].statements) - 1)
                and owned(other, len(self.blocks[other].statements) - 1)
                and not elsewhere(other)
            ]
            lines[number] = (indexes, stores)
            if indexes or stores:
                explained.add(number)
        for number in sorted(blocks):
            block = self.blocks[number]
            terminator = len(block.statements) - 1
            indexes, stores = lines[number]
            if not indexes and not stores:
                if not (set((shared or {}).get(number, ())) - {number}) & explained:
                    for index in range(len(block.statements)):
                        found[(number, index)] = None
                continue
            candidates = [((number, index), number, index) for index in indexes]
            candidates += [
                ((number, 0), other, len(self.blocks[other].statements) - 1) for other in stores
            ]
            for index in indexes:
                if index == terminator and (_call(block, index) or _DROP.match(block.terminator)):
                    target = _target(block)
                    if target is not None and (
                        facts is None or not self._sure(number, index, line, file, facts)
                    ):
                        candidates.append(((target, 0), number, index))
            for position, owner, index in candidates:
                if facts is not None and number != 0:
                    lexical = self.lexical(_scope(self.blocks[owner], index))
                    froms = predecessors.get(position[0], ())
                    if (
                        position[0] == number
                        and froms
                        and all(
                            other in blocks and self._sure_end(other, lexical, line, file, facts)
                            for other in froms
                        )
                    ):
                        continue
                place(position, owner, index)
        return found

    def anywhere(self, blocks):
        """Every place in ``blocks``, and after each call or drop ending one."""
        found = set()
        for number in blocks:
            block = self.blocks.get(number)
            if block is None:
                return None
            found.update((number, index) for index in range(len(block.statements)))
            terminator = len(block.statements) - 1
            if _call(block, terminator) or _DROP.match(block.terminator):
                if (target := _target(block)) is not None:
                    found.add((target, 0))
        return sorted(found)

    def covering(self, line, file, foreign):
        """The blocks holding code of ``line`` that no IR places (#116): with a statement
        that emits code there (one starting on it, or a call spanning it: a method call's
        code is on its name's line), or that spans another file where the line may
        invoke a macro (``foreign``)."""
        found = set()
        for number, block in self.blocks.items():
            for index, (text, spanned, spanned_line) in enumerate(block.statements):
                if _NO_CODE.match(text) or spanned is None:
                    continue
                if spanned != file:
                    if not foreign:
                        continue
                elif spanned_line != line:
                    span = _span(block, index)
                    if not (_call(block, index) and span and spanned_line < line <= span[1]):
                        continue
                found.add(number)
                break
        return found

    def initialized_before(self, local, positions):
        """True if ``local`` is initialized at each of ``positions`` ((block, index):
        before that statement), following the dataflow into each block."""
        if not self.supported or not positions:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit = 1 << local
        for number, index in positions:
            state = entry.get(number)
            if state is None or number not in self.blocks:
                return False
            for text, _, _ in self.blocks[number].statements[:index]:
                state = self._transfer(text, state)
            if not state & bit:
                return False
        return True

    def _sure(self, number, index, line, file, facts):
        """True if the IR shows code of statement ``index`` (a terminator) of block
        ``number`` on ``line``, at a column the statement can have: its own call (a
        call's, a drop's glue), not inlined, or its conditional branch (an assert's, a
        switch's). Codegen emits the MIR blocks merged into an IR block in order, each
        call terminator one call there, so the calls are told apart by order, where
        their number matches (an inlined call leaves none of its own)."""
        block = self.blocks[number]
        text, spanned, spanned_line = block.statements[index]
        if index != len(block.statements) - 1:
            return False
        if spanned == file:
            span = _span(block, index)
            if spanned_line != line or span is None or span[1] != line:
                return False
            low, high = facts.to_bytes(span[0]), facts.to_bytes(span[2])
        elif facts.known:
            low = high = None
        else:
            return False

        def columns(found):
            # Its own (within its span), or a macro's on the line it is collapsed onto.
            return any(c in facts.macros or low is not None and low <= c <= high for c in found)

        chain = self._ir_chain(number, facts.function)
        if chain is None:
            return False
        label, members = chain
        calls, branches = facts.function.events.get(label, ((), ()))
        if _call(block, index) or _DROP.match(text):
            if "intrinsics::" in text or columns(facts.inlined):
                return False
            callers = [
                member
                for member in members
                if _call(self.blocks[member], len(self.blocks[member].statements) - 1)
                or _DROP.match(self.blocks[member].terminator)
            ]
            if len(callers) != len(calls):
                return False
            found = calls[callers.index(number)]
        elif _BRANCHES.match(text):
            if members[-1] != number or len(branches) != 1:
                return False
            found = branches[0]
        else:
            return False
        return found[0] == line and columns({found[1]})

    def _ir_chain(self, number, function):
        """(label, [MIR blocks in it, in order]) of the IR block holding ``number``."""
        predecessors, labeled = self.predecessors(), function.labeled()
        head = number
        for _ in range(4096):
            if head in labeled:
                break
            froms = predecessors.get(head, set())
            if len(froms) != 1:
                return None
            (head,) = froms
        else:
            return None
        members = [head]
        while (target := _merge_target(self, function, members[-1], predecessors)) is not None:
            if target in members or len(members) > 4096:
                return None
            members.append(target)
        if number not in members:
            return None
        return ("start" if head == 0 else f"bb{head}"), members

    def _sure_end(self, number, lexical, line, file, facts):
        block = self.blocks.get(number)
        if block is None or not block.statements:
            return False
        index = len(block.statements) - 1
        return self.lexical(_scope(block, index)) == lexical and self._sure(
            number, index, line, file, facts
        )

    def _could_be(self, number, index, line, file, facts):
        """True if the stop's column can be that of statement ``index``'s code."""
        block = self.blocks[number]
        text, spanned, spanned_line = block.statements[index]
        column = facts.column
        if column in facts.macros:
            return True
        if spanned != file:
            # Another file's code is collapsed onto a macro's path on the line.
            return not facts.known
        span = _span(block, index)
        if span is None:
            return True
        start = facts.to_bytes(span[0]) if spanned_line == line else 1
        if _call(block, index):
            return start <= column and (span[1] > line or column <= facts.to_bytes(span[2]))
        return spanned_line == line and column == start

    def used_ahead(self, local, start):
        """True if ``local`` is used on some path from ``start`` ((block, index): before
        that statement) before it is assigned again or its storage ends. NLL keeps every
        borrow it holds live there: what it borrows has not been moved, dropped or
        written since it was made."""
        pending, seen = [start], set()
        while pending:
            number, index = pending.pop()
            if (number, index) in seen or number not in self.blocks:
                continue
            seen.add((number, index))
            block = self.blocks[number]
            ended = False
            for text, _, _ in block.statements[index:]:
                event = _event(text, local)
                if event == "use":
                    return True
                if event == "end":
                    ended = True
                    break
            if not ended:
                # Through a suspension too: what it borrows stays borrowed while it is.
                resumed = (self.resumes[number],) if number in self.resumes else ()
                pending.extend((successor, 0) for successor in (*block.successors, *resumed))
        return False

    def named_locals(self):
        """The locals the program's own bindings name (`debug x => _3`)."""
        return frozenset(
            binding[0]
            for bindings in self.bindings.values()
            for binding in bindings
            if binding[0] is not None
        )

    def copy_undoes(self, copy, root, start, projection=(), sinks=(frozenset(), frozenset())):
        """True if, on some path from ``start`` ((block, index): before that statement),
        the program uses ``copy`` (or a pointer to it) and then assigns ``root`` whole or
        ends it without reading what a change made (#192): it goes on with the value
        ``copy`` took from ``root``, so a change made at ``start`` is never seen (`x = match
        x { n => … n }` puts the old value back; `let y = x;` … `x = y + 1;`; a `move`
        closure called before `x` goes out of scope unread). Where the program reads
        ``root`` first, it sees the change there, whatever its copies hold (`let a = s.x;`
        keeps the old value, as the program's own). ``projection`` is the part changed
        (``_root_access``): only a read that surely covers part of it counts, `copy`,
        `move`, or std's printing or formatting of a borrow of it (``_format_reads``, at
        that call); a borrow given to anything else may never be read through, so the
        path goes on (#192 review: `ignore(&x)`, `println!("{}", x.a)` for a change to
        `x.b`, `ignore(format_args!("{}", x))`). A write
        covering the whole part counts as an assignment, any other as nothing. ``sinks``:
        ``_format_reads``'s (trusted files, the crate's own function paths)."""
        copies, formats = self._pointers_to(copy) | {copy}, self._format_reads(*sinks)
        # (block, index, whether the copy was used on the way here).
        pending, seen = [(start[0], start[1], False)], set()
        while pending:
            number, index, used = pending.pop()
            if (number, index, used) in seen or number not in self.blocks:
                continue
            seen.add((number, index, used))
            block = self.blocks[number]
            stopped = False
            for text, _, _ in block.statements[index:]:
                used = used or any(_event(text, each) == "use" for each in copies)
                access = _root_access(text, root, projection, formats)
                if access == "read":
                    stopped = True
                    break
                if access == "end":
                    if used:
                        return True
                    stopped = True
                    break
                if _event(text, copy) == "end" and not used:
                    stopped = True
                    break
            if not stopped:
                resumed = (self.resumes[number],) if number in self.resumes else ()
                pending.extend((successor, 0, used) for successor in (*block.successors, *resumed))
        return False

    def _format_reads(self, trusted=frozenset(), crate=frozenset()):
        """{a call that formats what it is given (``_FORMAT_SINK``, from std's macros'
        own file): the shared borrows (`&(_1.1: i64)`) whose primitive it formats}, each
        read at that call (#192 review 3: a borrow given to `Argument::new_display` is
        read only if the `fmt::Arguments` made from it is formatted; `ignore(format_args!
        (…))` and a dropped `format_args!` never are). From the call back through what
        made its `fmt::Arguments`: `Arguments::new` (`_5 = Arguments::<'_>::new::<10, 2>(
        move _14, move _16)`), the array of `Argument`s (`_16 = &(*_17)`, `_17 = &_9`,
        `_9 = [move _10, move _12]`), one made from a pointer to a primitive (`_10 =
        Argument::<'_>::new_display::<i64>(move _11)`, not a type parameter named like
        one), and that pointer (`_11 = &(*_104)`, `_104 = copy (_6.0: &i64)`, `_6 =
        (move _7, move _8)`, `_7 = &_1`): only through locals assigned once. Empty where
        the body's type parameters are not known. Only a call whose span is in one of the
        ``trusted`` files (``OwnedLocals.std_macros``: std's own, exactly as the
        toolchain names them; #192 review 4: a crate's `src/library/std/src/macros.rs`
        is not), to a function none of the ``crate``'s own is named as (`format`)."""
        key = (self.generics, trusted, crate)
        if self._format_cache is None or self._format_cache[0] != key:
            values, sinks = {}, []
            for block in self.blocks.values():
                for text, file, _ in block.statements:
                    text = _STRING.sub('""', text)
                    if whole := _WHOLE_ASSIGNMENT.match(text):
                        values.setdefault(int(whole[1]), []).append(whole[2].split(" -> [")[0])
                    sink = _FORMAT_SINK.match(text)
                    if sink and file in trusted and sink[1] not in crate:
                        sinks.append((text, int(sink[2])))
            found = {}
            for text, arguments in sinks if self.generics is not None else ():
                borrows, pending, seen = set(), [(arguments, "arguments", None)], set()
                while pending:
                    item = pending.pop()
                    local, kind, member = item
                    if item in seen or len(values.get(local, ())) != 1:
                        continue
                    seen.add(item)
                    value = values[local][0]
                    operands = [int(n) for n in re.findall(r"(?:move|copy) _([0-9]+)\b", value)]
                    if member is not None:
                        # The tuple of the arguments' pointers: its ``member``th operand.
                        whole = re.fullmatch(r"\((.*?),?\)", value)
                        parts = whole[1].split(", ") if whole else []
                        part = (
                            re.fullmatch(r"(?:move|copy) _([0-9]+)", parts[member])
                            if (member < len(parts))
                            else None
                        )
                        if part:
                            pending.append((int(part[1]), "pointer", None))
                    elif kind == "arguments":
                        if argument := _FORMAT_ARGUMENT.fullmatch(value):
                            if (
                                _FORMATTED_PRIMITIVE.fullmatch(argument[1])
                                and argument[1] not in self.generics
                            ):
                                pending.append((int(argument[2]), "pointer", None))
                        elif _FORMAT_ARGUMENTS.match(value) or re.fullmatch(
                            r"\[(?:(?:move|copy) _[0-9]+(?:, )?)*\]|&\(\*_[0-9]+\)|&_[0-9]+",
                            value,
                        ):
                            refs = re.findall(r"(?<![\w])_([0-9]+)\b", value)
                            pending.extend(
                                (n, "arguments", None) for n in {*operands, *map(int, refs)}
                            )
                    elif step := re.fullmatch(r"&\(\*_([0-9]+)\)|(?:move |copy )?_([0-9]+)", value):
                        pending.append((int(step[1] or step[2]), "pointer", None))
                    elif step := re.fullmatch(
                        r"(?:move|copy) \(_([0-9]+)\.([0-9]+): [^()]*\)", value
                    ):
                        pending.append((int(step[1]), "tuple", int(step[2])))
                    elif value.startswith("&") and not _NOT_SHARED.match(value):
                        borrows.add(value)
                if borrows:
                    found[text] = frozenset(borrows)
            self._format_cache = (key, found)
        return self._format_cache[1]

    def _pointers_to(self, local):
        """The locals any borrow of ``local`` (or of a part, or of such a pointer's
        target) is assigned to anywhere in the body (`_6 = &_2`, `_4 = &mut (*_6)`), and
        what is made from one by a cast, a copy or a call, of a type that may hold a
        pointer (#311: `p` of `let p = slot.as_mut_ptr();`, through which `*p = x;` stored
        a copy in `slot`; review: the `Option<&mut u32>` of `v.last_mut()` and `r` of its
        `unwrap()`, and an iterator `v.iter_mut()`)."""
        if self._pointer_cache is None:
            self._pointer_cache = {}
        if local in self._pointer_cache:
            return self._pointer_cache[local]
        found, changed = set(), True
        while changed:
            changed = False
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    whole = _WHOLE_ASSIGNMENT.match(_STRING.sub('""', text))
                    if not whole or int(whole[1]) in found:
                        continue
                    mentioned = {int(n) for n in _MENTIONED.findall(whole[2])}
                    if whole[2].startswith("&"):
                        made = local in mentioned or mentioned & found
                    else:
                        kind = self.types.get(int(whole[1]))
                        made = mentioned & found and not _pointer_free(kind, self.generics)
                    if made:
                        found.add(int(whole[1]))
                        changed = True
        self._pointer_cache[local] = frozenset(found)
        return self._pointer_cache[local]

    def live_ahead(self, start, drops=False):
        """A bitset of the locals ``used_ahead`` finds used on some path from ``start``,
        all at once (#105 judges every copy of a local at every place of a line): each
        statement is read once, and which locals each block's successors use is a
        backward dataflow over the body; a place replays its block from there. With
        ``drops``, dropping a local uses it too (#164: a `Drop` may read a borrow)."""
        number, index = start
        if number not in self.blocks:
            return 0
        out, effects = self._liveness(drops)
        state = out[number]
        for uses, ends in reversed(effects[number][index:]):
            state = uses | state & ~ends
        return state

    def _liveness(self, drops=False):
        """({block: the locals some path from its end uses ahead}, {block: [(the locals
        each statement uses, those it ends)]}) for ``live_ahead``, per ``_event``;
        with ``drops``, a `drop` of a local is a use of it."""
        if self._live is None:
            self._live = {}
        if drops not in self._live:
            effects = {}
            for number, block in self.blocks.items():
                found = []
                for text, _, _ in block.statements:
                    uses = ends = 0
                    plain = _STRING.sub('""', text)
                    dropped = _DROP.match(plain) if drops else None
                    for local in {int(n) for n in _ANY_LOCAL.findall(plain)}:
                        event = "use" if dropped else _event(text, local)
                        if event == "use":
                            uses |= 1 << local
                        elif event == "end":
                            ends |= 1 << local
                    found.append((uses, ends))
                effects[number] = found

            def through(number, state):
                for uses, ends in reversed(effects[number]):
                    state = uses | state & ~ends
                return state

            successors, predecessors = {}, {}
            for number, block in self.blocks.items():
                # Through a suspension too, as ``used_ahead`` goes.
                resumed = (self.resumes[number],) if number in self.resumes else ()
                successors[number] = [n for n in (*block.successors, *resumed) if n in self.blocks]
                for target in successors[number]:
                    predecessors.setdefault(target, set()).add(number)
            out = dict.fromkeys(self.blocks, 0)
            entry = {number: through(number, 0) for number in self.blocks}
            work = list(self.blocks)
            queued = set(work)
            while work:
                number = work.pop()
                queued.discard(number)
                state = 0
                for target in successors[number]:
                    state |= entry[target]
                out[number] = state
                found = through(number, state)
                if found != entry[number]:
                    entry[number] = found
                    for other in predecessors.get(number, ()):
                        if other not in queued:
                            queued.add(other)
                            work.append(other)
            self._live[drops] = (out, effects)
        return self._live[drops]

    def copies_of(self, local, formatting=True, compares=False, reads="any"):
        """The temporaries (locals no variable names, other than the return place) that
        may hold a value read from ``local``'s storage, which a later change to it does
        not reach (#105): one assigned from a read of it (`_4 = copy (_1.0: i32)`, not a
        borrow of it), from a read through any pointer (which may point into it), from a
        call given a pointer that may (`get(&s)`), or from such a temporary. A pointer
        into it is not a copy (reads through it see the change), but makes what a call
        returns one, except `format_args!`'s constructors where ``formatting`` (the crate
        names nothing `core` or `std` that could pass for them). The program's own
        variables are not counted here: ``copies_at`` follows its direct copies (#192).
        ``compares`` and ``reads`` as for ``_copy_verdict``."""
        if self._copies is None:
            self._copies = {}
        key = (local, formatting, compares, reads)
        if key in self._copies:
            return self._copies[key]
        parts, mentions, seeds, _, named, _ = self._copy_parts()
        # From what holds or points with no local given, then along what mentions each
        # temporary found: the least fixpoint, as passes over every assignment reach.
        holds, points = set(), set()
        work = [*seeds, *mentions.get(local, ())]
        queued = set(work)
        while work:
            index = work.pop()
            queued.discard(index)
            target = parts[index][0]
            if target == local or target in named:
                continue
            hold, point = _copy_verdict(
                parts[index], local, holds, points, formatting, compares, reads
            )
            for found, into in ((hold, holds), (point, points)):
                if found and target not in into:
                    into.add(target)
                    for other in mentions.get(target, ()):
                        if other not in queued:
                            queued.add(other)
                            work.append(other)
        self._copies[key] = frozenset(holds)
        return self._copies[key]

    def lent_from(self, local, generics=None, free_types=frozenset()):
        """The locals that may hold a pointer into ``local``'s storage or into what it
        owns (#164: a String's buffer): one assigned a borrow of it or of a place of it
        (`&_1`, `&mut (_1.0: T)`, `&raw const _1`), or a value that mentions such a local
        (what a call returns from one, `deref`, `index`, `as_str`, `chars`; a copy, a
        struct, a closure holding one); and what a call given one may store it into: what
        another pointer it is given points at (`v.push(&s)` stores it in `v`), through a
        `&mut` or raw pointer, or a shared one to what may hide a `Cell` (#164). Only
        locals whose type can hold a pointer count (a reference, a raw pointer, a
        lifetime, a closure or coroutine, a type parameter of ``generics``, or a type not
        known); a `usize` from `s.len()` holds none."""
        if self._lent_from is None:
            self._lent_from = {}
        key = (local, generics, free_types)
        if key in self._lent_from:
            return self._lent_from[key]
        assignments, borrows, sources = [], {}, {}
        for block in self.blocks.values():
            for text, _, _ in block.statements:
                text = _STRING.sub('""', text)
                whole = _WHOLE_ASSIGNMENT.match(text)
                found = whole or _PART_ASSIGNMENT.match(text)
                if not found:
                    continue
                target, value = int(found[1]), found[2]
                lent = {int(n) for n in _DIRECT_BORROW.findall(value)}
                mentioned = {int(n) for n in _MENTIONED.findall(value)}
                borrows.setdefault(target, set()).update(lent)
                sources.setdefault(target, set()).update(mentioned - lent)
                call = "->" in value and not _NOT_A_CALL.match(value)
                # `(*_9).0 = …` stores into what `_9` points at.
                through = whole is None and text.lstrip("(").startswith("*")
                assignments.append((target, mentioned, local in lent, call, through))

        def kind(number):
            return self.types.get(number)

        def pointing(number):
            return not _pointer_free(kind(number), generics, free_types)

        def hidden_cell(number):
            # What a shared reference may still change: a `Cell`, a lock, a type of the
            # crate's own (it may hold one), a type parameter, a type not known.
            found = kind(number)
            return (
                found is None
                or generics is None
                or mentions(found, generics)
                or bool(_INTERIOR.search(found))
                or any(
                    not (word in _PRIMITIVE_WORDS or word.startswith(_STD_PATHS))
                    for word in _TYPE_WORDS.findall(_LIFETIME.sub("", found))
                )
            )

        def referents(number, shared, seen):
            """What the pointer ``number`` may point at, and could have a pointer stored
            in: through a `&mut` or raw pointer anything; through shared ones only what
            ``hidden_cell`` allows."""
            found = set()
            if number in seen:
                return found
            seen.add(number)
            mutable = re.match(r"&mut |\*mut ", kind(number) or "&mut ")
            shared = shared and not mutable
            for target in borrows.get(number, ()):
                if target != local and (not shared or hidden_cell(target)):
                    found.add(target)
                found |= referents(target, shared, seen)
            for source in sources.get(number, ()):
                # A pointer copied out of a local (`_40 = copy (_6.0: Unique<…>) as *const
                # …`, as `vec![…]` fills its Box) points into what that local owns.
                if pointing(source):
                    if source != local and (not shared or hidden_cell(source)):
                        found.add(source)
                    found |= referents(source, shared, seen)
            return found

        held, changed = set(), True
        while changed:
            changed = False
            for target, mentioned, direct, call, through in assignments:
                if not (direct or mentioned & held):
                    continue
                into = {target} if target != local else set()
                if through:
                    into |= referents(target, False, set())
                for other in mentioned if call else ():
                    if other != local and other not in held:
                        into |= referents(other, True, set())
                for number in into:
                    if number != local and number not in held and pointing(number):
                        held.add(number)
                        changed = True
        self._lent_from[key] = frozenset(held)
        return self._lent_from[key]

    def _copy_parts(self):
        """``copies_of``'s reading of each assignment to a local (a temporary, or one of
        the program's own bindings, for ``copies_at``'s direct copies, #192: `n` of `x =
        match x { n => … }`), once per body: ([(its target, the locals its value
        mentions, whether it reads through a pointer, is a borrow, is a call, is
        `format_args!`'s constructor, the reference variables it mentions, whether it is
        a call the dump names as the standard library's address comparison)], {local: the
        indexes of those mentioning it}, the indexes of those that may hold or point
        whatever local is judged, {(block, index): its part}, the named locals, {(block,
        index): the writes through a pointer there, each (its parts, the locals it may
        write into, the pointer bindings among them, the locals the pointers were made
        from, the pointer bindings that stand for what they point at, the locals a refusal
        may name it as a line of)}).

        A write through a pointer has a part for each local it may write into
        (``_stored_into``), which it leaves holding what it held too (#311): a store
        (`(*_35) = [move _15, const 1_u32]`, as `vec![x, 1]` fills its Box), a
        `copy_nonoverlapping`, and a call given a pointer it may write through
        (``_may_write_through``), of what else the call is given (#311 review: `Vec::push(move _6,
        move _7)` with `_6 = &mut v`, `ptr::write(p, x)`, `mem::replace`, `mem::swap`); not
        of that pointer itself (`Box::new` given a closure holding `&s` copies no `s`). Where no local the
        pointers were made from stands for what they point at (an argument, the Box
        `Box::into_raw` was given), each pointer binding among them does
        (``_pointees``)."""
        if self._copy_reads is None:
            named = {
                binding[0]
                for bindings in self.bindings.values()
                for binding in bindings
                if binding[0] is not None
            }
            parts, mentions, seeds, at, stores = [], {}, [], {}, {}

            def add(value, targets, text, mentioned=None):
                if mentioned is None:
                    mentioned = frozenset(int(n) for n in _MENTIONED.findall(value))
                pointers = frozenset(
                    n for n in mentioned if n in named and _BORROWING.search(self.types.get(n, "&"))
                )
                through = bool(re.search(r"\(\*[(_]", value))
                made = []
                for target in targets:
                    parts.append(
                        (
                            target,
                            mentioned,
                            through,
                            bool(re.match(r"&(?:mut |raw const |raw mut |fake \w+ )?", value)),
                            "->" in value and not _NOT_A_CALL.match(value),
                            bool(
                                _FORMAT_CALL.match(value)
                                and _FORMAT_TYPE.fullmatch(self.types.get(target, ""))
                            ),
                            pointers,
                            bool(_ADDRESS_COMPARISON.match(text)),
                        )
                    )
                    made.append(len(parts) - 1)
                    for n in mentioned:
                        mentions.setdefault(n, []).append(len(parts) - 1)
                    if through or pointers:
                        seeds.append(len(parts) - 1)
                return tuple(made)

            def through(bases, value, text, carried=True, mentioned=None):
                targets = frozenset().union(*(self._stored_into(n) for n in bases)) - {0}
                owners = frozenset().union(*(self._made_from(n) for n in bases))
                aiming = frozenset(
                    n for n in targets if n in named and _POINTER_TYPE.match(self.types.get(n, ""))
                )
                made = add(value, sorted(targets), text, mentioned)
                pointees = self._pointees(owners, aiming)
                # A call given only the pointers it may write through brings no new value
                # (`slot.as_mut_ptr()`): never named where a copy was made.
                return made, targets, aiming, owners, pointees, owners if carried else frozenset()

            for number, block in self.blocks.items():
                for index, (text, _, _) in enumerate(block.statements):
                    text = _STRING.sub('""', text)
                    stored = _STORE_BASE.match(text)
                    found = _WHOLE_ASSIGNMENT.match(text) or (
                        None if re.match(r"\(+\*_", text) else _PART_ASSIGNMENT.match(text)
                    )
                    copied = _COPY_NONOVERLAPPING.match(text)
                    if stored and " = " in text:
                        value = text.split(" = ", 1)[1].removesuffix(";")
                        stores[number, index] = (through({int(stored[1])}, value, text),)
                    elif copied:
                        value = f"copy (*_{copied[2]})"
                        stores[number, index] = (through({int(copied[1])}, value, text),)
                    elif found:
                        value = found[2]
                        if int(found[1]) != 0:
                            at[number, index] = add(value, [int(found[1])], text)[0]
                        arguments = (
                            _CALL_ARGUMENTS.match(text)
                            if "->" in value and not _NOT_A_CALL.match(value)
                            else None
                        )
                        given = (
                            {int(n) for n in _MENTIONED.findall(arguments[1])} if arguments else ()
                        )
                        bases = {n for n in given if self._may_write_through(n)}
                        # Through each, what else it is given; `()` carries nothing.
                        writes = tuple(
                            through({base}, value, text, True, frozenset(others))
                            for base in sorted(bases)
                            if (others := {n for n in given - {base} if self.types.get(n) != "()"})
                        )
                        if writes:
                            stores[number, index] = writes
            self._copy_reads = (parts, mentions, seeds, at, frozenset(named), stores)
        return self._copy_reads

    def _pointees(self, owners, aiming):
        """The pointer bindings of ``aiming`` that stand for what a write through them
        reaches (#311 review): all of them, where no local of ``owners`` (what the pointers
        were made from) does, one not itself a pointer and never moved out of (`slot` of
        `p = slot.as_mut_ptr()`; not the Box `Box::into_raw` was given, nor anything for an
        argument `p`)."""
        moved = self._moved()
        standing = any(
            not _POINTER_TYPE.match(self.types.get(n, "")) and n not in moved for n in owners
        )
        return frozenset() if standing else aiming

    def _copy_places(self):
        """[((block, index), the index of a part ``_copy_parts`` reads there)], of an
        assignment, and of a write through a pointer for each local the pointer was made
        from (#311: the Box `vec!` fills, `slot` of `*p = x;`, `v` of `v.push(x)`). Not for
        its other aliases, so `team[pick].shifts += 1;` is never where `p` of `same(p, q)`
        was made."""
        parts, _, _, at, _, stores = self._copy_parts()
        return [
            *at.items(),
            *(
                (place, part)
                for place, writes in stores.items()
                for made, _, _, _, _, reported in writes
                for part in made
                if parts[part][0] in reported
            ),
        ]

    def _may_write_through(self, local):
        """Whether a callee given ``local`` may write through a pointer it holds (#311
        review round 3): its type shows one (``_may_write``), or it may hold a pointer and
        something it was made from shows one (``_made_from``: the `&mut v` an `Entry` or a
        closure was made from). Positive evidence only: `a.chunks_exact(1)`, made from a
        shared `&[u32]`, writes nothing. What it reaches only through a shared `&` counts
        only for a cell or a raw pointer (round 4, ``_reached``): `&h` of `h = Hold { p:
        &mut v }` writes nothing, `h` itself and `&mut h` may."""
        kind = self.types.get(local)
        if _may_write(kind, self.generics):
            return True
        if _pointer_free(kind, self.generics):
            return False
        return any(
            _may_write(self.types.get(made), self.generics, shared)
            for made, shared in self._reached(local)
            if made != local
        )

    def _reached(self, local):
        """What ``local`` may have been made from (``_made_from``), each with whether it
        reaches it only through a shared `&` (#311 review round 4): it does past a shared
        borrow (`_12 = &_7;`, `_11 = &(*_12);`), and past a value of a shared reference
        type (`&Hold<'_>`, a call's result too); not past a `&mut`, a raw borrow, or a
        value moved or copied (`Hold { p: move _8 }`)."""
        found, pending = {}, [(local, _shared_reference(self.types.get(local)))]
        assigned = self._assignments()
        while pending:
            local, shared = pending.pop()
            # Seen already, as reachable at least as freely.
            if local in found and (shared or not found[local]):
                continue
            found[local] = shared
            for value in assigned.get(local, ()):
                value = _STRING.sub('""', value)
                borrow = _REFERENCE.match(value)
                past = shared or bool(borrow and not borrow[1] and not value.startswith("&raw "))
                for n in map(int, _DIRECT_BORROW.findall(value)):
                    pending.append((n, past or _shared_reference(self.types.get(n))))
                for n in map(int, _MENTIONED.findall(value)):
                    kind = self.types.get(n)
                    if kind is None or not _POINTERLESS.fullmatch(kind):
                        pending.append((n, past or _shared_reference(kind)))
        return found.items()

    def _made_from(self, pointer):
        """The locals ``pointer`` may have been made from, back through every assignment
        to it (#311): itself, each local a value assigned to it borrows (`&mut _4`), and
        each one it mentions that may hold a pointer, and so on (`_10 = copy ((_6.0:
        Unique<…>).0: NonNull<…>) as *const …` is made from the Box `_6`)."""
        found, pending, assigned = set(), [pointer], self._assignments()
        while pending:
            local = pending.pop()
            if local in found:
                continue
            found.add(local)
            for value in assigned.get(local, ()):
                value = _STRING.sub('""', value)
                # What it borrows, and what that was made from in turn (`&mut _10` of an
                # iterator `_10 = iter_mut(…)` made from `&mut v`).
                pending.extend(int(n) for n in _DIRECT_BORROW.findall(value))
                for n in map(int, _MENTIONED.findall(value)):
                    kind = self.types.get(n)
                    if kind is None or not _POINTERLESS.fullmatch(kind):
                        pending.append(n)
        return frozenset(found)

    def _stored_into(self, pointer):
        """The locals a store through ``pointer`` (the local a written place starts from:
        `_35` of `((*_35).1: T) = …`, `_6` of `(*((*_6).0: &mut u32)) = …`) may write into,
        or into what they own, flow-insensitively (#311): ``pointer`` and its aliases
        (``_aliases``: what it was made from and what was made from it, as the Box `vec!`
        fills and the Vec made from that), and every local one of them borrows itself
        (`&mut _4`, a `u32`'s too, which ``_aliases`` links to no pointer)."""
        if self._stored_cache is None or self._stored_cache[0] != self.generics:
            self._stored_cache = (self.generics, {})
        found = self._stored_cache[1]
        if pointer not in found:
            into, assigned = set(self._aliases(pointer)), self._assignments()
            for alias in list(into):
                for value in assigned.get(alias, ()):
                    into.update(int(n) for n in _DIRECT_BORROW.findall(_STRING.sub('""', value)))
            found[pointer] = frozenset(into)
        return found[pointer]

    def copies_at(
        self, local, position, formatting=True, compares=False, reads="any", projection=()
    ):
        """The temporaries that may hold a value read from ``local``'s current value at
        ``position`` ((block, index): before that statement), on some path from bb0
        (#183): ``copies_of``'s reading of each assignment, taken in the order the code
        runs, where an assignment to ``local`` itself, or the start or end of its
        storage, leaves none. A temporary set before ``local`` took its current value
        holds an earlier one, which a change to it does not touch either (`for entry in
        args`: the iterator's `next` into a temporary, then `entry` moved out of it).
        ``copies_of`` where the flow is past its budget; ``compares`` and ``reads`` as for
        ``_copy_verdict``, and with "value", only reads of what may overlap the part
        ``projection``."""
        state = self._copy_state(local, position, formatting, projection, compares, reads)
        return self.copies_of(local, formatting, compares, reads) if state is None else state[0]

    def direct_copies_at(
        self, local, position, formatting=True, projection=(), compares=False, reads="any"
    ):
        """The locals, the program's own bindings among them, that hold a value taken
        from ``local``'s current value at ``position`` by value (#192): a copy or move of
        it or of a part (`n` of `x = match x { n => … }`, `let y = x;`), a value computed
        from one (`v` of `while let Some(v) = (x > 0).then_some(x)`), a closure or
        aggregate holding one (`move || x`), or a read through a pointer to it (`*r` of
        `let r = &x;`); not what a call given a pointer to it returns (`x.len()`), which
        ``copies_at`` judges for temporaries only. With ``projection`` (``_root_access``),
        only what may have been taken from that part counts (#192 review: `seen = r.end`
        holds none of `o.marks`). None where the flow is past its budget. ``compares`` and
        ``reads`` (``_copy_verdict``) change none of these, only the flow's key."""
        state = self._copy_state(local, position, formatting, projection, compares, reads)
        return None if state is None else state[2]

    def _copy_state(self, local, position, formatting, projection=(), compares=False, reads="any"):
        """(holds, points, direct, own, kept) at ``position``, or None past the budget."""
        if self._copy_flows is None:
            self._copy_flows = {}
        key = (local, formatting, projection, compares, reads)
        if key not in self._copy_flows:
            self._copy_flows[key] = self._copy_flow(local, formatting, projection, compares, reads)
        entry = self._copy_flows[key]
        number, index = position
        if entry is None or number not in entry:
            return None
        state = entry[number]
        for at in range(min(index, len(self.blocks[number].statements))):
            state = self._copy_step(
                local, formatting, number, at, state, projection, compares, reads
            )
        return state

    def _copy_step(
        self, local, formatting, number, index, state, projection=(), compares=False, reads="any"
    ):
        """``copies_at``'s transfer over one statement: (holds, points, direct, own, kept)
        after it: ``copies_of``'s holds and points, for temporaries; and the locals,
        named or not, holding a direct copy (``direct_copies_at``), the pointers made from
        a borrow of ``local`` (`&x`, `&mut (*_6)` of such), whose reads are direct, and
        the locals that hold a direct copy or point at one (#311 review: `&a` of `let a =
        [x];`, casts and aggregates of it, and a pointer a call returns from one, as
        `a.iter()`). Then what the statement writes through a
        pointer (``_copy_store``), judged by what held a copy before it: a call's
        arguments are read before it returns."""
        block = self.blocks[number]
        text = _STRING.sub('""', block.statements[index][0])
        whole = _WHOLE_ASSIGNMENT.match(text)
        if re.match(rf"Storage(?:Live|Dead)\(_{local}\);$", text) or (
            whole and int(whole[1]) == local
        ):
            # Its value starts again: what was copied before holds an earlier one.
            return (frozenset(),) * 5
        parts, _, _, at, named, stores = self._copy_parts()
        found = at.get((number, index))
        after = state
        if found is not None:
            after = self._copy_assign(
                local,
                formatting,
                text,
                whole,
                parts[found],
                named,
                state,
                projection,
                compares,
                reads,
            )
        for stored in stores.get((number, index), ()):
            if stored[0]:
                after = self._copy_store(
                    local, text, stored, state, after, formatting, projection, compares, reads
                )
        return after

    def _copy_assign(
        self, local, formatting, text, whole, part, named, state, projection, compares, reads
    ):
        """``_copy_step`` over an assignment's own target, ``part``."""
        holds, points, direct, own, kept = state
        target, mentioned, through, borrow, called, _, _, _ = part
        if target == local:
            return state
        # A place of it that may hold part of ``projection`` (#192 review).
        touched = local in mentioned and _touches(text, local, projection)
        if borrow:
            mine = touched or bool(mentioned & own)
            taken = False
            # A pointer to what may hold a copy (`&a` of `let a = [x];`) points at one.
            keeps = bool(mentioned & kept)
        else:
            # By value: a copy, a move, what is computed from one (a call given one too),
            # an aggregate or closure holding one; or a read through its own pointer.
            mine = not called and bool(mentioned & own) and not through
            taken = touched or bool(mentioned & direct) or (through and bool(mentioned & own))
            # And a cast, copy or aggregate of a pointer to one, or a pointer a call given
            # one returns (`a.iter()`); not a value a call computes, as #192 has it for
            # `x.len()` (`x.to_string()`, `w.push(v)`), nor a pointer into the local itself
            # (`&s[2..]` holds no copy of `s`).
            aims = not called or not _pointer_free(self.types.get(target), self.generics)
            keeps = taken or (aims and bool(mentioned & kept))
        if whole:
            # Reassigned: what it held before is gone (a call's result: on its return
            # edge, which is the only one ``_copy_flow`` gives this state).
            holds, points = holds - {target}, points - {target}
            direct, own, kept = direct - {target}, own - {target}, kept - {target}
        direct = direct | {target} if taken else direct
        own = own | {target} if mine else own
        kept = kept | {target} if keeps else kept
        if target in named:
            return holds, points, direct, own, kept
        hold, point = _copy_verdict(
            part, local, holds, points, formatting, compares, reads, own, touched
        )
        return (
            holds | {target} if hold else holds,
            points | {target} if point else points,
            direct,
            own,
            kept,
        )

    def _copy_store(
        self, local, text, stored, state, after, formatting, projection, compares, reads
    ):
        """``_copy_step`` over a write through a pointer (#311: `((*_35).1: …) = [move
        _15, const 1_u32]`, as `vec![x, 1]` fills its Box; a call given `&mut v` and `x`):
        each local it may write into (``stored``: its parts, those locals, the pointer
        bindings among them, what they were made from, the pointer bindings standing for
        what they point at) may hold what the value holds, judged by ``state`` (before
        it), or point where it points, besides what it holds in ``after``. A temporary
        among them holds a copy the statement uses where it is used again (the Box, the Vec
        made from it, the pointer); a binding of the program's own holds a direct copy, as
        `slot` of `*p = x;` and `v` of `v.push(x)` do. A pointer binding (`p`) holds none,
        but what is read through it does, as through a pointer made from a borrow; where
        nothing else stands for what it points at, it does (an argument `p`). A source that
        holds or points at a copy counts (``kept``, #311 review: `&a` given to
        `v.extend_from_slice`), as does a pointer into the local itself."""
        holds, points, direct, own, kept = state
        parts, _, _, _, named, _ = self._copy_parts()
        made, targets, pointers, _, pointees, _ = stored
        first = parts[made[0]]
        _, mentioned, through, borrow, called, _, _, _ = first
        touched = local in mentioned and _touches(text, local, projection)
        # A call given a pointer into the local may copy what it points at.
        taken = not borrow and (
            touched
            or bool(mentioned & (direct | kept))
            or ((through or called) and bool(mentioned & own))
        )
        hold, point = _copy_verdict(
            first, local, holds, points, formatting, compares, reads, own, touched
        )
        holds, points, direct, own, kept = after
        targets = targets - {local}
        if taken:
            direct = direct | (targets - pointers) | (pointees - {local})
            own = own | (pointers - pointees - {local})
            kept = kept | targets
        temporaries = targets - named
        return (
            holds | temporaries if hold else holds,
            points | temporaries if point else points,
            direct,
            own,
            kept,
        )

    def read_copies_at(self, local, position, projection=(), transfers=False):
        """What surely holds a value read from ``local``'s current value (or from its part
        ``projection``) at ``position``, on every path from bb0 (#300: what a refusal may
        say the statement has read): (the temporaries that read it, the locals, the
        program's own bindings among them, that took it by value, and (each such local
        taken through a binding's copy, a binding it was computed from)), or None where
        that is not known (past the budget). A read is a `copy` or `move` of it or of an
        overlapping place (``_reads_part``), a read through a pointer made from a borrow of
        it (the whole local only), or a value computed from what holds one; a temporary
        computed from a binding's copy took it, but the statement did not read it (#192).
        A pointer a call returns or an aggregate holds, a local a writable borrow is ever
        taken of, and a coroutine's body are not followed. Where a writable borrow of a
        place that may overlap the part is taken anywhere in the body (``_exposed``), a
        call or drop, or a write through a pointer not surely elsewhere, may change it: what
        was read before may hold an earlier value, so it is known no more (#300). A store
        through a pointer surely into a local, or into what it owns (`vec![x, 1]` fills its
        Box so), of what holds a read makes that local hold it; one, or a call given a
        pointer, that may write into what holds a read leaves it holding it no more (#311:
        ``_read_store``, ``_read_clobber``). A call's result holds no sure copy, and what
        it is given holds none after it (#311 review: `discard(vec![x, 1])` may return
        anything), but for the standard library's Box-to-Vec conversion `vec!` makes, where
        ``transfers`` (``OwnedLocals._std_transfers``) proves it is."""
        if self.coroutine is not None:
            return None
        if self._read_flows is None:
            self._read_flows = {}
        key = (local, projection, transfers)
        if key not in self._read_flows:
            self._read_flows[key] = self._read_flow(local, projection, transfers)
        entry = self._read_flows[key]
        number, index = position
        if entry is None:
            return None
        if number not in entry:
            return frozenset(), frozenset(), frozenset()
        state = entry[number]
        for at in range(min(index, len(self.blocks[number].statements))):
            state = self._read_step(local, projection, number, at, state, transfers)
        return state[0], state[1], state[3]

    def _read_step(self, local, projection, number, index, state, transfers=False):
        """``read_copies_at``'s transfer over one statement: (read, taken, own, origins,
        apart, into) after it: its two sets, the pointers surely made from a borrow of the
        local (``projection`` () only), (a local taken through a binding's copy, that
        binding) for every binding it was surely computed from, which stays true after the
        binding is assigned again, the pointers surely to a place apart from the part (into
        another local, a part of it apart from ``projection``, or the caller's), and (a
        pointer, a local it surely points into or into what that owns) for each pointer
        made from a borrow of a local or a Box's own pointer, which a store through it
        fills (#311: ``_read_store``). ``transfers`` as for ``read_copies_at``."""
        empty = (frozenset(),) * 6
        text = _STRING.sub('""', self.blocks[number].statements[index][0])
        whole = _WHOLE_ASSIGNMENT.match(text)
        if re.match(rf"Storage(?:Live|Dead)\(_{local}\);$", text) or (
            whole and int(whole[1]) == local
        ):
            return empty
        if self._read_ends(local, projection, text):
            return empty
        before = state
        read, taken, own, origins, apart, into = self._read_clobber(text, state)
        if ended := _STORAGE.match(text) or _DROP.match(text):
            gone = int(ended[1])
            if through := _DROP_THROUGH.match(text):
                # What it drops through a pointer (`drop((*_35))`) may be what holds a read.
                lost = (read | taken) & self._stored_into(int(through[1]))
                lost = frozenset(each for each in lost if self._addressable(each))
                read, taken = read - lost, taken - lost
                origins = frozenset(pair for pair in origins if pair[0] not in lost)
            kept = frozenset(pair for pair in origins if pair[0] != gone)
            into = frozenset(pair for pair in into if gone not in pair)
            return read - {gone}, taken - {gone}, own - {gone}, kept, apart - {gone}, into
        # A write to a part of it that may overlap the part a change makes, or through a
        # pointer that may point there: what was read before holds an earlier value (#300).
        steps = _written_place(text, local)
        if steps is not None and not _apart(steps, projection):
            return empty
        # Through a pointer loaded from memory (`(*((*_6).0: &mut u32))`) as well: only one
        # surely apart, written through directly, keeps what is known.
        written = text.split(" = ", 1)[0] if " = " in text else ""
        if "(*" in written and self._exposed(local, projection):
            through = _WRITE_THROUGH.match(text)
            if not (through and written.count("(*") == 1 and int(through[1]) in apart):
                return empty
        state = read, taken, own, origins, apart, into
        if (stored := _STORE_BASE.match(text)) and written:
            return self._read_store(local, projection, text, int(stored[1]), state)
        parts, _, _, at, named, _ = self._copy_parts()
        found = at.get((number, index))
        if found is None:
            return state
        target, mentioned, _, borrow, _, _, _, _ = parts[found]
        if target == local:
            return state
        read, taken, own, apart = (each - {target} for each in (read, taken, own, apart))
        into = frozenset(pair for pair in into if target not in pair)
        given = {binding for made, binding in origins if made in mentioned}
        given |= mentioned & taken & named
        origins = frozenset(pair for pair in origins if pair[0] != target)
        if not whole or target in self._lent_writably():
            return read, taken, own, origins, apart, into
        # A pointer whose own storage is lent may be retargeted through the loan (`*q = r;`
        # for `q = &mut p`), or through a `Cell` it is in: never known to point apart.
        aimed = target not in self._borrowed()
        value, whole_local = whole[2], projection == ()
        if "->" in value and not _NOT_A_CALL.match(value):
            # A call's result holds no sure copy: the callee may return anything (#311
            # review: `discard(vec![x, 1])` returns `vec![7, 1]`). Only the standard
            # library's Box-to-Vec conversion `vec!` makes gives the Vec what its Box held.
            transfer = _STD_TRANSFER.match(value) if transfers else None
            if transfer:
                box, (was_read, was_taken, _, was_origins, _, _) = int(transfer[1]), before
                if box in was_taken:
                    gave = {binding for made, binding in was_origins if made == box}
                    gave |= {box} & named
                    taken = taken | {target}
                    origins = origins | {(target, binding) for binding in gave}
                if box in was_read and target not in named:
                    read = read | {target}
            return read, taken, own, origins, apart, into
        plain = [int(n) for n in re.findall(r"\(\*_([0-9]+)\)", value)]
        only = bool(plain) and len(plain) == value.count("(*")
        through = only and set(plain) <= own
        if borrow:
            rooted = _DIRECT_BORROW.match(value)
            if whole_local and ((rooted and int(rooted[1]) == local) or through):
                own = own | {target}
            if rooted and int(rooted[1]) != local:
                elsewhere = True
            elif rooted:
                place = _place_steps(value, rooted.start(1) - 1)
                elsewhere = place is not None and _apart(place[0], projection)
            else:
                elsewhere = only and set(plain) <= apart
            if aimed and elsewhere:
                apart = apart | {target}
            # Into what it borrows (`&mut _4`), or where the pointer it reborrows points.
            if aimed and rooted:
                into = into | {(target, int(rooted[1]))}
            elif aimed and only and len(set(plain)) == 1:
                into = into | {(target, made) for pointer, made in into if pointer == plain[0]}
            return read, taken, own, origins, apart, into
        reads = _reads_part(value, local, projection) or (whole_local and through)
        if reads or mentioned & taken:
            taken = taken | {target}
            origins = origins | {(target, binding) for binding in given}
        if target not in named and (reads or mentioned & read):
            read = read | {target}
        copied = re.fullmatch(r"(?:copy|move) _([0-9]+)", value)
        if whole_local and copied and int(copied[1]) in own:
            own = own | {target}
        pointer = re.fullmatch(r"(?:copy|move) _([0-9]+)(?: as .*)?", value)
        if aimed and pointer and int(pointer[1]) in apart:
            apart = apart | {target}
        if aimed and pointer:
            into = into | {(target, made) for each, made in into if each == int(pointer[1])}
        # A Box's own pointer (`_35 = copy ((_14.0: Unique<…>).0: NonNull<…>) as *const …
        # (Transmute)`) points into what the Box owns.
        box = _BOX_POINTER.match(text)
        if aimed and box and re.fullmatch(r"_[0-9]+", box[2]):
            into = into | {(target, int(box[2][1:]))}
        return read, taken, own, origins, apart, into

    def _lent_writably(self):
        """The locals a writable borrow (`&mut`, `&raw`) is taken of anywhere in the body."""
        if self._writable_borrows is None:
            self._writable_borrows = frozenset(
                int(n)
                for block in self.blocks.values()
                for statement, _, _ in block.statements
                for n in _WRITABLE_BORROW.findall(_STRING.sub('""', statement))
            )
        return self._writable_borrows

    def _read_store(self, local, projection, text, pointer, state):
        """``_read_step`` over a store through a pointer (#311). What it may write into
        (``_stored_into``; anything, through a pointer loaded from memory) no longer surely
        holds what it held. Where the pointer surely points into a local, or into what that
        owns (``into``; a Box written through itself, as `ShallowInitBox`'s is), which no
        writable borrow is taken of, and the value surely holds a read, that local holds it
        too: `vec![x, 1]` fills its Box by `((*_35).1: …) = [move _15, const 1_u32]`,
        where `_15` holds `x`'s copy, then gives the Box to the Vec it makes. The value
        is read before the store writes (`[move _9, move _11]` into the Box `_9` is linked
        to)."""
        read, taken, own, origins, apart, into = state
        written, value = text.split(" = ", 1)
        value = value.removesuffix(";")
        simple = bool(_WRITE_THROUGH.match(text)) and written.count("(*") == 1
        owners = set()
        if simple:
            owners = {made for each, made in into if each == pointer}
            if _BOX_TYPE.match(self.types.get(pointer, "")) and pointer not in self._borrowed():
                owners.add(pointer)
        owners = frozenset(owners) - {local} - self._lent_writably()
        mentioned = {int(n) for n in _MENTIONED.findall(value)}
        plain = [int(n) for n in re.findall(r"\(\*_([0-9]+)\)", value)]
        only = bool(plain) and len(plain) == value.count("(*")
        reads = _reads_part(value, local, projection) or (
            projection == () and only and set(plain) <= own
        )
        named = self._copy_parts()[4]
        given = {binding for made, binding in origins if made in mentioned}
        given |= mentioned & taken & named
        holds = bool(owners) and bool(reads or mentioned & taken)
        sure = holds and bool(reads or mentioned & read)
        held = frozenset(each for each in read | taken if self._addressable(each))
        lost = held & self._stored_into(pointer) if simple else held
        read, taken = read - lost, taken - lost
        origins = frozenset(pair for pair in origins if pair[0] not in lost)
        if holds:
            taken = taken | owners
            origins = origins | {(owner, binding) for owner in owners for binding in given}
        if sure:
            read = read | (owners - named)
        return read, taken, own, origins, apart, into

    def _addressable(self, local):
        """Whether a pointer may point into ``local``'s storage, or into what it owns
        (#311): its storage is borrowed somewhere, or its type may hold a pointer (a
        Box's, a Vec's, an array of them)."""
        kind = self.types.get(local)
        return local in self._borrowed() or (kind is not None and not _POINTERLESS.fullmatch(kind))

    def _read_clobber(self, text, state):
        """``state`` (``_read_step``'s) less what a call ``text``, or a
        `copy_nonoverlapping`, may change, where something holds a read (#311): what it is
        given, moved or passed (#311 review: what a callee does with it is not known), and
        a local it is given a pointer that may point into, as `fill(p)` may overwrite the
        Box `p` was made from: an alias of it a callee may write through (``_may_write_through``: a
        `&mut` or raw pointer, a mutable view, or a value made from one)."""
        read, taken, own, origins, apart, into = state
        if not (read or taken):
            return state
        if copied := _COPY_NONOVERLAPPING.match(text):
            given, writable = set(), {int(copied[1])}
        elif "->" not in text or text.startswith("goto") or _NOT_A_CALL.match(text):
            return state
        else:
            arguments = _CALL_ARGUMENTS.match(text)
            given = {int(n) for n in _MENTIONED.findall(arguments[1] if arguments else text)}
            writable = {n for n in given if self._may_write_through(n)}
        held = read | taken
        lost = held & given
        lost |= {
            each
            for each in held
            if self._addressable(each) and (self._aliases(each) - {each}) & writable
        }
        if not lost:
            return state
        origins = frozenset(pair for pair in origins if pair[0] not in lost)
        return read - lost, taken - lost, own, origins, apart, into

    def _read_ends(self, local, projection, text):
        """Whether the statement ``text`` is a call, a drop or a `copy_nonoverlapping` that
        may change the part ``projection`` of ``local``, where a writable borrow of a place
        overlapping it is taken anywhere in the body (``_exposed``): `keep(&mut x)` (#300)."""
        if not re.match(r"(?:drop|copy_nonoverlapping)\(", text) and (
            "->" not in text or text.startswith("goto") or _NOT_A_CALL.match(text)
        ):
            return False
        return self._exposed(local, projection)

    def _exposed(self, local, projection):
        """Whether the body takes a writable borrow (`&mut`, `&raw`) of a place of
        ``local`` anywhere that may overlap its part ``projection``: then a call, a drop,
        or a write through a pointer not surely elsewhere may change that part (#300)."""
        if self._exposed_cache is None:
            self._exposed_cache = {}
        key = (local, projection)
        if key not in self._exposed_cache:
            found = False
            for block in self.blocks.values():
                for statement, _, _ in block.statements:
                    text = _STRING.sub('""', statement)
                    for borrowed in _WRITABLE_BORROW.finditer(text):
                        if int(borrowed[1]) != local:
                            continue
                        place = _place_steps(text, borrowed.start(1) - 1)
                        if place is None or not _apart(place[0], projection):
                            found = True
            self._exposed_cache[key] = found
        return self._exposed_cache[key]

    def _read_flow(self, local, projection, transfers=False):
        """{block: (read, taken, own, origins, apart, into) on entry} for
        ``read_copies_at``: its transfer, met by intersection over every way in (the
        greatest fixpoint from bb0); None past the budget. A call sets its result on its
        return edge only; one that may change the part (``_read_ends``) ends what is known
        on every edge, and what it may write into (``_read_clobber``) too. The caller's
        pointers, the arguments, point elsewhere than any local of this frame, while no
        borrow of them can retarget them (``_borrowed``)."""
        empty = frozenset()
        given = frozenset(self.arguments - self._borrowed()) - {local}
        entry = {0: (empty, empty, empty, empty, given, empty)}
        work, queued, steps = [0], {0}, 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(self.blocks))
        while work:
            steps += 1
            if steps > limit:
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks.get(number)
            if block is None:
                return None
            state = entry[number]
            last = len(block.statements) - 1
            for index in range(last):
                state = self._read_step(local, projection, number, index, state, transfers)
            after = (
                self._read_step(local, projection, number, last, state, transfers)
                if last >= 0
                else state
            )
            text = _STRING.sub('""', block.statements[last][0]) if last >= 0 else ""
            ends = last >= 0 and self._read_ends(local, projection, text)
            for target in self._flows(number):
                if target not in self.blocks:
                    continue
                if ends or block.returns is None or target == block.returns:
                    out = after
                else:
                    out = self._read_clobber(text, state)
                old = entry.get(target)
                met = out if old is None else tuple(a & b for a, b in zip(old, out))
                if met != old:
                    entry[target] = met
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        return entry

    def _copy_flow(self, local, formatting, projection=(), compares=False, reads="any"):
        """{block: (holds, points, direct, own, kept) on entry} for ``copies_at``: its
        transfer, merged by union up from nothing (the least fixpoint); None past the
        budget."""
        empty = (frozenset(),) * 5
        entry = dict.fromkeys(self.blocks, empty)
        # Every block the code can reach once, whatever flows into it: a copy made in a
        # block whose entry holds none must still flow on (#192 review: a `while let`'s
        # `then_some(x)`); one it cannot reach adds nothing to a join (#192 review 2).
        work = sorted(self._reachable(), reverse=True)
        queued, steps = set(work), 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(self.blocks))
        while work:
            steps += 1
            if steps > limit:
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks.get(number)
            if block is None:
                return None
            state = entry[number]
            last = len(block.statements) - 1
            for index in range(last):
                state = self._copy_step(
                    local, formatting, number, index, state, projection, compares, reads
                )
            # A call sets its result on its return edge only; the others leave all as was.
            after = (
                self._copy_step(local, formatting, number, last, state, projection, compares, reads)
                if last >= 0
                else state
            )
            for target in self._flows(number):
                if target not in entry:
                    continue
                out = after if block.returns is None or target == block.returns else state
                merged = tuple(old | new for old, new in zip(entry[target], out))
                if merged != entry[target]:
                    entry[target] = merged
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        return entry

    def used_before(self, local, start, compares=False):
        """True if every path into ``start`` passes a use of ``local`` (or its assignment)
        with nothing since that can move, drop, free or write what it points into: no
        call, drop, write to a place's part or a borrowed local, nor end of a borrowed
        local's storage. At a use the borrow checker has kept its target; only branches,
        assertions and whole assignments to locals nothing borrows run between, and with
        ``compares``, the standard library's address comparisons of two raw pointers
        (``_harmless``)."""
        return not self.gaps_before(local, start, compares, every=False)

    def gaps_before(self, local, start, compares=False, every=True, limit=16):
        """Why ``used_before`` is False at ``start``: {(block, index)} of each statement
        on a path back from it to a use of ``local``, that can move, drop, free or write
        what it points into, or ends its storage (where the path stops), with (None,
        None) for a path that reaches the function's start or a block not known first.
        With ``every`` False, only the first found; else up to ``limit`` of them (a long
        function's walk back to an early use stops there)."""
        predecessors, borrowed = self.predecessors(), self._borrowed()
        pending, seen, found = [start], set(), set()
        while pending and len(found) < limit:
            number, index = pending.pop()
            if (number, index) in seen:
                continue
            seen.add((number, index))
            block = self.blocks.get(number)
            if block is None:
                found.add((None, None))
                if not every:
                    return found
                continue
            stopped = False
            for back in range(min(index, len(block.statements)) - 1, -1, -1):
                text = block.statements[back][0]
                event = _event(text, local)
                assigned = _WHOLE_ASSIGNMENT.match(text)
                if event == "use" or (assigned and int(assigned[1]) == local):
                    stopped = True
                    break
                if event == "end" or not _harmless(
                    text, borrowed, self.types if compares else None
                ):
                    found.add((number, back))
                    if not every or len(found) >= limit:
                        return found
                    if event == "end":
                        stopped = True
                        break
            if stopped:
                continue
            froms = predecessors.get(number, ())
            if number == 0 or not froms:
                found.add((None, None))
                if not every:
                    return found
                continue
            pending.extend((other, len(self.blocks[other].statements)) for other in froms)
        return found

    def _borrowed(self):
        """Locals whose own storage is borrowed somewhere (`&_3`, `&mut (_3.0: Pt)`,
        `&raw const _3`), not through a pointer they hold (`&(*_3)`)."""
        if self._direct is None:
            self._direct = {
                int(number)
                for block in self.blocks.values()
                for text, _, _ in block.statements
                for number in _DIRECT_BORROW.findall(text)
            }
        return self._direct

    def _moved(self):
        """Locals moved out of somewhere, whole or in part (`move _3`, `move (_3.0: T)`),
        not through a pointer they hold (`move (*_3)`)."""
        if self._moved_cache is None:
            self._moved_cache = {
                int(number)
                for block in self.blocks.values()
                for text, _, _ in block.statements
                for number in re.findall(r"\bmove \(*_([0-9]+)\b", _STRING.sub('""', text))
            }
        return self._moved_cache

    def _mentions(self):
        """{local: [each statement mentioning it]} (strings blanked), indexed once."""
        if self._mention_index is None:
            index = {}
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    if '"' in text:
                        text = _STRING.sub('""', text)
                    for number in set(_MENTIONED.findall(text)):
                        index.setdefault(int(number), []).append(text)
            self._mention_index = index
        return self._mention_index

    def _written_through(self):
        """Locals borrowed `&mut` or raw somewhere (`&mut _3`, `&raw const _3`): what
        they hold can change by a write no assignment of their own shows."""
        if self._writable is None:
            self._writable = {
                int(number)
                for block in self.blocks.values()
                for text, _, _ in block.statements
                for number in _WRITABLE_BORROW.findall(_STRING.sub('""', text))
            }
        return self._writable

    def visible_scopes(self, line, path):
        """The scopes of the statements on ``line`` of the frame's source ``path``, and
        the scopes enclosing them: where a binding lldb-dap shows there is declared
        (rustc's DWARF lexical blocks are these scopes). None if no statement is on it."""
        shown = str(path or "").replace("\\", "/")
        found = set()
        for (file, number), scopes in self.line_scopes.items():
            if number != line or not (shown == file or shown.endswith("/" + file)):
                continue
            for scope in scopes:
                while scope is not None and scope not in found:
                    found.add(scope)
                    scope = self.scopes.get(scope)
        return found or None

    @staticmethod
    def in_scopes(bindings, visible):
        """The bindings declared in ``visible`` scopes; all of them if none is (or no
        scope is known): never fewer than lldb-dap could be showing."""
        kept = [binding for binding in bindings if visible is None or binding[4] in visible]
        return kept or list(bindings)

    def initialized_throughout(self, local, blocks):
        """True if ``local`` is initialized on entry to each block and none of them
        moves, drops or ends it."""
        if not self.supported or not blocks:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit = 1 << local
        for number in blocks:
            state = entry.get(number)
            if state is None or not state & bit:
                return False
            block = self.blocks[number]
            if any(local in self._kills(text) for text, _, _ in block.statements):
                return False
        return True

    def initialized_at(
        self, local, blocks, line, file, declared=None, shared=None, *, foreign=True
    ):
        """True if ``local`` is initialized wherever code of ``line`` in ``file`` can be
        in these blocks: a statement spanning that line, or spanning another file or
        none (external macro code takes the line that invoked it), and bb0's start when
        the line is the function's own (``declared``: its prologue). Moves, drops and
        assignments are followed statement by statement in between.

        Statements that emit no code at opt-level 0 (storage markers) are never
        positions. A call's result is stored at its target's start, at the call's line;
        then the call's own block has code of that line too and is checked before the
        call, which is stricter: built MIR passes locals to calls only through temps.

        ``foreign=False`` says the line cannot invoke a macro, so no code from another
        file is on it.
        ``shared`` maps a block to every block codegen emitted into the same IR block
        (``_merged``): the IR cannot say which of them a line's code came from, so a
        block without code of the line is passed over when another of them has some.
        """
        if not self.supported or not blocks:
            return False
        entry = self._dataflow()
        if entry is None:
            return False
        bit, predecessors = 1 << local, self.predecessors()

        def explained(number):
            """(prologue, statement positions, result store): this block's code of line."""
            block = self.blocks[number]
            positions = [
                index
                for index, (text, spanned, spanned_line) in enumerate(block.statements)
                if (spanned_line == line if spanned == file else spanned is None or foreign)
                and not _NO_CODE.match(text)
            ]
            stored = any(
                caller in blocks
                and self.blocks[caller].returns == number
                and (
                    self.blocks[caller].statements[-1][2] == line
                    if self.blocks[caller].statements[-1][1] == file
                    else self.blocks[caller].statements[-1][1] is None or foreign
                )
                for caller in predecessors.get(number, ())
            )
            return number == 0 and line == declared, positions, stored

        for number in blocks:
            state = entry.get(number)
            if state is None:
                return False
            block = self.blocks[number]
            starts_here, positions, stored = explained(number)
            if not starts_here and not positions:
                if not stored and any(
                    any(explained(other)) for other in (shared or {}).get(number, set()) - {number}
                ):
                    # The line's code in this IR block is another merged block's, and that
                    # block is checked itself.
                    continue
                if stored:
                    # Only a checked caller's result store, at this block's start.
                    if not state & bit:
                        return False
                    continue
                # The IR has code of this line here, yet no statement can have made it:
                # not understood, so the whole block must keep the local.
                if not state & bit or any(
                    local in self._kills(text) for text, _, _ in block.statements
                ):
                    return False
                continue
            if starts_here and not state & bit:
                return False
            for index, (text, _, _) in enumerate(block.statements):
                if index in positions and not state & bit:
                    return False
                state = self._transfer(text, state)
            # A stop at a call's or drop's return address (`step --out`) is still on
            # this line, after the terminator's moves and drops.
            last = len(block.statements) - 1
            if last in positions and (block.returns is not None or _DROP.match(block.terminator)):
                if not state & bit:
                    return False
        return True

    def _boxes(self):
        """{temp: Box local}: the raw pointers built from a Box's own pointer
        (`_23 = copy ((_1.0: Unique<T>).0: NonNull<T>) as *const T (Transmute)`), which
        the built MIR derefs in place of the Box. A Box inside one (`bx.inner.data`) is
        reached through such a pointer (`((*_23).1: Box<Inner>)`): its own pointers map
        to the outermost local too, as do pointers made from a copy of such a Box
        (`_17 = copy ((*_37).1: Box<Inner>)`). ``_box_paths`` names each pointer's Box
        by a place from that local."""
        if self._box_temps is None:
            made = []
            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    if found := _BOX_POINTER.match(text):
                        made.append((False, int(found[1]), found[2]))
                    elif found := _BOX_COPY.match(text):
                        made.append((True, int(found[1]), found[2]))
            # A pointer made from a copy waits for the copy to be told apart; a copy
            # whose Box never is (copied out of a reference) is a Box of its own.
            waiting = {temp for copy, temp, _ in made if copy}
            boxes, paths, copies, changed = {}, {}, {}, True
            while changed or waiting - copies.keys():
                if not changed:
                    waiting = set(copies)
                changed = False
                for copy, temp, place in made:
                    if temp in (copies if copy else boxes):
                        continue
                    root = re.match(r"\(*(\*?)_([0-9]+)\b", place)
                    if root is None:
                        continue
                    inner = int(root[2])
                    if root[1]:
                        if inner not in boxes:
                            continue
                        box = boxes[inner]
                        path = place.replace(f"(*_{inner})", f"(*{paths[inner]})", 1)
                    elif inner in copies:
                        box, held = copies[inner]
                        path = re.sub(rf"\b_{inner}\b", lambda _, held=held: held, place, count=1)
                    elif inner in waiting or (copy and place == f"_{inner}"):
                        continue
                    else:
                        box, path = inner, f"B{place}"
                    if copy:
                        copies[temp] = (box, path)
                    else:
                        boxes[temp], paths[temp] = box, path
                    changed = True
            self._box_temps, self._box_paths = boxes, paths
        return self._box_temps

    def _kills(self, text):
        """Locals ``text`` moves out of, drops or ends; moving or dropping through a
        Box's pointer (`move ((*_23).1: Box<Pt>)`) leaves the Box partly moved."""
        killed = _kills(text)
        if boxes := self._boxes():
            killed |= {boxes[int(n)] for n in _THROUGH.findall(text) if int(n) in boxes}
        return killed

    def _transfer(self, text, state):
        state = self._terminate(text, state)
        if assigned := _ASSIGN.match(text):
            state |= 1 << int(assigned[1])
        elif (local := self._replacements()[1].get(text)) is not None:
            # The store of `bx.data = new` (or `h.b = new`) makes the Box (the local)
            # whole again only if it was whole before the drop (no field moved out
            # earlier, nor since).
            pending = 1 << self._pending(local)
            if state & pending:
                state |= 1 << local
            state &= ~pending
        return state

    def _terminate(self, text, state):
        """``text``'s kills; the drop of a drop-and-replace remembers (in the local's
        pending bit) whether the local was whole. Any other move or drop of the local
        forgets it: the store then leaves it partly moved."""
        killed = self._kills(text)
        box = self._replacements()[0].get(text)
        whole = box is not None and state & (1 << box)
        state &= ~_mask(killed) & ~_mask(self._pending(local) for local in killed)
        if whole:
            state |= 1 << self._pending(box)
        return state

    def _pending(self, box):
        if self._offset is None:
            self._offset = 1 + max(map(_max_local, self.blocks.values()), default=0)
        return self._offset + box

    def _replacements(self):
        """({drop: local}, {store: local}) for a drop-and-replace: `bx.data = new` or
        `*bx = new` through a Box (`drop(((*_13).1: T))`, then the store to the same
        place through another pointer of the same Box), and `h.b = new` on a local's
        own field or element (`drop((_1.1: Box<Pt>))`, then `(_1.1: Box<Pt>) = move
        _4;` with nothing but storage markers and drop flags between, #108). The store
        fills what the drop emptied."""
        if self._replaced is None:
            boxes, drops, found = self._boxes(), {}, {}
            for block in self.blocks.values():
                dropped = _DROP_PLACE.match(block.terminator)
                target = _target(block)
                if not dropped or target is None or target not in self.blocks:
                    continue
                place = _through_box(dropped[1], boxes, self._box_paths)
                direct = _DIRECT_PLACE.match(dropped[1])
                if place is None and direct is None:
                    continue
                text, passed = self._next_code(target)
                if place is not None:
                    store = _STORE.match(text or "")
                    if store and _through_box(store[1], boxes, self._box_paths) == place:
                        found[text] = place[0]
                        drops[block.terminator] = place[0]
                    continue
                named = {int(n) for n in _MENTIONED.findall(_STRING.sub('""', dropped[1]))}
                if (text or "").startswith(dropped[1] + " = ") and all(
                    (flag := _CONSTANT_STORE.match(statement)) and int(flag[1]) not in named
                    for statement in passed
                ):
                    # The very place, with nothing between that could change what it
                    # names (an index local) or move out of the local: at most drop
                    # flags set (`arr[i] = new`).
                    found[text] = int(direct[1])
                    drops[block.terminator] = int(direct[1])
            self._replaced = (drops, found)
        return self._replaced

    def _next_code(self, number):
        """(the next statement from block ``number`` that is not passed over, the code
        passed over to reach it): storage markers and `goto`s are passed over, and so
        are other assignments (new pointers to a Box, drop flags) and the debug checks
        on them (`assert(copy _44, "misaligned pointer dereference...")`), which run."""
        seen, text, passed = set(), None, []
        while number in self.blocks and number not in seen and text is None:
            seen.add(number)
            for statement, _, _ in self.blocks[number].statements:
                whole = _WHOLE_ASSIGNMENT.match(statement)
                if _NO_CODE.match(statement):
                    continue
                if whole and "->" not in statement:
                    passed.append(statement)
                    continue
                if goto := re.fullmatch(r"goto -> bb([0-9]+);", statement):
                    number = int(goto[1])
                elif statement.startswith("assert(") and (
                    success := re.search(r"\bsuccess: bb([0-9]+)", statement)
                ):
                    passed.append(statement)
                    number = int(success[1])
                else:
                    text = statement
                break
            else:
                break
        return text, passed

    def _aliases(self, local):
        """``local`` and the locals made from it (`&mut (*_1)`, `&((*_1).0: T)`,
        `copy _1`, `move _1`, and so on): pointers into what it points at."""
        if self._alias_edges is not None and self._alias_generics != self.generics:
            # Built while its type parameters were unknown (or others): build it again.
            self._alias_edges = self._alias_cache = None
        if self._alias_cache is None:
            self._alias_cache = {}
        if local in self._alias_cache:
            return self._alias_cache[local]
        if self._alias_edges is None:
            # {local: the locals linked to it}. A value made from another, whole or in
            # part (a reborrow, a copy, a struct or closure holding it, a call's result),
            # may point where it does, and what is stored through either may land in the
            # other (`*sl.get() = &mut *v`, `Hold { slot: &mut slot }`, a closure setting
            # `slot`), so links go both ways; never through a value that cannot hold a
            # pointer (`v.len()`).
            edges = {}

            def link(a, b):
                if a != b and all(
                    not _POINTERLESS.fullmatch(kind := self.types.get(n, ""))
                    # `i64` in a generic body may be a type parameter holding a pointer.
                    or self.generics is None
                    or mentions(kind, self.generics)
                    for n in (a, b)
                ):
                    edges.setdefault(a, set()).add(b)
                    edges.setdefault(b, set()).add(a)

            for block in self.blocks.values():
                for text, _, _ in block.statements:
                    text = _STRING.sub('""', text)
                    assigned = _WHOLE_ASSIGNMENT.match(text) or _PART_ASSIGNMENT.match(text)
                    if assigned:
                        for used in _MENTIONED.findall(assigned[2]):
                            link(int(used), int(assigned[1]))
                    # A call may store any argument through a mutable one
                    # (`mem::swap(&mut slot, &mut w)`, `Cell::set(&c, &mut *v)`).
                    if " -> " in text and not _NOT_A_CALL.match(text):
                        arguments = _CALL_ARGUMENTS.match(text)
                        used = (
                            {int(n) for n in _MENTIONED.findall(arguments[1])}
                            if arguments
                            else set()
                        )
                        for n in used:
                            for writable in used:
                                if re.search(
                                    r"&mut |\*mut |NonNull<|Cell<", self.types.get(writable, "")
                                ):
                                    link(n, writable)
            self._alias_edges, self._alias_generics = edges, self.generics
        found, pending = {local}, [local]
        while pending:
            for made in self._alias_edges.get(pending.pop(), ()):
                if made not in found:
                    found.add(made)
                    pending.append(made)
        self._alias_cache[local] = found
        return found

    def drops_through(self, local):
        """True if the body drops what ``local`` or an alias of it points at anywhere
        (`drop((*_1))`, `drop(((*_8).1: T))`)."""
        if self._dropped is None:
            self._dropped = {
                int(found[1])
                for block in self.blocks.values()
                if (found := _DROP_THROUGH.match(block.terminator))
            }
        return bool(self._dropped & self._aliases(local))

    def dropped_through(self, local, start):
        """True if some path into ``start`` (block, index) drops what ``local`` points at
        (`drop((*_1))`, the first half of `*v = new`) with no write through it since:
        its target is a dropped value there."""
        # Drops and stores through a reborrow or a copy of it (`let w = &mut *v;`) count.
        names = "|".join(str(alias) for alias in sorted(self._aliases(local)))
        if not self.drops_through(local):
            return False
        dropped = re.compile(rf"^drop\(\(*\*\(*_(?:{names})\b")
        written = re.compile(rf"^\(+\*\(*_(?:{names})\b[^=]*= |^_{local} = ")
        predecessors = self.predecessors()
        pending, seen = [start], set()
        while pending:
            number, index = pending.pop()
            if (number, index) in seen or number not in self.blocks:
                continue
            seen.add((number, index))
            ended = False
            for text, _, _ in reversed(self.blocks[number].statements[:index]):
                if dropped.match(text):
                    return True
                if written.match(text):
                    ended = True
                    break
            if not ended:
                pending.extend(
                    (other, len(self.blocks[other].statements))
                    for other in {*predecessors.get(number, ()), *self._flows_into(number)}
                )
        return False

    def _flows(self, number):
        """The blocks control flows to from ``number``: a suspending block's flows to the
        block resuming it, which the dispatch at bb0 jumps to only on a later poll."""
        if number in self.resumes:
            return (self.resumes[number],)
        successors = self.blocks[number].successors
        if number == 0 and self.resumes:
            resumed = set(self.resumes.values())
            return tuple(target for target in successors if target not in resumed)
        return successors

    def _reachable(self):
        """The blocks control can reach (``_flows``) from bb0 and from where a coroutine
        resumes."""
        found, pending = set(), [0, *self.resumes.values()]
        while pending:
            number = pending.pop()
            if number in found or number not in self.blocks:
                continue
            found.add(number)
            pending.extend(self._flows(number))
        return found

    def _flows_into(self, number):
        """The blocks control flows into ``number`` from (``_flows``, reversed)."""
        if self._into is None:
            found = {}
            for other in self.blocks:
                for target in self._flows(other):
                    found.setdefault(target, set()).add(other)
            self._into = found
        return self._into.get(number, ())

    def _lent(self):
        """A coroutine's locals that only ever hold what its caller lent it: an `async
        fn`'s parameters, which its state keeps from the call (`_3 = move ((*_34).0:
        &mut Vec<Pt>)`), and the poll's `&mut Context`."""
        if self._lent_cache is None:
            found = set()
            if self.coroutine is not None:
                state = self.coroutine
                # A field written in the body (`((*_34).0: &Pt) = &b`, `&mut` to it) may
                # hold what the body made, not what the caller lent.
                assigned = re.compile(rf"^\(*\(\(\*_{state}\)\.([0-9]+): .* = ")
                borrowed = re.compile(
                    rf"&(?:mut |raw const |raw mut )\(*\(\(\*_{state}\)\.([0-9]+): "
                )
                written = set()
                for block in self.blocks.values():
                    for text, _, _ in block.statements:
                        text = _STRING.sub('""', text)
                        written.update(int(number) for number in borrowed.findall(text))
                        if field := assigned.match(text):
                            written.add(int(field[1]))
                kept = re.compile(rf"(?:move|copy) \(\(\*_{state}\)\.([0-9]+): .+\)")
                found = {
                    local
                    for local, values in self._assignments().items()
                    if local != 2
                    and all(
                        (held := kept.fullmatch(value)) and int(held[1]) not in written
                        for value in values
                    )
                }
                # The poll's context: on resuming, rustc moves it out and back
                # (`_32 = move _2; _2 = move _32;`), which leaves the same pointer.
                assigned = self._assignments()
                if re.fullmatch(
                    r"&mut (?:(?:std|core)::task::)?Context<'_>", self.types.get(2, "")
                ) and all(
                    (back := re.fullmatch(r"move _([0-9]+)", value))
                    and assigned.get(int(back[1])) == ["move _2"]
                    for value in assigned.get(2, [])
                ):
                    found.add(2)
                self._written_fields = frozenset(written)
            self._lent_cache = frozenset(found)
        return self._lent_cache

    def _dataflow(self):
        """{block: initialized-locals bitset on entry} for blocks reachable from bb0, by
        a must analysis from "everything" down to its greatest fixpoint."""
        if self._entry is not None:
            return self._entry or None
        reachable, pending = set(), [0]
        while pending:
            number = pending.pop()
            if number in reachable:
                continue
            if number not in self.blocks:
                self._entry = {}
                return None
            reachable.add(number)
            pending.extend(self._flows(number))
        # Each local's bit, and a pending bit per Box local above them (`_pending`).
        top = (1 << (2 * self._pending(0))) - 1
        entry = {number: top for number in reachable}
        entry[0] = _mask(self.arguments)
        # Across a suspension, only what the state saves (and the next poll's arguments).
        kept = _mask(self.saved | self.arguments)
        work = sorted(reachable, reverse=True)  # every block is processed at least once
        queued, steps = set(work), 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(reachable))
        while work:
            steps += 1
            if steps > limit:
                self._entry = {}
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks[number]
            state = entry[number]
            for text, _, _ in block.statements[:-1]:
                state = self._transfer(text, state)
            # The terminator moves and drops on every edge; a call sets its result only
            # on its return edge.
            state = self._terminate(block.terminator, state)
            for target in self._flows(number):
                out = state if number not in self.resumes else state & kept
                if target == block.returns:
                    out |= 1 << int(_ASSIGN.match(block.terminator)[1])
                merged = entry[target] & out
                if merged != entry[target]:
                    entry[target] = merged
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        self._entry = entry
        return entry

    def maybe_initialized(self, position):
        """A bitset of the locals that may hold a value at ``position`` ((block, index):
        before that statement), on some path from bb0: assigned and not moved, dropped
        or ended since (#164: a `drop` of a local moved out on every path drops
        nothing). Every local where the analysis gives up."""
        if self._maybe is None:
            self._maybe = self._maybe_flow() or {}
        number, index = position
        state = self._maybe.get(number)
        if state is None or number not in self.blocks:
            return -1
        for text, _, _ in self.blocks[number].statements[:index]:
            state = self._transfer(text, state)
        return state

    def _maybe_flow(self):
        """{block: maybe-initialized bitset on entry}: ``_dataflow``'s transfer, merged
        by union up from nothing (its least fixpoint); None past the budget."""
        entry = {number: 0 for number in self.blocks}
        entry[0] = _mask(self.arguments)
        work, queued, steps = [0], {0}, 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(self.blocks))
        while work:
            steps += 1
            if steps > limit:
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks.get(number)
            if block is None:
                return None
            state = entry[number]
            for text, _, _ in block.statements[:-1]:
                state = self._transfer(text, state)
            state = self._terminate(block.terminator, state)
            for target in self._flows(number):
                out = state
                if target == block.returns:
                    out |= 1 << int(_ASSIGN.match(block.terminator)[1])
                merged = entry.get(target, 0) | out
                if merged != entry.get(target, 0):
                    entry[target] = merged
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        return entry

    def _assigned_flow(self, resumed=True):
        """{block: bitset of the locals assigned on entry, on every path from bb0} for the
        blocks reachable from bb0 (#132); None past its budget. A local is assigned from
        a whole assignment (a call's result on its return edge) until its storage begins
        again (`StorageLive`: a loop's next pass, a poll resuming the body), not until a
        move or drop: those leave a value's bytes, which #108's views judge, and at
        opt-level 0 a slot keeps what was last stored in it past `StorageDead` (rustc
        emits no lifetime markers, so no other local shares it). Across a suspension,
        only what the coroutine's state saves is kept. ``resumed=False``: the first
        poll's paths only, none through a suspension."""
        if self._assigned_flows is None:
            self._assigned_flows = {}
        if resumed in self._assigned_flows:
            return self._assigned_flows[resumed]

        def flows(number):
            return () if not resumed and number in self.resumes else self._flows(number)

        self._assigned_flows[resumed] = None
        reachable, pending = set(), [0]
        while pending:
            number = pending.pop()
            if number in reachable:
                continue
            if number not in self.blocks:
                return None
            reachable.add(number)
            pending.extend(flows(number))
        top = (1 << self._pending(0)) - 1
        entry = {number: top for number in reachable}
        entry[0] = _mask(self.arguments)
        kept = _mask(self.saved | self.arguments)
        events = self._assign_events()
        work = sorted(reachable, reverse=True)
        queued, steps = set(work), 0
        limit = max(MAX_ITERATIONS, STEPS_PER_BLOCK * len(reachable))
        while work:
            steps += 1
            if steps > limit:
                return None
            number = work.pop()
            queued.discard(number)
            block = self.blocks[number]
            _, killed, assigned = events[number]
            state = entry[number] & ~killed | assigned
            for target in flows(number):
                out = state & kept if number in self.resumes else state
                if target == block.returns:
                    out |= 1 << int(_ASSIGN.match(block.terminator)[1])
                merged = entry[target] & out
                if merged != entry[target]:
                    entry[target] = merged
                    if target not in queued:
                        queued.add(target)
                        work.append(target)
        self._assigned_flows[resumed] = entry
        return entry

    def _assign_events(self):
        """{block: ({local: [(index, assigned)]}, the locals its statements leave
        unassigned, those they leave assigned)}: what each statement before the
        terminator does to a local for ``_assigned_flow`` (a whole assignment assigns
        it; `StorageLive` or `Deinit` begins its storage anew), read once. The dataflow
        then takes a block at a time, and a place's state is its block's entry with
        each local's last event before it."""
        if self._assign_cache is None:
            found = {}
            for number, block in self.blocks.items():
                events, killed, assigned = {}, 0, 0
                for index, (text, _, _) in enumerate(block.statements[:-1]):
                    if begun := _STORAGE_BEGINS.match(text):
                        local, made = int(begun[1]), False
                    elif made := _ASSIGN.match(text):
                        local, made = int(made[1]), True
                    else:
                        continue
                    events.setdefault(local, []).append((index, made))
                    bit = 1 << local
                    if made:
                        assigned |= bit
                    else:
                        killed |= bit
                        assigned &= ~bit
                found[number] = (events, killed, assigned)
            self._assign_cache = found
        return self._assign_cache

    def has_line_code(self, number, line, file, foreign):
        """True if some statement of block ``number`` emits code of ``line``
        (``line_code``)."""
        if self._line_blocks is None:
            self._line_blocks = {}
        key = (number, line, file, foreign)
        if key not in self._line_blocks:
            self._line_blocks[key] = any(
                self.line_code(number, at, line, file, foreign)
                for at in range(len(self.blocks[number].statements))
            )
        return self._line_blocks[key]

    def line_code(self, number, index, line, file, foreign):
        """True if statement ``index`` of block ``number`` emits code of ``line`` of
        ``file``, as ``stop_positions`` counts it (``foreign``: the line may invoke a
        macro, whose statements span another file or none)."""
        block = self.blocks[number]
        text, spanned, spanned_line = block.statements[index]
        if _NO_CODE.match(text):
            return False
        if spanned != file:
            return spanned is None or foreign
        span = _span(block, index)
        return spanned_line == line or (
            _call(block, index) and span is not None and spanned_line < line <= span[1]
        )

    def scopes_at(self, position, line, file, foreign=True, blocks=None):
        """The MIR scopes whose DWARF lexical blocks hold the pc with the stop at
        ``position`` ((block, index)) on ``line`` of ``file``: those enclosing the scope
        of the code there (the statement about to run, or a call or drop returning into
        the block). None where it can be any: one of the line's ``blocks`` with no
        statement of the line (code none explains) can stop anywhere. lldb-dap lists a
        variable only where the pc is in its scope's block."""
        number, index = position
        block = self.blocks.get(number)
        if block is None:
            return None
        if (blocks is None or number in blocks) and not self.has_line_code(
            number, line, file, foreign
        ):
            return None
        owners = [(number, index)] if index < len(block.statements) else []
        if index == 0:
            owners += [
                (other, len(self.blocks[other].statements) - 1)
                for other in self.predecessors().get(number, ())
                if _target(self.blocks[other]) == number
            ]
        found = set()
        for owner, at in owners:
            scope = _scope(self.blocks[owner], at)
            if scope is None:
                return None
            for _ in range(4096):
                if scope is None or scope in found:
                    break
                found.add(scope)
                scope = self.scopes.get(scope)
        return found

    def could_show(self, position, scope, line, file, foreign=True, blocks=None):
        """False only if lldb-dap cannot show a binding of MIR ``scope`` with the stop at
        ``position`` (see ``scopes_at``)."""
        found = self.scopes_at(position, line, file, foreign, blocks)
        return found is None or scope in found

    def returned_into(self, number, line, file, foreign, blocks):
        """The blocks whose call or drop of ``line`` returns into block ``number``, where
        the stop can be at its start only after one of them (``stop_positions``' place
        after a call): the block's first statement is not code of the line, and the
        block is not one of the line's ``blocks`` with none (anywhere in it). None
        otherwise. A join's other edges (a cleanup block entered from an earlier
        unwind) do not reach that place."""
        block = self.blocks.get(number)
        if block is None or not block.statements:
            return None
        if self.line_code(number, 0, line, file, foreign):
            return None
        if number in blocks and not self.has_line_code(number, line, file, foreign):
            return None
        found = [
            other
            for other in self.predecessors().get(number, ())
            if _target(self.blocks[other]) == number
            and (
                _call(self.blocks[other], last := len(self.blocks[other].statements) - 1)
                or _DROP.match(self.blocks[other].terminator)
            )
            and self.line_code(other, last, line, file, foreign)
        ]
        return found or None

    def unassigned_after(self, local, number, callers, resumed=True):
        """``unassigned_at`` at the start of block ``number`` when entered from one of the
        ``callers`` returning into it: True if one leaves ``local`` unassigned (a call's
        return edge assigns its result), None if none is reached."""
        verdicts = []
        for other in callers:
            block = self.blocks[other]
            verdict = self.unassigned_at(local, (other, len(block.statements) - 1), resumed)
            if verdict and block.returns == number:
                verdict = int(_ASSIGN.match(block.terminator)[1]) != local
            verdicts.append(verdict)
        if True in verdicts:
            return True
        return False if False in verdicts else None

    def reassigned_before(self, local, place, positions):
        """True if a write to ``local`` other than its copy of ``place`` (a whole or
        partial assignment, a `&mut` or raw borrow of it) can run before one of
        ``positions``, through a suspension and a later poll too; or if the body writes
        the state's field ``place`` (see _lent)."""
        self._lent()
        if int(re.search(r"\.([0-9]+): ", place)[1]) in self._written_fields:
            return True
        sources = []
        for number, block in self.blocks.items():
            for index, (text, _, _) in enumerate(block.statements):
                text = _STRING.sub('""', text)
                found = _WHOLE_ASSIGNMENT.match(text) or _PART_ASSIGNMENT.match(text)
                if (
                    found
                    and int(found[1]) == local
                    and found[2].partition(" -> ")[0] not in (f"copy {place}", f"move {place}")
                ) or local in {int(n) for n in _WRITABLE_BORROW.findall(text)}:
                    sources.append((number, index))

        def after(number):
            # A suspension flows to its resume, and the next poll starts at bb0, whose
            # dispatch goes on only to a resume (never to the first poll's code).
            if number == 0 and self.resumes:
                return ()
            return (*self._flows(number), *((0,) if number in self.resumes else ()))

        pending, seen = [], set()
        for number, index in sources:
            if any(other == number and index < at for other, at in positions):
                return True
            pending.extend(after(number))
        targets = {number for number, _ in positions}
        while pending:
            number = pending.pop()
            if number in seen:
                continue
            if number in targets:
                return True
            seen.add(number)
            pending.extend(after(number) if number in self.blocks else ())
        return False

    def unassigned_at(self, local, position, resumed=True):
        """True if a path from bb0 reaches ``position`` ((block, index): before that
        statement) with ``local`` not assigned since its storage began (see
        ``_assigned_flow``), False if every path assigns it; None if no path reaches the
        position or the analysis gave up."""
        entry = self._assigned_flow(resumed)
        number, index = position
        if entry is None or number not in entry:
            return None
        events = self._assign_events()[number][0].get(local)
        if events and (last := bisect.bisect_left(events, (index,))):
            return not events[last - 1][1]
        return not entry[number] >> local & 1


_CLOSURE_SEGMENT = re.compile(r"_?\{+closure(?:#[0-9]+)?\}+")
# What rustc's debug info names a coroutine by its kind; its MIR path says `{closure#N}`
# with the same disambiguator (#116).
_COROUTINE_NAME = re.compile(
    r"\{(?:async_fn|async_block|async_closure|coroutine|gen_fn|gen_block|gen_closure"
    r"|async_gen_fn|async_gen_block|async_gen_closure)#([0-9]+)\}"
)


def _mir_name(name):
    """The MIR's name for an IR function named ``name``: `{async_fn#0}` -> `{closure#0}`."""
    found = _COROUTINE_NAME.fullmatch(name)
    return f"{{closure#{found[1]}}}" if found else name


def _shape(segments):
    """(the nearest named function, how many closures deep) of a path's segments."""
    depth = 0
    for segment in reversed(segments):
        if _CLOSURE_SEGMENT.fullmatch(segment):
            depth += 1
            continue
        return segment, depth
    return None, depth


def frame_shape(name):
    """The shape of lldb-dap's frame name (`vb::outer::{{closure}}::{{closure}}`, or
    escaped `_$u7b$$u7b$closure$u7d$$u7d$`, with a legacy hash or not)."""
    text = str(name or "").replace("$u7b$", "{").replace("$u7d$", "}")
    # lldb-dap marks an optimized frame's name ` [opt]` (macOS).
    text = re.sub(r"\s*\[opt\]$", "", text)
    text = re.sub(r"::h[0-9a-f]{16}$", "", text)
    return _shape(_segments(text))


def _segments(path):
    """`<impl at src/a.rs:2:1: 2:11>::push` -> ['<impl at ...>', 'push']."""
    parts, depth, current = [], 0, ""
    for index, char in enumerate(path):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if depth == 0 and path.startswith("::", index):
            parts.append(current)
            current = ""
            continue
        if depth == 0 and current == "" and char == ":" and path[index - 1 : index] == ":":
            continue
        current += char
    parts.append(current)
    return [part for part in parts if part]


def _mask(locals_):
    return sum(1 << local for local in locals_)


def _max_local(block):
    found = [int(n) for text, _, _ in block.statements for n in _ANY_LOCAL.findall(text)]
    return max(found, default=0)


def _event(text, local):
    """What ``text`` does to ``local``: "use" (reads it), "end" (assigns it whole, or
    ends its storage), or None."""
    # `const "_3"` names no local.
    text = _STRING.sub('""', text)
    if text.startswith(f"StorageDead(_{local})"):
        return "end"
    # Drop glue keeps no loan alive (`#[may_dangle]`, a struct without `Drop` holding a
    # reference beside a String); a type whose own `Drop` reads it would be over-gated.
    if _NOT_A_USE.match(text) or _DROP.match(text):
        return None
    if found := _WHOLE_ASSIGNMENT.match(text):
        if local in {int(n) for n in _MENTIONED.findall(found[2])}:
            return "use"
        if int(found[1]) == local:
            return "end"
        return None
    if local in {int(n) for n in _MENTIONED.findall(text)}:
        return "use"
    return None


def _type_end(text, start):
    """Where the type that starts at ``start`` in ``text`` ends: the index of the `)`
    that closes the place around it (`(_1.1: [u32; 3])`), or None."""
    depth, at = 0, start
    while at < len(text):
        if text.startswith("->", at):
            at += 2
            continue
        char = text[at]
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            if depth == 0:
                return at if char == ")" else None
            depth -= 1
        at += 1
    return None


def _place_steps(text, start):
    """The place whose local's `_N` starts at ``start`` in a MIR statement: (its
    projections, as ``("field", n)``, ``("index", n)`` with None for an index not
    constant, ``("other", None)`` for a variant; where its text starts; where it ends),
    or None where it is not read as a place (`Len(_1)`, a dereference)."""
    local = re.match(r"_[0-9]+", text[start:])
    if local is None or (start and text[start - 1] == "*"):
        return None
    begin = start
    while begin and text[begin - 1] == "(":
        begin -= 1
    opens, at, steps = start - begin, start + local.end(), []
    while True:
        if text.startswith("[", at):
            end = text.find("]", at)
            if end < 0:
                return None
            constant = re.fullmatch(r"([0-9]+) of [0-9]+", text[at + 1 : end])
            steps.append(("index", int(constant[1]) if constant else None))
            at = end + 1
            continue
        if not opens:
            return steps, begin, at
        field = re.match(r"\.([0-9]+): ", text[at:])
        if field is None and not text.startswith(" as ", at):
            return None
        end = _type_end(text, at + (field.end() if field else 4))
        if end is None:
            return None
        steps.append(("field", int(field[1])) if field else ("other", None))
        at, opens = end + 1, opens - 1


def _overlaps(steps, projection):
    """Whether the place ``steps`` surely covers part of ``projection`` (one is a prefix
    of the other, each step the same and known); None as a projection: not known, so
    only the whole local does."""
    if projection is None:
        return not steps
    return all(mine == theirs and mine[1] is not None for mine, theirs in zip(steps, projection))


def _pointer_kind(kind):
    """What a variable of the MIR type ``kind`` is, for a refusal naming it (#300): a
    "reference", a "raw pointer", a "closure", a "coroutine" (an `async` block's or fn's
    too); None for another type that may hold a pointer (`Option<&T>`, `NonNull<T>`)."""
    kind = kind or ""
    if kind.startswith("&"):
        return "reference"
    if _RAW_ONLY.match(kind):
        return "raw pointer"
    if kind.startswith("{closure@"):
        return "closure"
    if kind.startswith(("{coroutine@", "{async")):
        return "coroutine"
    return None


def _copy_verdict(
    part, local, holds, points, formatting, compares, reads, own=frozenset(), touched=None
):
    """(hold, point): whether the assignment ``part`` (``Body._copy_parts``) may make its
    target hold a value read from ``local``, or point into it, given the temporaries that
    may already (``holds``, ``points``; ``own``: the pointers made from a borrow of it).

    With ``compares`` (the dump's `std::ptr::eq` and `addr_eq` are the standard
    library's: ``OwnedLocals._std_compares``), such a call reads the two addresses it is
    given and nothing they point at (#300): what it returns holds a copy only where they
    do. Any other call given a pointer may read through it.

    ``reads`` says which pointers count (#300: what a refusal may say): "any", also one
    Peeky does not follow (read through, or a reference variable a call is given), which
    may point anywhere; "traced", only one the flow shows may point into ``local``
    (``points``, ``own``); "value", none: only reads of it by value (``touched``: of a
    place that may overlap the part a change makes, where given), or of what holds one.
    What surely read it is ``Body.read_copies_at``'s."""
    _, mentioned, through, borrow, call, formats, pointers, compare = part
    if compares and compare:
        return local in mentioned or bool(mentioned & holds), False
    if reads == "value":
        mine = local in mentioned if touched is None else touched
        return bool(mentioned & holds) or (not borrow and mine), False
    if reads == "traced":
        through = through and bool(mentioned & (points | own))
        points, pointers = points | own, frozenset()
    pointing = bool(mentioned & points) or any(n != local for n in pointers)
    if borrow:
        return bool(mentioned & holds), local in mentioned or through or pointing
    # `format_args!`'s constructors keep the pointers they are given.
    hold = local in mentioned or through or bool(mentioned & holds)
    return hold or (call and pointing and not (formatting and formats)), pointing


def _touches(text, local, projection):
    """Whether ``text`` mentions a place of ``local`` that may hold part of
    ``projection``: one not surely apart from it (another field, another constant
    element), or one not read as a place."""
    if not projection:
        return True
    for found in re.finditer(rf"(?<![\w])_{local}\b", text):
        place = _place_steps(text, found.start())
        if place is None:
            return True
        if all(
            mine == theirs or mine[0] != theirs[0] or None in (mine[1], theirs[1])
            for mine, theirs in zip(place[0], projection)
        ):
            return True
    return False


def _reads_part(value, local, projection, formatted=False):
    """Whether ``value`` (a statement's right-hand side, or a whole terminator) surely
    reads part of ``projection`` of ``local``: a `copy` or `move` of a place that
    overlaps it, or with ``formatted``, the shared borrow `&place` the value is."""
    for found in re.finditer(rf"(?<![\w*])_{local}\b", value):
        place = _place_steps(value, found.start())
        if place is None:
            continue
        steps, begin, end = place
        before = value[:begin]
        read = before.endswith(("copy ", "move ")) or (
            formatted and before == "&" and end == len(value)
        )
        if read and _overlaps(steps, projection):
            return True
    return False


# A write through a local's pointer: `(*_6) = …`, `((*_6).0: u32) = …`.
_WRITE_THROUGH = re.compile(r"^\(+\*_([0-9]+)\)[^=]* = ")


def _written_place(text, local):
    """The steps of the place of ``local`` the statement ``text`` assigns (`(_1.0: u32)
    = …`: [("field", 0)]; `_1[_5] = …`: [("index", None)]), or None where it assigns
    none of it (#300)."""
    written = re.match(rf"\(*_{local}\b", text)
    if written is None:
        return None
    place = _place_steps(text, written.end() - len(f"_{local}"))
    if place is None or not text.startswith(" = ", place[2]):
        return None
    return place[0]


def _apart(steps, projection):
    """Whether the place ``steps`` is surely apart from the part ``projection`` (another
    field, another constant element), so a write to it leaves that part as it was; never
    for the whole local (()) or a part not known (None)."""
    if not projection:
        return False
    return any(
        mine != theirs and mine[0] == theirs[0] and None not in (mine[1], theirs[1])
        for mine, theirs in zip(steps, projection)
    )


def _root_access(text, local, projection=(), formats=None):
    """What the MIR statement ``text`` surely does to the part ``projection`` of
    ``local`` (#192 review: ``Body.copy_undoes``): "read" (a `copy` or `move` of a place
    that overlaps it, or std's printing or formatting of a borrow of one: ``formats``,
    ``Body._format_reads``), "end" (an assignment to the whole local or to a place
    covering the part, or the end of its storage), or None (anything else: a borrow, a
    write to another part)."""
    text = _STRING.sub('""', text)
    if text.startswith(f"StorageDead(_{local})"):
        return "end"
    if _NOT_A_USE.match(text) or _DROP.match(text):
        return None
    formatted = (formats or {}).get(text, ())
    if any(_reads_part(borrow, local, projection, True) for borrow in formatted):
        return "read"
    if whole := _WHOLE_ASSIGNMENT.match(text):
        target = int(whole[1])
        if _reads_part(whole[2], local, projection):
            return "read"
        return "end" if target == local else None
    if written := re.match(rf"\(*_{local}\b", text):
        place = _place_steps(text, written.end() - len(f"_{local}"))
        if place is not None and text.startswith(" = ", place[2]):
            if _reads_part(text[place[2] + 3 :], local, projection):
                return "read"
            steps = place[0]
            covered = projection is not None and (
                len(steps) <= len(projection) and _overlaps(steps, projection)
            )
            return "end" if covered else None
    return "read" if _reads_part(text, local, projection) else None


@dataclass(frozen=True)
class StopBlock:
    """Where a stop's pc is (#115): the names of the variables each lexical block
    declares (sorted), from the innermost DWARF block holding the pc out to the
    function's own scope; and how many of the function's blocks have each such chain."""

    chain: tuple
    chains: dict


def stop_block(blocks, pc):
    """The ``StopBlock`` of ``pc`` (unslid) in a function's ``CodeBlock``s, or None where
    the pc is outside its own code, in an inlined call, or in two blocks at once."""
    if not blocks or type(pc) is not int or blocks[0].parent is not None:
        return None

    def holds(block):
        return any(low <= pc < high for low, high in block.ranges)

    if not holds(blocks[0]):
        return None
    children = {}
    for index, block in enumerate(blocks[1:], 1):
        children.setdefault(block.parent, []).append(index)
    current = 0
    for _ in range(len(blocks)):
        inside = [index for index in children.get(current, ()) if holds(blocks[index])]
        if not inside:
            break
        if len(inside) > 1 or blocks[inside[0]].inlined:
            return None
        current = inside[0]

    def chain(index):
        names = []
        while index is not None:
            names.append(blocks[index].names)
            index = blocks[index].parent
        return tuple(names)

    counts = {}
    for index, block in enumerate(blocks):
        if not block.inlined:
            counts[chain(index)] = counts.get(chain(index), 0) + 1
    return StopBlock(chain(current), counts)


@dataclass(frozen=True)
class LineFacts:
    column: int | None
    function: object  # the IrFunction
    inlined: frozenset  # columns of the calls inlined on the line
    macros: frozenset  # columns where a macro's path starts on the line
    to_bytes: object  # a column of the line in characters (the MIR's) to bytes (DWARF's)
    known: bool = True  # the line's text is known: ``macros`` holds all its macros


# Where a macro's path starts (`println!(`, `std::vec![`); `if !(x)` too, which only
# widens what a column can be.
_MACRO_CALL = re.compile(r"(?<![\w:])((?:::)?(?:\w+::)*\w+)\s*!\s*[(\[{]")


# A type nothing can change behind a shared reference, holding no reference of its own
# (#156): integers, floats, bools, chars, `str`, `String`, and `Vec`s, `Box`es, arrays,
# slices and tuples of them.
# A function's header prints its arguments' types by their short names (`&Vec<String>`),
# its locals' by their whole paths: either, where the crate names nothing else so
# (OwnedLocals._std_names).
_FROZEN_LEAF = r"(?:[iu](?:8|16|32|64|128|size)|f32|f64|bool|char|str|(?:std::string::)?String)"
_FROZEN_TYPE = re.compile(
    rf"(?:(?:std::vec::)?Vec<|(?:std::boxed::)?Box<|\[|\()*"
    rf"{_FROZEN_LEAF}(?:(?:, {_FROZEN_LEAF})*,?\)|; [0-9_a-z]+\]|\]|>)*"
)
_FROZEN_NAMES = (
    "Vec",
    "String",
    "Box",
    "str",
    "bool",
    "char",
    "f32",
    "f64",
    *(f"{sign}{bits}" for sign in "iu" for bits in ("8", "16", "32", "64", "128", "size")),
)


# One pass over a source's code (comments and strings blanked) for the names it may
# give its own items (OwnedLocals._untrusted_names): a statement's end, a macro's
# opening (`mk!(`, `#![`), a word (`r#Vec` is `Vec`), a `macro_rules!` body's opening.
_NAMING = re.compile(r"(;)|(!\s*[(\[{])|(?:r#)?(\w+)")
_DECLARING = frozenset({"struct", "enum", "union", "type", "trait", "mod"})
_MACRO_NAME = re.compile(r"\s*!\s*(?:r#)?(\w+)(?:\s*([({\[]))?")
# One space (more count as another crate's, as the per-name search had it).
_STD_USE = re.compile(r"\s(?:::)?(?:std|core|alloc)::")
_TYPE_FOLLOWS = re.compile(r"\s*(?:::|<|\()")


def _closings(code):
    """{an opening bracket's index: its closing one's} in ``code`` (one pass; brackets
    not closed run to its end)."""
    closing, opened = {}, []
    for bracket in re.finditer(r"[()\[\]{}]", code):
        if bracket[0] in "([{":
            opened.append(bracket.start())
        elif opened:
            closing[opened.pop()] = bracket.start()
    return closing


def _passes_std(code):
    """True if a macro invocation in ``code`` (comments and strings blanked) has `std` or
    `core` among its arguments other than as a path's start (`make_mod!(std)`)."""
    closing = _closings(code)
    for opening in _MACRO_OPENING.finditer(code):
        start = opening.start(1)
        if _BARE_STD.search(code, start + 1, closing.get(start, len(code))):
            return True
    return False


def _named_own(code, names):
    """Which of ``names`` the source ``code`` (comments and strings blanked) may give an
    item of its own (see OwnedLocals._untrusted_names), in one pass over its words."""
    closing, found = _closings(code), set()
    # A `use` up to its `;` (and whether it imports from std, core or alloc); a macro's
    # arguments up to the next `;`; the end of the `macro_rules!` body a word is in.
    using = std_use = macro = False
    body_end, previous = -1, None
    for token in _NAMING.finditer(code):
        if token[1]:
            using = macro = False
            previous = None
            continue
        if token[2]:
            macro = True
            previous = None
            continue
        word, start, end = token[3], token.start(), token.end()
        # `r#Vec` counts as bare even after a path's `::`.
        before = "#" if token[0].startswith("r#") else code[start - 1] if start else ""
        if word in names:
            gap = code[previous[1] : start] if previous else ""
            if (
                (previous and previous[0] in _DECLARING and gap.strip() == "" and gap)
                or (using and previous and previous[0] == "as" and gap.strip() == "" and gap)
                or (using and not std_use)
                or (
                    macro
                    and word in ("Vec", "String", "Box")
                    and before != ":"
                    and not _TYPE_FOLLOWS.match(code, end)
                )
                or (start < body_end and before not in (":", "$"))
            ):
                found.add(word)
        if word == "use" and token[0] == "use":
            # A `use` from elsewhere counts until the `;`, whatever follows it.
            std_use = bool(_STD_USE.match(code, end)) and (std_use or not using)
            using = True
        elif word == "macro_rules" and (named := _MACRO_NAME.match(code, end)):
            if named[1] in names:
                found.add(named[1])
            if named[2]:
                body_end = max(body_end, closing.get(named.start(2), len(code)))
        previous = (word, end)
    return found


# A call and its edges: `-> [return: bb3, unwind: bb9]`, `unwind continue`, `unwind
# unreachable`, `unwind terminate(abi)`.
_CALL = re.compile(
    r"([^()]+)\((.*)\) -> \[return: bb[0-9]+(?:, unwind(?:: bb[0-9]+| \w+(?:\(\w+\))?))?\]"
)
_ITERATOR_NEXT = re.compile(r"<.+ as (?:(?:std|core)::iter::)?Iterator>::next$")


def _place_base(place):
    """(the local a MIR place starts from, how many pointers it goes through) for a
    place (`((_25 as Some).0: &str)`, `(*_18)`, `(*_20)[_5]`), or None for a cast or a
    place naming several locals."""
    text = re.sub(r" as \w+\)", ")", place)
    if " as " in text:
        return None
    mentioned = _MENTIONED.findall(re.sub(r"\[_[0-9]+\]", "", text))
    return (int(mentioned[0]), text.count("(*")) if len(mentioned) == 1 else None


def _frozen_sources(body, local, seen, iterator):
    """{(owner, whether it is a shared reference argument)} whose storage (or, for an
    argument, whose referent) holds everything ``local``'s values may point into, or
    None where a value is made otherwise (see OwnedLocals._borrows_frozen)."""
    if local in seen:
        return set()
    seen.add(local)
    if local in body.arguments and local not in body._assignments():
        return {(local, True)} if OwnedLocals._shared_argument(body, local) else None
    values = body._assignments().get(local)
    if not values:
        return None
    if local in body._written_through() and not _only_iterated(body, local, iterator):
        return None
    found = set()
    for value in values:
        value = _STRING.sub('""', value)
        if call := _CALL.fullmatch(value):
            more = _call_sources(body, call, seen, iterator)
        elif value.startswith(("&mut ", "&raw ", "&fake ")):
            return None
        elif value.startswith("&") and (place := _place_base(value[1:])) is not None:
            base, through = place
            kind = body.types.get(base, "")
            if through == 0 and not _BORROWING.search(kind):
                # Into the variable's own storage (or what its Box holds).
                more = {(base, False)}
            elif through == 1 and value[1:].count(f"(*_{base})") == 1:
                if kind.startswith("&") and not kind.startswith("&mut "):
                    more = _frozen_sources(body, base, seen, iterator)
                elif re.match(r"std::boxed::Box<", kind):
                    more = {(base, False)}
                else:
                    return None
            else:
                return None
        elif (copied := re.fullmatch(r"(?:copy|move) (.+)", value)) and (
            place := _place_base(copied[1])
        ):
            if place[1]:
                return None
            more = _frozen_sources(body, place[0], seen, iterator)
        else:
            return None
        if more is None:
            return None
        found |= more
    return found


def _frozen_owners(body, local, iterator, untrusted=()):
    """The frozen owners of the reference ``local`` (see OwnedLocals._borrows_frozen),
    each but whether it is initialized at the stop, or None. ``untrusted``: the short
    type names (`Vec`) that may be the crate's own (OwnedLocals._untrusted_names)."""
    if body.generics is None:
        # A twin a macro generates: any type name may be a type parameter.
        return None
    owners = _frozen_sources(body, local, set(), iterator)
    scopes = _binding_scopes(body, local)
    if not owners or len(scopes) != 1:
        return None
    assigned = body._assignments()
    for owner, argument in owners:
        kind = body.types.get(owner, "")
        if mentions(kind, body.generics) or mentions(kind, untrusted):
            # `fn f<i8>(v: Vec<i8>)` may hold anything, as may a `Vec` the crate declares.
            return None
        if argument:
            if not _FROZEN_TYPE.fullmatch(kind[1:]):
                return None
            continue
        theirs = _binding_scopes(body, owner)
        if (
            len(theirs) != 1
            or not _encloses(body, theirs[0], scopes[0])
            or not _FROZEN_TYPE.fullmatch(kind)
            or owner in body._written_through()
            or owner in body._moved()
            # Assigned once, or a by-value argument never assigned.
            or len(assigned.get(owner, ())) != (0 if owner in body.arguments else 1)
        ):
            return None
    return frozenset(owners)


def _call_sources(body, call, seen, iterator):
    """What a call's result may point into: what its borrowing operands do. An operand
    borrowed `&mut` is allowed only as `Iterator::next`'s iterator, whose items borrow
    what the iterator does, never the iterator."""
    # A char constant (`const ','`) holds no separator.
    operands = _top_level(re.sub(r"const '(?:\\[^']*|[^'\\])'", "const 'c'", call[2]))
    if call[1].startswith(("move ", "copy ", "(")):
        return None  # a closure or a function pointer called
    found = set()
    for operand in operands:
        if operand.startswith("const "):
            continue
        moved = re.fullmatch(r"(?:copy|move) _([0-9]+)", operand)
        if moved is None:
            return None
        number = int(moved[1])
        kind = body.types.get(number)
        if kind is None:
            return None
        if kind.startswith("&mut "):
            if not (iterator and _ITERATOR_NEXT.match(call[1]) and len(operands) == 1):
                return None
            iterated = _iterated(body, number)
            if iterated is None:
                return None
            more = _frozen_sources(body, iterated, seen, iterator)
        elif _BORROWING.search(kind):
            if _RAW_POINTER.search(kind):
                return None
            more = _frozen_sources(body, number, seen, iterator)
        else:
            continue
        if more is None:
            return None
        found |= more
    return found


def _iterated(body, local):
    """The iterator a `&mut` operand of `Iterator::next` reborrows (`_26 = &mut (*_27)`,
    `_27 = &mut _22`: `_22`), or None."""
    for _ in range(8):
        values = body._assignments().get(local, [])
        if len(values) != 1:
            return None
        if found := re.fullmatch(r"&mut \(\*_([0-9]+)\)", values[0]):
            local = int(found[1])
            continue
        found = re.fullmatch(r"&mut _([0-9]+)", values[0])
        return int(found[1]) if found else None
    return None


def _only_iterated(body, local, iterator):
    """True if ``local`` is borrowed `&mut` only for `Iterator::next` (`_27 = &mut _22;
    _26 = &mut (*_27); _25 = <… as Iterator>::next(move _26)`): each such borrow, and
    each reborrow of it, mentioned nowhere else, so no `&mut` to it outlives a call.
    Found once per body and iterator."""
    if not iterator:
        return False
    if body._iterated_cache is None:
        body._iterated_cache = {}
    if local not in body._iterated_cache:
        body._iterated_cache[local] = _iterated_only(body, local)
    return body._iterated_cache[local]


def _iterated_only(body, local):
    mentions, assigned = body._mentions(), body._assignments()
    chain, pending = set(), [local]
    while pending:
        target = pending.pop()
        for text in mentions.get(target, ()):
            found = re.fullmatch(rf"_([0-9]+) = &mut (?:_{target}|\(\*_{target}\));", text)
            if not found:
                continue
            number = int(found[1])
            if len(assigned.get(number, ())) != 1 or number in chain:
                return False
            chain.add(number)
            pending.append(number)
    for number in chain:
        for text in mentions.get(number, ()):
            if (
                _NO_CODE.match(text)
                or re.match(rf"_{number} = &mut ", text)
                or re.fullmatch(rf"_[0-9]+ = &mut \(\*_{number}\);", text)
                or (
                    (call := _CALL.fullmatch(text.removesuffix(";")))
                    and _ITERATOR_NEXT.match(call[1].partition(" = ")[2])
                    and call[2] == f"move _{number}"
                )
            ):
                continue
            return False
    # The iterator itself: assigned whole, never moved, partly assigned or borrowed
    # otherwise.
    return (
        local not in body._moved()
        and len(assigned.get(local, ())) == 1
        and not any(
            re.search(rf"&(?:mut |raw \w+ |fake \w+ )?\(*_{local}\b", text)
            and not re.fullmatch(rf"_[0-9]+ = &mut _{local};", text)
            for text in mentions.get(local, ())
        )
    )


def _binding_scopes(body, local):
    return [b[4] for found in body.bindings.values() for b in found if b[0] == local]


def _encloses(body, outer, inner):
    for _ in range(4096):
        if inner == outer:
            return True
        if inner is None:
            return False
        inner = body.scopes.get(inner)
    return False


def _through_box(place, boxes, paths):
    """(Box local, the place with the pointer named by its Box) for a place through a
    pointer made from a Box (`((*_13).1: String)`), else None."""
    found = re.search(r"\(\*_([0-9]+)\)", place)
    if found is None or int(found[1]) not in boxes or not place.lstrip("(").startswith("*"):
        return None
    temp = int(found[1])
    return boxes[temp], place[: found.start()] + f"(*{paths[temp]})" + place[found.end() :]


def _span(block, index):
    try:
        return block.spans[index]
    except IndexError:
        return None


def _call(block, index):
    """True if statement ``index`` is the block's terminator and a call."""
    if index != len(block.statements) - 1 or "->" not in block.terminator:
        return False
    return not re.match(
        r"(?:drop|assert|switchInt|goto|falseEdge|falseUnwind|yield)\b", block.terminator
    )


def _target(block):
    """The block a call or drop returns to, if any. A lone `-> bbN` is a drop's return
    (in cleanup code) but a diverging call's unwind."""
    edges = block.terminator.rpartition("->")[2]
    if found := _RETURN_EDGE.search(edges):
        return int(found[1])
    found = re.fullmatch(r" bb([0-9]+);", edges)
    return int(found[1]) if found and _DROP.match(block.terminator) else None


def _scope(block, index):
    try:
        return block.scopes[index]
    except IndexError:
        return None


# #259: the standard library's address comparisons, which read the two pointers they are
# given and nothing else. The dump names `core::ptr::eq` `std::ptr::eq` (never `eq`
# alone: many items are), and `addr_eq` alone where no other item is so named
# (``OwnedLocals._std_compares`` says when those names are the standard library's).
_ADDRESS_COMPARISON = re.compile(
    r"^_([0-9]+) = (?:(?:std|core)::ptr::eq|(?:(?:std|core)::ptr::)?addr_eq)::<.+>\((.*)\)"
    r" -> \[return: bb[0-9]+(?:, unwind(?:: bb[0-9]+| \w+(?:\(\w+\))?))?\];$"
)
# A function of the crate's own that a call could be dumped under one of those names
# (`fn std::ptr::eq`, `fn inner::addr_eq`): its header's path, any prefix.
_COMPARISON_NAME = re.compile(r"(?:^|::)(?:ptr::eq|addr_eq)$")
# The standard library's conversion of the Box `vec!` fills into the Vec it gives (#311):
# what the Box holds, the Vec holds. Only where ``OwnedLocals._std_transfers`` proves it.
_STD_TRANSFER = re.compile(
    r"^(?:std|alloc)::boxed::box_assume_init_into_vec_unsafe::<.+>\(move _([0-9]+)\) -> "
)
_TRANSFER_NAME = re.compile(r"(?:^|::)box_assume_init_into_vec_unsafe$")
# An invocation's opening, as ``_MACRO_CALL``, and a `std` or `core` in its arguments
# that is not a path's start (`make_mod!(std)` may declare `mod std`); and an `extern
# crate` renamed so.
_MACRO_OPENING = re.compile(r"(?<![\w:])(?:(?:::)?(?:\w+::)*\w+)\s*!\s*([(\[{])")
_BARE_STD = re.compile(r"\b(?:std|core)\b(?!\s*::)")
# Either name may be a raw identifier (`extern crate evil as r#std;`).
_EXTERN_AS_STD = re.compile(r"\bextern\s+crate\s+(?:r#)?\w+\s+as\s+(?:r#)?(?:std|core)\b")


def _false(why, reason):
    """False, with ``reason`` added to ``why`` where one is kept (OwnedLocals._judge)."""
    if why is not None:
        why.append(reason)
    return False


def _gap(body, function, number, index):
    """(kind, line) of the statement ``gaps_before`` found at ``index`` of block
    ``number`` (see OwnedLocals._judge); ("start", None) for (None, None)."""
    if number is None:
        return ("start", None)
    block = body.blocks[number]
    text, spanned, spanned_line = block.statements[index]
    line = spanned_line if spanned == function.file else None
    if text.startswith("StorageDead("):
        return ("end", line)
    if _call(block, index):
        return ("call", line)
    if _DROP.match(text):
        return ("drop", line)
    return ("write", line)


def _harmless(text, borrowed, types=None):
    """True if the statement cannot move, drop, free or write what a reference points
    into: a branch, an assertion, a storage marker of a local never borrowed itself, a
    whole assignment (not a call) to a local never borrowed itself; with ``types`` (the
    body's {local: type}, given where the dump's `std::ptr::eq` and `addr_eq` are the
    standard library's), a call of one of them on two raw pointer locals whose result
    goes to such a local (#259: `ptr::eq(f, e.payer)` only compares two addresses)."""
    if _BRANCHES.match(text) or text.startswith("goto ") or _NOT_A_USE.match(text):
        return True
    if storage := re.match(r"^StorageDead\(_([0-9]+)\);$", text):
        return int(storage[1]) not in borrowed
    if text == "nop;":
        return True
    if types is not None and (found := _ADDRESS_COMPARISON.match(text)):
        operands = [re.fullmatch(r"(?:move|copy) _([0-9]+)", o) for o in _top_level(found[2])]
        return (
            len(operands) == 2
            and all(
                operand and re.match(r"\*(?:const|mut) ", types.get(int(operand[1]), ""))
                for operand in operands
            )
            and int(found[1]) not in borrowed
        )
    found = _WHOLE_ASSIGNMENT.match(text)
    return bool(found) and "->" not in text and int(found[1]) not in borrowed


def _kills(text):
    """Locals a statement or terminator moves out of, drops or ends."""
    killed = {int(n) for n in _MOVE.findall(text)}
    if storage := _STORAGE.match(text):
        killed.add(int(storage[1]))
    if drop := _DROP.match(text):
        killed.add(int(drop[1]))
    return killed


def _transfer(text, state):
    state &= ~_mask(_kills(text))
    if assigned := _ASSIGN.match(text):
        state |= 1 << int(assigned[1])
    return state


# Where a local's storage begins anew, its bytes are what the slot last held (#132).
_STORAGE_BEGINS = re.compile(r"^(?:StorageLive|Deinit)\(_([0-9]+)\);$")


def _top_level(text):
    """``text`` split at the commas outside brackets (`_1: HashMap<K, V>, _2: u8`)."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in "<([{":
            depth += 1
        elif char in ")]}" or char == ">" and text[index - 1 : index] != "-":
            # `->` in a function type closes nothing.
            depth -= 1
        if char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def parse_body(text):
    """One `-Zdump-mir` body, or None when the file holds no function body."""
    lines = split_lines(text)
    start = next((i for i, line in enumerate(lines) if line.startswith("fn ")), None)
    if start is None or not (header := _HEADER.match(lines[start])):
        return None
    arguments = frozenset(int(n) for n in _ARGUMENT.findall(header[2]))
    body = Body(header[1], arguments, None, {}, {})
    lines, saved = _saved_locals(body, lines, start)
    # Arguments are typed in the header only (`fn f(_1: &[u8], _2: usize)`).
    for argument in _top_level(header[2]):
        if found := re.fullmatch(r"_([0-9]+): (.+)", argument):
            body.types[int(found[1])] = found[2]
    block, spans, opened_scopes = None, [], [0]
    for line in lines[start + 1 :]:
        if line == "}":
            break
        if line.lstrip().startswith("//") or not line.strip():
            continue
        if local := _LOCAL.match(line):
            if local[1] == "0":
                body.file = local[3]
            body.types[int(local[1])] = local[2]
            spans.append((local[3], int(local[4])))
            continue
        if debug := _DEBUG.match(line):
            whole = _WHOLE_LOCAL.match(debug[2])
            # A binding through a projection (a closure's capture, an async local) has no
            # whole local, but names its type: `(_1.0: std::boxed::Box<Pt>)`.
            typed = None if whole else _projected_type(debug[2])
            if not whole:
                body.places[(debug[1], debug[4], int(debug[5]), int(debug[3]))] = debug[2]
            body.bindings.setdefault(debug[1], []).append(
                (
                    int(whole[1]) if whole else None,
                    debug[4],
                    int(debug[5]),
                    typed,
                    int(debug[3]),
                )
            )
            if full := _FULL_SPAN.match(line[debug.start(4) :]):
                body.binding_ends.setdefault((debug[1], debug[4], int(debug[5])), []).append(
                    (int(full[4]), int(full[5]))
                )
            spans.append((debug[4], int(debug[5])))
            continue
        if block is None and (scope := _SCOPE.match(line)):
            body.scopes[int(scope[1])] = opened_scopes[-1]
            opened_scopes.append(int(scope[1]))
            if "(inlined " in line:
                body.inlined_scopes.add(int(scope[1]))
            continue
        if block is None and len(opened_scopes) > 1 and re.fullmatch(r" +\}", line):
            opened_scopes.pop()
            continue
        if opened := _BLOCK.match(line):
            block = body.blocks.setdefault(int(opened[1]), Block())
            continue
        if line == "    }":
            if block is not None and not _finish(block):
                body.supported = False
            block = None
            continue
        if block is None:
            # Locals, scopes and their closing braces come before the blocks.
            if not re.match(r"^ +(?:let |scope [0-9]+ \{$|\}$)", line):
                body.supported = False
            continue
        statement = _STATEMENT.match(line)
        if not statement:
            body.supported = False
            continue
        span = _SPAN.match(statement[3])
        block.statements.append(
            (statement[1], span[1] if span else None, int(span[2]) if span else None)
        )
        block.scopes.append(int(statement[2]))
        full = _FULL_SPAN.match(statement[3])
        block.spans.append((int(full[3]), int(full[4]), int(full[5])) if full else None)
        if span:
            spans.append((span[1], int(span[2])))
            # Every line the span covers: rustc may put the code on a later one (a method
            # call's on its name's line, `K\n.get()`, while its span starts at `K`).
            end = _SPAN_END.match(statement[3])
            last = int(span[2])
            if end and int(end[1]) > last and not _SPAN_ONLY.match(statement[1]):
                last = int(end[1])
            for number in range(int(span[2]), last + 1):
                body.line_scopes.setdefault((span[1], number), set()).add(int(statement[2]))
    if block is not None or not body.blocks or 0 not in body.blocks or body.file is None:
        body.supported = False
    body.lines = frozenset(number for file, number in spans if file == body.file)
    if body.coroutine is not None and body.coroutine >= 0:
        body.types.update(saved)
        body.saved = frozenset(saved)
        _suspensions(body)
    return body


# `_34 = copy (_1.0: &mut {async fn body of f()})`: bb0 of a coroutine's body takes the
# pointer to its state from its pinned `self`.
_STATE = re.compile(r"^        _([0-9]+) = copy \(_1\.0: &mut \{.+\}\); +// ")


def _saved_locals(body, lines, start):
    """A coroutine's body (an `async fn`'s or block's, #116) keeps the locals it holds
    across a suspension in its state, as fields of the suspension's variant (`(((*_34)
    as variant#3).0: &Pt)`), which the debug info binds and the code reads and writes as
    it would the local. Each is renamed a local of its own, numbered past the body's,
    so that it is judged as one: (the lines renamed, {local: its type})."""
    if not re.match(r"_1: (?:(?:std|core)::pin::)?Pin<&mut \{", _HEADER.match(lines[start])[2]):
        return lines, {}
    opened = next((i for i in range(start, len(lines)) if lines[i] == "    bb0: {"), None)
    state = (
        _STATE.match(lines[opened + 1]) if opened is not None and opened + 1 < len(lines) else None
    )
    if state is None:
        # A coroutine whose state is not found: what it keeps is not followed.
        body.coroutine, body.supported = -1, False
        return lines, {}
    body.coroutine = number = int(state[1])
    place = re.compile(rf"\(\(\(\*_{number}\) as variant#([0-9]+)\)\.([0-9]+): ")
    last = max((int(n) for line in lines[start:] for n in _MENTIONED.findall(line)), default=0)
    named, saved, renamed = {}, {}, list(lines[: start + 1])
    for line in lines[start + 1 :]:
        for found in reversed(list(place.finditer(line))):
            depth, end = 0, None
            for index in range(found.start(), len(line)):
                depth += {"(": 1, ")": -1}.get(line[index], 0)
                if depth == 0:
                    end = index
                    break
            if end is None:
                body.supported = False
                break
            kind = line[found.end() : end]
            key = (int(found[1]), int(found[2]))
            if key not in named:
                last += 1
                named[key] = last
            local = named[key]
            if saved.setdefault(local, kind) != kind:
                body.supported = False
            line = f"{line[: found.start()]}_{local}{line[end + 1 :]}"
        # Any other place in a variant of the state is not understood.
        if f"(*_{number}) as variant#" in line:
            body.supported = False
        renamed.append(line)
    return renamed, saved


def _suspensions(body):
    """Each block that suspends the coroutine (sets its state's variant to a suspension,
    3 or more, and returns), with the block its dispatch resumes it at."""
    entry = body.blocks.get(0)
    state = body.coroutine
    dispatch = re.fullmatch(
        r"switchInt\(move _([0-9]+)\) -> \[(.*)\];", entry.terminator if entry else ""
    )
    texts = [text for text, _, _ in entry.statements] if entry else []
    if (
        dispatch is None
        or len(texts) != 3
        or texts[1] != f"_{dispatch[1]} = discriminant((*_{state}));"
    ):
        body.supported = False
        return
    targets = {
        int(value): int(target)
        for value, target in re.findall(r"\b([0-9]+): bb([0-9]+)", dispatch[2])
    }
    for number, block in body.blocks.items():
        if block.terminator != "return;":
            continue
        texts = [text for text, _, _ in block.statements[:-1] if not _NO_CODE.match(text)]
        found = re.fullmatch(
            rf"discriminant\(\(\*_{state}\)\) = ([0-9]+);", texts[-1] if texts else ""
        )
        if found is None or int(found[1]) < 3:
            continue
        if int(found[1]) not in targets:
            body.supported = False
            return
        body.resumes[number] = targets[int(found[1])]


def _finish(block):
    """Classify the block's last statement as its terminator; False if not understood."""
    if not block.statements:
        return False
    text = block.statements[-1][0]
    block.terminator = text
    if _UNSUPPORTED.match(text):
        return False
    if _EXITS.match(text):
        block.successors = ()
        return True
    if "->" not in text:
        return False
    edges = text.rpartition("->")[2]
    block.successors = tuple(int(n) for n in _SUCCESSOR.findall(edges))
    if _ASSIGN.match(text):
        returned = _RETURN_EDGE.search(edges)
        block.returns = int(returned[1]) if returned else None
    return True


def load_bodies(directory, crate, defined=None):
    """Every `PreCodegen` body rustc dumped for ``crate``; empty when over budget. The
    path of every function dumped, parsed or not, is added to the set ``defined`` if
    given (OwnedLocals._std_compares)."""
    directory = Path(directory)
    files = sorted(directory.glob(f"{crate}.*.PreCodegen.after.mir"))
    if sum(path.stat().st_size for path in files) > MAX_MIR_BYTES:
        return []
    bodies = []
    for path in files:
        # As rustc wrote it: no newline translation, and no byte read as another (#188).
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        if defined is not None:
            defined.update(found[1] for found in _FUNCTION_HEADER.finditer(text))
        body = parse_body(text)
        if body is not None:
            bodies.append(body)
    return bodies


# A dumped function's header, up to its parameters: `fn std::ptr::eq(`.
_FUNCTION_HEADER = re.compile(r"^fn (.+?)\(", re.MULTILINE)


_DEFINE = re.compile(r'^define .*?@("?)([^"(\s]+)\1\(.*!dbg (![0-9]+) \{$')
_LEGACY_HASH = re.compile(r"17(h[0-9a-f]{16})E$")
# A block codegen left unreachable (the dead arm of `if cfg!(debug_assertions)` in
# `debug_assert!`) is labeled `; No predecessors!`.
# LLVM quotes a label with characters such as `$` (an inlined `Box::new`'s exit block).
_LABEL = re.compile(r'^([-$._A-Za-z0-9]+|"[^"]+"):(?:\s+; (?:preds = (.*)|No predecessors!))?$')
_DBG = re.compile(r"!dbg (![0-9]+)")
_META = re.compile(
    r"^(![0-9]+) = (?:distinct )?!(DILocation|DISubprogram|DILexicalBlock|DILexicalBlockFile"
    r"|DIFile|DINamespace|DICompositeType|DITemplateTypeParameter|DITemplateValueParameter"
    r"|DILocalVariable|DICompileUnit)\((.*)\)\s*$"
)
_METAFIELDS = {
    key: re.compile(rf'(?:^|, ){key}: ("(?:[^"\\]|\\.)*"|![0-9]+|[0-9]+)')
    for key in (
        "name",
        "linkageName",
        "scope",
        "file",
        "line",
        "column",
        "inlinedAt",
        "filename",
        "directory",
        "templateParams",
    )
}
# A short metadata list (`!2313 = !{!2314}`): a subprogram's template parameters.
_META_LIST = re.compile(r"^(![0-9]+) = (?:distinct )?!\{((?:![0-9]+(?:, )?){0,64})\}\s*$")
# An instruction that surely leaves code at its location: a call (not an LLVM intrinsic,
# which may lower to nothing) or a conditional branch.
_IR_CALL = re.compile(r"^\s*(?:%\S+ = )?(?:tail |musttail |notail )?(?:call|invoke) (?!.*@llvm\.)")
_IR_BRANCH = re.compile(r"^\s*(?:br i1 |switch )")
# Code opt-level 0 surely emits at the instruction's location (#115): a branch on a value
# (not a constant), a store of bytes (not of nothing, nor of an undefined value).
_IR_SURE_BRANCH = re.compile(r"^\s*br i1 %")
_IR_STORE = re.compile(r"^\s*store (?:volatile )?(.+) (\S+), ptr ")
# An instruction's own line: not a label, a comment, an `invoke`'s `to label` line, a
# `switch`'s cases or its closing `]`.
_IR_INSTRUCTION = re.compile(r"^  (?! )(?![;\]])(?!to label )\S")


def _stores_bytes(found):
    kind, value = found[1].strip(), found[2]
    return (
        value not in ("undef", "poison")
        and kind not in ("{}", "{ }", "<{}>", "<{ }>")
        and not kind.startswith(("[0 x", "<0 x"))
    )


@dataclass
class IrFunction:
    name: str
    parent: str | None
    file: str
    blocks: dict  # {label: {line, ...}} for code of this function (not inlined)
    preds: dict  # {label: [label, ...]}
    line: int | None = None  # the DISubprogram's: where the function is declared
    # {label: ((line, column) of each own call in order, of each conditional branch)}:
    # a MIR block's call or branch is its terminator. {line: {column}} of the calls LLVM
    # inlined (where their code's `inlinedAt` chain leaves this function).
    events: dict = field(default_factory=dict)
    inlined: dict = field(default_factory=dict)
    # Lines LLVM inlined code of this function into other functions on (#116): a frame
    # with this hash may be such a copy (`#[inline(always)]`), whose layout ``blocks``
    # does not describe. ``inline_only``: it has no IR function of its own at all.
    copies: frozenset = frozenset()
    inline_only: bool = False
    # The names of the namespaces it is in, outermost first (its crate's, then its
    # modules', functions', closures'): `parent` is the last.
    ancestors: tuple = ()
    # The names of the type parameters this instantiation fills (a generic function's,
    # its impl's or its enclosing function's, for a closure): () for none, None when it
    # is generic in a way not understood (a const parameter). Its MIR body is generic,
    # so any type it declares may name one of them, whatever the name (`i8`).
    generics: tuple | None = ()
    # Its own lexical blocks (#115), each named as the DWARF names a block holding a stop
    # (`StopBlock.chain`): {chain: how many blocks with it have code, LLVM emitting a
    # DWARF block for each}, {(line, chain): how many have code of the line}, {label:
    # ((line, chain, the calls before it in the label), ...) for each instruction that
    # surely leaves code there: a call, a store of bytes, a branch on a value}, and
    # {label: its calls}. ``scoped``: every block's names are known.
    chains: dict = field(default_factory=dict)
    line_chains: dict = field(default_factory=dict)
    sure: dict = field(default_factory=dict)
    calls: dict = field(default_factory=dict)
    scoped: bool = False
    # A generic's instance as the DISubprogram names it, with its arguments (`tf<rc::A>`;
    # `name` drops them), else "": a frame's symbol hash is all that tells instances apart.
    instance: str = ""
    # {label: {line}}: the lines of its own file where code LLVM inlined into that label
    # is called from (a `#[inline(always)]` callee's `inlinedAt` chain leaving it).
    sites: dict = field(default_factory=dict)
    # {label: ((instruction kind, line of its own file or None, known), ...)}: where each
    # instruction is, in order (see _placed_at), unlocated ones included (#230 review).
    provenance: dict = field(default_factory=dict)
    # The labels an `invoke` ends: its result is stored in its normal successor.
    invokes: frozenset = frozenset()

    def call_lines(self, number):
        """The lines of its own file the IR block of MIR block ``number`` has code of (an
        inlined callee's by its call site; another file's code counts for none), where
        every instruction of the block says where it is; None where any does not, where
        an `invoke` ends it (the call's result is stored in its successor, unexamined),
        or where the IR names no block for it (codegen merged it into another). The
        whole block counts, not a call within it: which LLVM call is the MIR
        terminator's is not told (a helper may follow it, an inlined callee may call
        others, the call may be gone) (#230 review, round 2)."""
        label = "start" if number == 0 else f"bb{number}"
        found = self.provenance.get(label)
        if label not in self.blocks or not found or label in self.invokes:
            return None
        if not all(known for _, _, known in found):
            return None
        return {line for _, line, _ in found if line is not None}

    def agrees(self, block, line):
        """True if the IR's blocks with the names of a stop's DWARF block (``block``, a
        ``StopBlock``) are as many as the DWARF's, and just one has code of ``line``:
        that one is the stop's."""
        return (
            self.scoped
            and block.chains.get(block.chain) == self.chains.get(block.chain)
            and self.line_chains.get((line, block.chain)) == 1
        )

    def labeled(self):
        """MIR blocks that have an IR block of their own (`start` is bb0)."""
        if getattr(self, "_labeled", None) is None:
            self._labeled = {
                0 if label == "start" else int(label[2:])
                for label in self.blocks
                if label == "start" or re.fullmatch(r"bb[0-9]+", label)
            }
        return self._labeled

    def lines(self):
        return set().union(*self.blocks.values()) if self.blocks else set()

    def mir_blocks(self, line):
        """MIR blocks holding code of ``line``; None if a block's origin is unknown."""
        found, seen = set(), set()
        pending = [label for label, lines in self.blocks.items() if line in lines]
        if not pending:
            return None
        while pending:
            label = pending.pop()
            if label in seen:
                continue
            seen.add(label)
            if label == "start":
                found.add(0)
            elif match := re.fullmatch(r"bb([0-9]+)", label):
                found.add(int(match[1]))
            elif self.preds.get(label):
                # A codegen helper (panic, landing pad): it runs at the end of the MIR
                # block that branches to it.
                pending.extend(self.preds[label])
            else:
                return None
        return found


def _unquote(value):
    """A metadata field's value: a quoted string's text (ir_string), else as written."""
    return ir_string(value[1:-1]) if value and value.startswith('"') else value


def ir_functions(path, files):
    """{legacy hash: IrFunction} for functions defined in ``files`` (relative paths)."""
    path = Path(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        return {}
    raw, meta, current, pending = [], {}, None, ""
    # Bytes not UTF-8 stay apart (surrogates ir_string refuses), never one `\ufffd`.
    with path.open(encoding="utf-8", errors="surrogateescape", newline="") as stream:
        for line in stream:
            if line.startswith("!"):
                if found := _META_LIST.match(line):
                    meta[found[1]] = ("list", {"items": found[2].split(", ") if found[2] else []})
                elif found := _META.match(line):
                    meta[found[1]] = (
                        found[2],
                        {
                            key: value
                            for key, pattern in _METAFIELDS.items()
                            if (m := pattern.search(found[3]))
                            # A string LLVM does not write names nothing (#188 review).
                            and (value := _unquote(m[1])) is not None
                        },
                    )
                continue
            if current is None:
                if line.startswith("define ") and (found := _DEFINE.match(line.rstrip())):
                    hashed = _LEGACY_HASH.search(found[2])
                    current = (hashed[1] if hashed else None, found[3], {}, {}, ["start"], set())
                continue
            if line.startswith("}"):
                if pending:
                    # An `invoke` or branch whose location never came: unknown (#230 review).
                    current[2].setdefault(current[4][0], []).append((None, pending))
                    pending = ""
                raw.append(current)
                current = None
                continue
            if label := _LABEL.match(line.rstrip()):
                if pending:
                    current[2].setdefault(current[4][0], []).append((None, pending))
                    pending = ""
                name = label[1].strip('"')
                current[4][0] = name
                current[2].setdefault(name, [])
                if label[2]:
                    current[3][name] = [
                        p.strip().lstrip("%").strip('"') for p in label[2].split(",")
                    ]
                continue
            if "@llvm.dbg." in line or line.lstrip().startswith("#dbg_"):
                continue
            if _IR_CALL.match(line):
                kind = "c"
                if re.search(r"\binvoke ", line):
                    # It ends its block: its result is stored in the normal successor.
                    current[5].add(current[4][0])
            elif _IR_BRANCH.match(line):
                kind = "B" if _IR_SURE_BRANCH.match(line) else "b"
            elif (store := _IR_STORE.match(line)) and _stores_bytes(store):
                kind = "s"
            else:
                kind = ""
            if dbg := _DBG.search(line):
                # An `invoke`'s location is on its `to label ...` line.
                current[2].setdefault(current[4][0], []).append((dbg[1], kind or pending))
                pending = ""
            elif kind == "c" and not re.search(r"\binvoke ", line):
                # A call with no location: still one of the block's calls.
                current[2].setdefault(current[4][0], []).append((None, kind))
            elif kind in ("c", "b", "B"):
                pending = kind
            elif _IR_INSTRUCTION.match(line):
                # Code with no location: where it is cannot be told (#230 review).
                current[2].setdefault(current[4][0], []).append((None, kind))
    wanted = _source_names(meta, set(files))
    # {lexical block or subprogram: the names of the variables it declares} (#115).
    names = {}
    for kind, fields in meta.values():
        if kind == "DILocalVariable" and "name" in fields:
            block = _lexical_block(meta, fields.get("scope"))
            if block is not None:
                names.setdefault(block, []).append(fields["name"])
    # {subprogram: lines} of code LLVM inlined from each function into another, in the
    # inlined function's own file (#116).
    copies = {}
    for _, _, blocks, _, _, _ in raw:
        for refs in blocks.values():
            for ref, _ in refs:
                kind, location = meta.get(ref, (None, {}))
                if kind != "DILocation" or "inlinedAt" not in location:
                    continue
                subprogram = _subprogram(meta, location.get("scope"))
                number = int(location.get("line") or 0)
                if (
                    subprogram is not None
                    and number > 0
                    and _file(meta, location.get("scope")) == _file(meta, subprogram)
                ):
                    copies.setdefault(subprogram, set()).add(number)
    functions = {}
    for hashed, subprogram, blocks, preds, _, invokes in raw:
        kind, fields = meta.get(subprogram, (None, {}))
        if hashed is None or kind != "DISubprogram":
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        lines, events, inlined, sites, provenance = {}, {}, {}, {}, {}
        # #115: the named blocks with code (an inlined call's site counts), those with
        # code of each line, and each label's sure instructions and calls.
        active, line_blocks, sure, calls = set(), {}, {}, {}
        for label, refs in blocks.items():
            lines[label] = set()
            found_events = ([], [])
            found_sure, before = [], 0
            # (instruction, its line in the file or None, whether where it is is known)
            # for each instruction, in order (#230 review).
            placed = provenance.setdefault(label, [])
            for ref, instruction in refs:
                kind, location = meta.get(ref, (None, {}))
                placed.append((instruction, *_placed_at(meta, location, file, kind)))
                if kind != "DILocation":
                    before += instruction == "c"
                    continue
                if "inlinedAt" in location:
                    site = _call_site(meta, location)
                    if site is not None:
                        active.add(_named_block(meta, names, site.get("scope")))
                    if site is not None and _file(meta, site.get("scope")) == file:
                        found = (int(site.get("line") or 0), int(site.get("column") or 0))
                        inlined.setdefault(found[0], set()).add(found[1])
                        if found[0] > 0:
                            sites.setdefault(label, set()).add(found[0])
                    continue
                calls_before = before
                before += instruction == "c"
                block = _named_block(meta, names, location.get("scope"))
                active.add(block)
                if _file(meta, location.get("scope")) != file or not location.get("line"):
                    continue
                number = int(location["line"])
                if number > 0:
                    lines[label].add(number)
                    line_blocks.setdefault(number, set()).add(block)
                    if instruction in ("c", "B", "s"):
                        found_sure.append((number, block, calls_before))
                slot = {"c": 0, "b": 1, "B": 1}.get(instruction)
                if slot is not None:
                    found_events[slot].append((number, int(location.get("column") or 0)))
            events[label] = (tuple(found_events[0]), tuple(found_events[1]))
            sure[label], calls[label] = found_sure, before
        chain = _chains(meta, names, active)
        parent = meta.get(fields.get("scope"), (None, {}))[1].get("name")
        functions[hashed] = IrFunction(
            re.sub(r"<.*>$", "", fields.get("name", "")),
            parent,
            wanted[file],
            lines,
            preds,
            int(fields["line"]) if str(fields.get("line", "")).isdigit() else None,
            events,
            inlined,
            frozenset(copies.get(subprogram, ())),
            ancestors=_namespaces(meta, fields.get("scope")),
            generics=_generics(meta, fields),
            instance=_instance(fields.get("name", "")),
            chains=_count(chain[block] for block in chain) if chain else {},
            line_chains=_count(
                (number, chain[block]) for number, found in line_blocks.items() for block in found
            )
            if chain
            else {},
            sure={
                label: tuple((number, chain[block], at) for number, block, at in found)
                for label, found in sure.items()
            }
            if chain
            else {},
            calls=calls,
            scoped=chain is not None,
            sites=sites,
            provenance={label: tuple(found) for label, found in provenance.items()},
            invokes=frozenset(invokes),
        )
    # A function LLVM inlined everywhere (`#[inline(always)]`, even at opt-level 0) has
    # no IR function, yet its frames carry its own hash; its MIR body is the one before
    # inlining.
    for subprogram, lines in copies.items():
        kind, fields = meta.get(subprogram, (None, {}))
        hashed = _LEGACY_HASH.search(fields.get("linkageName") or "")
        if kind != "DISubprogram" or hashed is None or hashed[1] in functions:
            continue
        file = _file(meta, subprogram)
        if file not in wanted:
            continue
        functions[hashed[1]] = IrFunction(
            re.sub(r"<.*>$", "", fields.get("name", "")),
            meta.get(fields.get("scope"), (None, {}))[1].get("name"),
            wanted[file],
            {},
            {},
            int(fields["line"]) if str(fields.get("line", "")).isdigit() else None,
            copies=frozenset(lines),
            inline_only=True,
            ancestors=_namespaces(meta, fields.get("scope")),
            generics=_generics(meta, fields),
            instance=_instance(fields.get("name", "")),
        )
    return functions


def _instance(name):
    """A DISubprogram's name where it gives generic arguments (`tf<rc::A>`), else "";
    one past 300 characters is cut and ends with `…`."""
    if not isinstance(name, str) or not name.endswith(">"):
        return ""
    return name if len(name) <= 300 else name[:299] + "…"


# A const generic's value as rustc names an instantiation (`sum<3>`, `cgt<u8, 2>`).
# A char's is a hash (`ch<{CONST#27d96aba666aa2f7}>` for `ch::<'a'>`, rustc 1.96).
_CONST_ARGUMENT = re.compile(r"-?[0-9]+|true|false|\{CONST#[0-9a-f]+\}")


def _generics(meta, fields):
    """A DISubprogram's type parameters, by name (see IrFunction.generics).

    rustc lists each type parameter (its impl's and enclosing function's too) and never
    a const one, and its name gives every argument (`cgt<u8, 2>`) but no lifetime: the
    arguments beyond the listed types must all be const values, which are no types.
    """
    kind, listed = meta.get(fields.get("templateParams"), (None, {}))
    items = listed.get("items", []) if kind == "list" else None
    named = fields.get("name", "")
    arguments = _name_arguments(named)
    if items is None:
        # No list read: generic only if its name says so (`show<u64>`), or if the name
        # cannot be read.
        return () if arguments == [] else None
    names = []
    for item in items:
        kind, parameter = meta.get(item, (None, {}))
        if kind != "DITemplateTypeParameter" or not parameter.get("name"):
            return None
        names.append(parameter["name"])
    if arguments is None:
        return None
    consts = sum(1 for argument in arguments if _CONST_ARGUMENT.fullmatch(argument))
    if len(arguments) - len(names) != consts and arguments:
        return None
    return tuple(names)


def _name_arguments(name):
    """The generic arguments a DISubprogram's name ends with (`cgt<u8, 2>`: ["u8", "2"]),
    [] for none, None if they cannot be read."""
    if not name.endswith(">"):
        return []
    # `->` would close a bracket (`show<fn(u8) -> u8>`).
    text, depth = name.replace("->", "  "), 0
    for at in range(len(text) - 1, -1, -1):
        depth += {">": 1, "<": -1}.get(text[at], 0)
        if depth == 0:
            inner = name[at + 1 : -1]
            break
    else:
        return None
    parts, depth, start = [], 0, 0
    masked = inner.replace("->", "  ")
    for at, char in enumerate(masked):
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(inner[start:at].strip())
            start = at + 1
    parts.append(inner[start:].strip())
    return None if depth or not all(parts) else parts


def mentions(kind, names):
    """Whether the MIR type ``kind`` names any of ``names`` (type parameters) as a type."""
    return any(
        re.search(rf"(?<![\w:]){re.escape(name)}(?![\w])", str(kind or "")) for name in names
    )


def _namespaces(meta, scope):
    """The names of the namespaces ``scope`` is in, outermost first."""
    found = []
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind not in ("DINamespace", "DICompositeType") or "name" not in fields:
            break
        found.append(fields["name"])
        scope = fields.get("scope")
    return tuple(reversed(found))


def _subprogram(meta, scope):
    """The DISubprogram a scope (a lexical block in it) belongs to, if any."""
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind == "DISubprogram":
            return scope
        if kind not in ("DILexicalBlock", "DILexicalBlockFile"):
            return None
        scope = fields.get("scope")
    return None


def _lexical_block(meta, scope):
    """The DILexicalBlock or DISubprogram ``scope`` is (a DILexicalBlockFile only gives
    its block another file), or None."""
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind in ("DILexicalBlock", "DISubprogram"):
            return scope
        if kind != "DILexicalBlockFile":
            return None
        scope = fields.get("scope")
    return None


def _named_block(meta, names, scope):
    """The block DWARF puts code of ``scope`` in: LLVM emits no DWARF block for a lexical
    block that declares no variable, so its code is its parent's."""
    for _ in range(4096):
        block = _lexical_block(meta, scope)
        kind, fields = meta.get(block, (None, {}))
        if kind != "DILexicalBlock" or names.get(block):
            return block
        scope = fields.get("scope")
    return None


def _chains(meta, names, blocks):
    """{block: its chain}: the sorted names of the variables it and each block around it
    declare, out to its function's (`StopBlock.chain`), for ``blocks`` and the blocks
    around them; None if one is not a function's lexical block."""
    found = {}
    for block in blocks:
        path = []
        for _ in range(4096):
            kind, fields = meta.get(block, (None, {}))
            if kind not in ("DILexicalBlock", "DISubprogram"):
                return None
            path.append(block)
            if kind == "DISubprogram":
                break
            block = _named_block(meta, names, fields.get("scope"))
        else:
            return None
        chain = ()
        for block in reversed(path):
            chain = (tuple(sorted(names.get(block, ()))), *chain)
            found[block] = chain
    return found


def _count(items):
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _call_site(meta, location):
    """The DILocation in the function itself that inlined code at ``location`` was called
    from: the last of its `inlinedAt` chain."""
    for _ in range(256):
        parent = location.get("inlinedAt")
        if parent is None:
            return location
        kind, location = meta.get(parent, (None, {}))
        if kind != "DILocation":
            return None
    return None


def _placed_at(meta, location, file, kind):
    """(line, known) of an instruction at ``location`` (of metadata ``kind``) in the
    function of ``file``: the line of the file it is on (an inlined call's site line for
    code inlined there; None for another file's), and whether that is known. Not known:
    no location, line 0, a scope naming no file, an `inlinedAt` chain that does not
    resolve (#230 review)."""
    if kind != "DILocation":
        return None, False
    if "inlinedAt" in location:
        location = _call_site(meta, location)
        if location is None:
            return None, False
    number = int(location.get("line") or 0)
    where = _file(meta, location.get("scope"))
    if number <= 0 or where is None:
        return None, False
    return (number if where == file else None), True


def _file(meta, scope):
    """The file a scope's code is in: its DIFile's directory and name as one normalized
    path (`/crate/src/a/mod.rs`), so files of one name in two directories differ (#230
    review); None where the scope names none."""
    for _ in range(256):
        kind, fields = meta.get(scope, (None, {}))
        if kind is None:
            return None
        if kind in ("DISubprogram", "DILexicalBlock", "DILexicalBlockFile") and "file" in fields:
            return _file_path(meta, fields["file"])
        scope = fields.get("scope")
    return None


def _file_path(meta, reference):
    kind, fields = meta.get(reference, (None, {}))
    name = fields.get("filename") if kind == "DIFile" else None
    if not name:
        return None
    directory = fields.get("directory")
    return posixpath.normpath(
        name if name.startswith("/") or not directory else f"{directory}/{name}"
    )


def _source_names(meta, files):
    """{a DIFile path (_file): the program's relative source name it is}: each of
    ``files`` under the directory the build ran in, its compile units' (or, in IR with
    none, the directories of the DIFiles that name it)."""
    roots = {
        fields.get("directory")
        for kind, unit in meta.values()
        if kind == "DICompileUnit"
        for fields in (meta.get(unit.get("file"), (None, {}))[1],)
        if fields.get("directory")
    }
    if not roots:
        roots = {
            fields.get("directory") or ""
            for kind, fields in meta.values()
            if kind == "DIFile" and fields.get("filename") in files
        }
    return {
        posixpath.normpath(name if name.startswith("/") or not root else f"{root}/{name}"): name
        for root in roots
        for name in files
    }


class OwnedLocals:
    """Proofs that an owned local is initialized at a stop (see the module docstring)."""

    # Why the crate may have unsafe code (rust.unsafe_source; set by `prepare`): the
    # frozen-owner rule does not apply (#156 review).
    unsafe_code = None
    # The exact files std's printing macros are written in (rust.std_macro_files; set by
    # `prepare`): a call there that formats reads what it is given (#192 review 4).
    std_macros = frozenset()

    def __init__(self, bodies, functions, sources=(), *, precise=True, texts=None, defined=()):
        self.bodies = list(bodies)
        # The path of every function the crate's MIR dump holds (load_bodies).
        self.defined = frozenset(defined)
        # Statement-level positions rely on local macro code keeping its own lines in
        # debug info; `#[collapse_debuginfo]` changes that, and then whole blocks count.
        self.precise = precise
        self.functions = dict(functions)
        self.sources = tuple(sources)
        # {source: text}, to tell which lines can invoke a macro; absent means any can.
        self.texts = dict(texts or {})
        self._matched = {}
        self._unanalyzed = {}
        # What each stop's rows share (its places, its lexical block's scopes), by what
        # determines it: every row of a frame asks for the same (see ``_memo``).
        self._memos = {}
        # {a StopBlock's contents: its number}, never renumbered (a memo's key holds one),
        # and {id(StopBlock): (it, its number)}.
        self._block_tokens, self._block_ids = {}, {}
        # {(file, line): {body index}}: which bodies have a span on each line.
        self._owners = {}
        for index, body in enumerate(self.bodies):
            for number in body.lines:
                self._owners.setdefault((body.file, number), set()).add(index)

    def body(self, hashed):
        """The one MIR body the IR function ``hashed`` was generated from, else None."""
        if hashed in self._matched:
            return self._matched[hashed]
        function = self.functions.get(hashed)
        found = None
        # A function inlined everywhere has only the lines of its inlined copies (#116).
        lines = (function.copies if function.inline_only else function.lines()) if function else ()
        if lines:
            # A coroutine's debug info names its kind (`{async_fn#0}`, `{async_block#1}`);
            # its MIR names it as a closure, by the same disambiguator (#116).
            name = _mir_name(function.name)
            # Positive identification, never by elimination: exactly one body of this
            # name and file (and parent) has a span on the line the function is declared
            # on. Twins a local macro generates share that line (the `fn` is in the
            # macro), so they are refused rather than told apart by their other lines.
            candidates = [
                body
                for body in self.bodies
                if body.file == function.file
                and body.name == name
                and _same_path(body, function)
                and function.line in body.lines
            ]
            if len(candidates) == 1:
                chosen = next(i for i, b in enumerate(self.bodies) if b is candidates[0])
                # A line with this function's code that only other bodies have spans on
                # is theirs: the candidate is a twin whose `fn` line merely matches
                # (macro-generated, one with cleanup on that line, one without).
                foreign = any(
                    chosen not in owners
                    for number in lines
                    if (owners := self._owners.get((function.file, number)))
                )
                if foreign or not function.labeled() <= set(candidates[0].blocks):
                    # A twin: the one generated from it has spans on those lines.
                    candidates = [
                        *candidates,
                        *(
                            self.bodies[index]
                            for number in lines
                            for index in sorted(self._owners.get((function.file, number), ()))
                            if self.bodies[index].name == name
                        ),
                    ]
            elif not candidates and function.inline_only:
                # Not identified, and no IR describes its code either: its borrowing
                # bindings are those of the bodies it may be (#116).
                self._unanalyzed[hashed] = [
                    body for body in self.bodies if body.file == function.file and body.name == name
                ]
            found = candidates[0] if len(candidates) == 1 else None
            if found is not None and not (
                found.supported and found._dataflow() is not None
                if function.inline_only
                # A coroutine whose state is not understood can hold anything (#116).
                else (found.supported or found.coroutine is None) and _covered(found, function)
            ):
                # Identified, but its layout or dataflow is not understood.
                found.generics = function.generics
                self._unanalyzed[hashed] = [found]
                found = None
            elif len(candidates) > 1:
                # Twins (a macro's methods): one of them, not told apart.
                self._unanalyzed[hashed] = list({id(body): body for body in candidates}.values())
        if found is not None:
            found.generics = function.generics
        self._matched[hashed] = found
        return found

    def _visible(self, body, line, path, block=None, site=None, bindings=None):
        """The scopes a binding shown at ``line`` can be in, or None to keep them all.
        A line that may invoke a macro runs code whose spans are in another file
        (`eprintln!`'s, collapsed onto the line), in scopes no statement of this file
        there shows: the stop can be in one of those. Where the DWARF lexical block the
        stop's pc is in (``block``) is told apart, only it and its enclosing scopes
        (#115): by the stop line's places (of ``site``) where several scopes have its
        names, else every scope with them. A name none of whose ``bindings`` are there
        (lldb-dap shows none at the stop) is judged as without the block."""
        visible = self._line_visible(body, line, path)
        scopes = self._block_scopes(site, line, block) if block is not None else None
        if scopes is None:
            return visible
        narrowed = scopes if visible is None else (visible & scopes) or visible
        if bindings is not None and not any(b[4] in narrowed for b in bindings):
            return visible
        return narrowed

    def _block_scopes(self, site, line, block):
        if site is None:
            return None
        return self._memo(
            "scopes", site, line, None, block, lambda: self._scopes_of(site, line, block)
        )

    def _scopes_of(self, site, line, block):
        body, function, blocks, shared = site
        named = body.chain_scopes(block, function)
        if named is None:
            return None
        text = self.texts.get(function.file)
        if function.inline_only or not (self.precise or not may_invoke_macro(text, line)):
            return named
        scope = body.stop_scope(
            blocks, line, function.file, may_invoke_macro(text, line), block, shared, function
        )
        if scope not in named:
            return named
        found = set()
        for _ in range(4096):
            if scope is None:
                break
            found.add(scope)
            scope = body.scopes.get(scope)
        return found

    def _line_visible(self, body, line, path):
        if line is None:
            return None
        # The stop's own file: a macro defined in another module, or `include!`d code,
        # stops in this frame at another file's line.
        shown = str(path or "").replace("\\", "/")
        matched = [name for name in self.texts if shown == name or shown.endswith("/" + name)]
        if len(matched) != 1 or may_invoke_macro(self.texts[matched[0]], line):
            return None
        return body.visible_scopes(line, path)

    def _local(self, body, variable_name, line=None, path=None, block=None, site=None):
        name, at, declared = str(variable_name).partition(" @ ")
        where = None
        if at:
            file, _, number = declared.rpartition(":")
            if not number.isdigit():
                return None
            where = (file, int(number))
        bindings = [
            b
            for b in body.bindings.get(name, [])
            if where is None or (Path(b[1]).name, b[2]) == where
        ]
        return body.local(name, where, self._visible(body, line, path, block, site, bindings))

    def declared_type(self, hashed, variable_name, line=None, path=None):
        """The MIR type of the frame's local ``variable_name`` (`std::vec::Vec<usize>`),
        which LLDB's C-style names cannot tell apart (u64 and usize); else None. With the
        stop's ``line`` and ``path``, a name bound more than once is the binding in scope
        there, if only one is."""
        body = self.body(hashed)
        local = self._local(body, variable_name, line, path) if body is not None else None
        kind = None if local is None else body.types.get(local)
        # In a generic body any type may name a type parameter, whatever its name
        # (`fn show<i8>(p: Pair<i8>)` called with chars): only the others are this frame's.
        generics = self.type_parameters(hashed)
        return None if generics is None or mentions(kind, generics) else kind

    def type_parameters(self, hashed):
        """The type parameters of the frame's function (see IrFunction.generics): () for
        a function that has none, None if unknown."""
        function = self.functions.get(hashed)
        return None if function is None else function.generics

    def binding_types(self, hashed, variable_name, path=None, line=None, closure=None, frame=None):
        """The MIR type of every binding lldb-dap could be showing as ``variable_name``
        (None for one whose type is not written out), in the frame's body. A frame
        whose body is not matched for proofs (an unmangled function) takes its types
        from the bodies whose code covers the stop line, of the frame's own shape when
        one is (``frame``, lldb-dap's name: `outer::{{closure}}::{{closure}}` is the
        MIR's `outer::{closure#0}::{closure#0}`). Empty when no body binds the name."""
        name, at, declared = str(variable_name).partition(" @ ")
        body = self.body(hashed)
        shown = str(path or "").replace("\\", "/")
        bodies = [body] if body is not None else []
        if body is None:

            def covers(candidate):
                # A span covering the line counts, not only one starting there (`K\n.get()`
                # has none starting on `.get()`'s line, while a nested item may); and in
                # the stop's own file, which a `macro_rules!` from another module puts
                # in a body of this file.
                if line in candidate.lines and shown.endswith("/" + str(candidate.file)):
                    return True
                return any(
                    number == line and (shown == file or shown.endswith("/" + file))
                    for file, number in candidate.line_scopes
                )

            bodies = [
                candidate
                for candidate in self.bodies
                if covers(candidate)
                # A closure defined on the line is a body of its own, with its own
                # captures of the frame's names: only bodies of the frame's kind count.
                and (closure is None or candidate.name.startswith("{closure") == closure)
            ]
            # An outer async body's spans cover its nested `async { }` block's lines too
            # (the coroutine's construction): the frame's own shape picks among them.
            wanted = frame_shape(frame) if frame else None
            shaped = [c for c in bodies if wanted and _shape(_segments(c.path)) == wanted]
            bodies = shaped or bodies
        found = []
        for candidate in bodies:
            # Only the bindings in scope on the stop line: a name `println!` or a sibling
            # block binds (`args`, `q`) is not the one lldb-dap shows.
            visible = self._visible(candidate, line, path)
            bindings = candidate.in_scopes(candidate.bindings.get(name, ()), visible)
            for local, file, number, projected, _ in bindings:
                if at and f"{Path(file).name}:{number}" != declared:
                    continue
                found.append(candidate.types.get(local) if local is not None else projected)
        return found

    def proven(self, hashed, variable_name, line, path):
        """True only if the frame's local ``variable_name`` (an lldb-dap name: `v`, or
        `v @ main.rs:8` when shadowed) is initialized at ``line`` of ``path``.

        The stop must be in the function's own file: a breakpoint in a macro defined in
        another file (or `include!`d code) stops in this frame at another file's line.
        """
        site = self._site(hashed, line, path)
        if site is None:
            return False
        local = self._local(site[0], variable_name, line, path)
        return local is not None and self._initialized(site, local, line)

    def copied_in_progress(
        self,
        hashed,
        variable_name,
        line,
        path,
        breakpoint=False,
        column=None,
        block=None,
        projection=(),
    ):
        """For the frame's local ``variable_name``: False if, from every place the stop
        can be on ``line`` of ``path``, no temporary that may hold a value read from it
        (``Body.copies_at``) is used again: a statement under way has not read it yet,
        so a change made here reaches what the program reads of it from now on. True if
        one may be (`let t = s.a + f();` back from `f`: `s.a` is in a temporary); None if
        not understood. ``breakpoint``, ``column`` and ``block`` as for
        ``reference_valid``: a line breakpoint (or a step that ends at its line's lowest
        address) stops before the line's first code in its lexical block; any other
        stop can be at any place of the line. Each temporary as ``Body.copies_at``
        finds it: one set before the local took its current value holds an earlier one."""
        found = self.copy_hazards(
            hashed, variable_name, line, path, breakpoint, column, block, projection
        )
        return None if found is None else any(hazards for _, hazards in found)

    def copy_hazards(
        self,
        hashed,
        variable_name,
        line,
        path,
        breakpoint=False,
        column=None,
        block=None,
        projection=(),
        named=True,
        reads="any",
    ):
        """``copied_in_progress``'s finding: [(a place the stop can be, the temporaries
        that may hold a value read from the local's current value and are used again
        from there)], or None if not understood (#183: the refusal names where those
        temporaries were made). ``projection``: the part of the local a change makes
        (``_root_access``; () for all of it, None where not known), which a binding's
        copy is judged by (#192 review); without ``named``, those copies are left out
        (what holds for every part of an aggregate, whose parts are judged each). The
        standard library's `ptr::eq` reads no pointee (#300, ``_std_compares``).
        ``reads``: which pointers count ("any", "traced", "value": ``_copy_verdict``; and
        "whole", as "value" for the whole local), or what a refusal may say of each (#300):
        "explicit", the temporaries that surely read it; "taken", what a binding's copy of
        it surely gave (``Body.read_copies_at``), and "sources", not hazards but the
        bindings those surely came from; "copied", what may hold such a copy, of "any";
        "bindings", not hazards but the bindings that may hold a direct copy."""
        site = self._site(hashed, line, path)
        if site is None:
            return None
        body, function = site[0], site[1]
        if not body.supported:
            return None
        local = self._local(body, variable_name, line, path, block, site)
        if local is None:
            return None
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        if breakpoint and (facts is None or facts.column is None):
            # The line's first place by the IR's order may emit no code (`&k` in `let t =
            # (&k,` / `s.a,` / `s.b);`, whose first code is the tuple's, after `s.a` is
            # read): only the stop's column tells the places it can be before apart.
            return None
        positions = self._positions(site, line, facts, block)
        if not positions:
            return None
        if breakpoint and not function.inline_only:
            positions = _first_places(body, function, positions)
        formatting, bindings = self._formatting(), body.named_locals()
        compares, transfers = self._std_compares(), self._std_transfers()
        # "whole": as "value", for a change to the whole local (#300: another part read);
        # "copied", "bindings": of "any"'s flow.
        flow = {"whole": "value", "copied": "any", "bindings": "any"}.get(reads, reads)
        part = projection if reads == "value" else ()
        # What a call std's printing makes reads (#192 review 4: by its file and path).
        sinks = (frozenset(self.std_macros), frozenset(each.path for each in self.bodies))

        def hazards(position):
            # A temporary that read the local, used again; a binding of the program's own
            # holding a direct copy (or a temporary taken from one) the program goes on
            # with, never reading the local (#192: `n` of `x = match x { n => … n }`).
            live = body.live_ahead(position)
            if reads in ("explicit", "taken", "sources"):
                sure = body.read_copies_at(local, position, projection, transfers)
                if sure is None:
                    return None
                read, taken, origins = sure
                if reads == "explicit":
                    return frozenset(n for n in read if live >> n & 1)
                kept = frozenset(
                    n
                    for n in (taken - read if named else ())
                    if body.copy_undoes(n, local, position, projection, sinks)
                )
                if reads == "taken":
                    return kept
                return (kept & bindings) | {binding for made, binding in origins if made in kept}
            direct = body.direct_copies_at(local, position, formatting, projection, compares, flow)
            if direct is None:
                return None
            if reads == "bindings":
                return direct & bindings
            temporaries = body.copies_at(local, position, formatting, compares, flow, part)
            temporaries -= bindings
            undone = frozenset(
                n
                for n in (direct - temporaries if named else ())
                if body.copy_undoes(n, local, position, projection, sinks)
            )
            if reads == "copied":
                return undone
            return frozenset(n for n in temporaries if live >> n & 1) | undone

        found = [(position, hazards(position)) for position in positions]
        return None if any(each is None for _, each in found) else found

    def copy_lines(self, hashed, temporaries, named_only=False):
        """The source lines where the code assigns any of ``temporaries`` in the body of
        ``hashed`` ({(file, line)}), for a refusal to name (#183); with ``named_only``,
        only the program's own bindings among them (#192), where they are not assigned a
        constant (#300: `week = 0` after its copy is no copy; #311: nor `b[0] = 7;`)."""
        body = self.body(hashed)
        if body is None:
            return set()
        if named_only:
            temporaries = frozenset(temporaries) & body.named_locals()
        parts = body._copy_parts()[0]
        found = set()
        for (number, index), part in body._copy_places():
            text = body.blocks[number].statements[index][0]
            constant = _CONSTANT_STORE.match(text) or _CONSTANT_THROUGH.match(text)
            if parts[part][0] in temporaries and not (named_only and constant):
                _, file, number_line = body.blocks[number].statements[index]
                if file is not None and number_line is not None:
                    found.add((file, number_line))
        return found

    def copy_sources(self, hashed, temporaries, among):
        """The bindings of ``among`` that ``temporaries`` are computed from, walking back
        through what each assignment to them mentions to the program's own bindings
        (#300: a binding's copy a refusal names, `week` of `let week = weeks;` for
        `same(week + 1)`); a binding among ``temporaries`` is its own."""
        body = self.body(hashed)
        if body is None:
            return frozenset()
        parts, named = body._copy_parts()[0], body._copy_parts()[4]
        made = {}
        for _, part in body._copy_places():
            made.setdefault(parts[part][0], []).append(parts[part])
        found, seen, pending = set(), set(), list(temporaries)
        while pending:
            local = pending.pop()
            if local in seen:
                continue
            seen.add(local)
            if local in named:
                if local in among:
                    found.add(local)
                continue
            for part in made.get(local, ()):
                pending.extend(part[1])
        return frozenset(found)

    def untraced_reads(self, hashed, temporaries):
        """Where ``temporaries`` (``copy_hazards``'s, only with ``reads="any"``) may have
        read the local through a pointer Peeky does not follow (#300): ({(file, line)} of
        each read through a pointer, or use of a variable whose type holds one, they are
        made from; [(the program's own name of each such variable, what it is:
        "reference", "raw pointer", "closure", "coroutine", or None for another value
        holding a pointer)], sorted)."""
        body = self.body(hashed)
        if body is None:
            return set(), []
        parts, named = body._copy_parts()[0], body._copy_parts()[4]
        made = {}
        for place, part in body._copy_places():
            made.setdefault(parts[part][0], []).append((place, parts[part]))
        lines, variables, seen, pending = set(), set(), set(), list(temporaries)
        while pending:
            local = pending.pop()
            if local in seen:
                continue
            seen.add(local)
            for (number, index), part in made.get(local, ()):
                _, mentioned, through, _, _, _, pointers, _ = part
                if through or pointers:
                    _, file, number_line = body.blocks[number].statements[index]
                    if file is not None and number_line is not None:
                        lines.add((file, number_line))
                    variables |= pointers
                pending.extend(n for n in mentioned if n not in named)
        kinds = {}
        for name, bindings in body.bindings.items():
            for binding in bindings:
                if binding[0] is not None and binding[0] in variables:
                    kinds.setdefault(name, set()).add(_pointer_kind(body.types.get(binding[0])))
        return lines, sorted(
            (name, found.pop() if len(found) == 1 else None) for name, found in kinds.items()
        )

    def pointer_bindings(self, hashed, locals_):
        """Those of ``locals_`` that are the program's own references or raw pointers in the
        body of ``hashed`` (#311 review: `p` of `*p = x;`, which stands for what it points
        at, where nothing else does)."""
        body = self.body(hashed)
        if body is None:
            return frozenset()
        return frozenset(
            local
            for local in locals_
            if local in body.named_locals() and _POINTER_TYPE.match(body.types.get(local, ""))
        )

    def copy_names(self, hashed, locals_):
        """The program's own names (sorted) of those of ``locals_`` a binding names in the
        body of ``hashed`` (#192: `n`, where `x = match x { n => … }` copied `x`)."""
        body = self.body(hashed)
        if body is None:
            return []
        return sorted(
            {
                name
                for name, bindings in body.bindings.items()
                for binding in bindings
                if binding[0] is not None and binding[0] in locals_
            }
        )

    def coded_hazards(self, hashed, variable_name, line, path, projection=(), reads="any"):
        """``copy_hazards`` at every place of ``line`` a stop can be at (``reads`` and
        ``projection`` as there), or None if not understood: a stop is never before a
        statement that emits no code (`StorageDead(_6)` after the array it ended was
        built), whose address is the next one's."""
        found = self.copy_hazards(
            hashed, variable_name, line, path, projection=projection, reads=reads
        )
        body = self.body(hashed)
        if found is None or body is None:
            return None
        coded = [
            (position, hazards)
            for position, hazards in found
            if not _NO_CODE.match(body.blocks[position[0]].statements[position[1]][0])
        ]
        return coded or found

    def copied_everywhere(self, hashed, variable_name, line, path, projection=()):
        """The temporaries (a frozenset) that hold a value read from the frame's local
        ``variable_name`` at every place of ``line`` and are used again from each, so no
        stop on the line can change it for the statement under way (#183: refused before
        the program runs); None where some place has none, or that is not understood.
        ``projection`` as for ``copy_hazards``."""
        found = self.coded_hazards(hashed, variable_name, line, path, projection)
        if not found or not all(hazards for _, hazards in found):
            return None
        common = frozenset.intersection(*(hazards for _, hazards in found))
        # What every place shares, or what each place has where they share nothing.
        return common or frozenset().union(*(hazards for _, hazards in found))

    def borrow_live(
        self,
        hashed,
        variable_name,
        line,
        path,
        breakpoint=False,
        column=None,
        block=None,
        free_types=frozenset(),
    ):
        """For the frame's owned local ``variable_name`` (a String a text change writes,
        #164): False if, from every place the stop can be on ``line`` of ``path``, no
        local that may hold a pointer into it (``Body.lent_from``: a `&str` of it, an
        iterator over it, anything built from one) is used again; True if one may be;
        None if not understood. A borrow still in use would read the new bytes with the
        old length: a mix of texts, or bytes that are not UTF-8. ``breakpoint``,
        ``column`` and ``block`` as for ``copied_in_progress``."""
        site = self._site(hashed, line, path)
        if site is None:
            return None
        body, function = site[0], site[1]
        if not body.supported:
            return None
        local = self._local(body, variable_name, line, path, block, site)
        if local is None:
            return None
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        if breakpoint and (facts is None or facts.column is None):
            breakpoint, facts = False, None
        positions = self._positions(site, line, facts, block)
        if not positions:
            return None
        if breakpoint and not function.inline_only:
            positions = _first_places(body, function, positions)
        generics = self.type_parameters(hashed)
        found = body.lent_from(local, generics, free_types)
        lent = _mask(found)
        # A drop uses what it drops where its code may read a borrow (#164: a guard's
        # `Drop`; not a `Vec<&str>`'s), unless what it would drop was moved out before the
        # stop (`drop(b);` then the scope's end).
        dropped = _mask(n for n in found if not _quiet_drop(body.types.get(n), generics))
        return any(
            body.live_ahead(position) & lent
            or body.live_ahead(position, drops=True) & body.maybe_initialized(position) & dropped
            for position in positions
        )

    def _formatting(self):
        """Whether a call that reads like `format_args!`'s constructors (and returns
        their type) is theirs (#105): the crate names nothing `core` or `std` but the
        standard library's paths (`core::fmt::...`), in every source's code, and has no
        MIR body under either (its own `mod core { pub mod fmt { ... } }` prints just
        like the real one). Unknown text counts as naming one."""
        if "_formatting_known" not in self.__dict__:
            from peeky.rust_lexer import code_only

            own = re.compile(r"(?<![\w:])(?:r#)?(?:core|std)\b(?!\s*::)")
            codes = [
                code_only(self.texts[source]) if source in self.texts else None
                for source in self.sources
            ]
            self._formatting_known = (
                bool(codes)
                and all(code is not None and not own.search(code) for code in codes)
                and not any(
                    str(body.path).startswith(("core::", "std::", "r#core::", "r#std::"))
                    for body in self.bodies
                )
            )
        return self._formatting_known

    def reference_valid(
        self,
        hashed,
        variable_name,
        line,
        path,
        breakpoint=False,
        column=None,
        block=None,
        why=None,
    ):
        """For the frame's local ``variable_name`` whose type borrows (a reference, `&str`,
        a tuple, `Vec` or closure holding one, a raw pointer): True if what it points into
        is shown to stand at ``line`` of ``path``, False if not (#110), None if this does
        not apply (another type, the site or the local not understood). ``breakpoint``:
        the stop is a line breakpoint's, at ``column`` (lldb-dap's, in bytes) if known.

        True where, from every place the stop can be on the line, the code still uses it
        ahead, or used it last with nothing since that can move, drop or write (no call,
        no drop; a call of `std::ptr::eq` or `addr_eq`, which only compares addresses, is
        none, #259): the borrow checker keeps what it borrows alive and unchanged until its
        last use. Past a call or drop after its last use, what it pointed into may be
        moved, dropped or reallocated, which no simpler reading of the MIR can rule out
        (interior mutability, `&mut` aliases, loops), so then it is False. Also True: a
        reference only ever made into a shared reference argument (the caller's loan
        freezes it for the call) or into a variable whose scope encloses its own and
        that is initialized at the stop (it points at that variable's storage), a fn
        pointer, a reference argument never reassigned (its target outlives the call),
        and a local only ever assigned constants (`"text"`, a promoted `&[1, 2]`); a
        local never borrowed itself, for the last two. A raw pointer keeps no borrow, so
        it is False (a tuple holding one is judged as a tuple, and the raw pointer in it
        shows only its address, #255). A value not yet assigned (its declaration line)
        is False. A name bound more than once that the stop cannot tell apart is judged
        by every binding. ``block`` (a ``StopBlock``): the
        DWARF lexical block the stop's pc is in, if known (#115). Where False, ``why`` (a
        list, if given) gets the reason (#259): ``_judge``'s, ("elsewhere",) for a stop
        the function's own lines cannot place, or ("captured",) for a capture whose
        closure or future may be dropped."""
        site = self._site(hashed, line, path)
        if site is None:
            verdict = self._elsewhere(hashed, variable_name, path)
            return _false(why, ("elsewhere",)) if verdict is False else verdict
        body, function = site[0], site[1]
        local = self._local(body, variable_name, line, path, block, site)
        captures = []
        if local is not None:
            candidates = [local]
        else:
            name, at, declared = str(variable_name).partition(" @ ")
            bindings = body.bindings.get(name, [])
            if at:
                file, _, number = declared.rpartition(":")
                bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
            bindings = body.in_scopes(
                bindings, self._visible(body, line, path, block, site, bindings)
            )
            candidates = [b[0] for b in bindings]
            # A capture (`(*((*_1).0: &mut Holder))`) has no local of its own: what it
            # points at is dropped through the environment it is reached by. A value a
            # coroutine's state holds itself (`((*_34).1: i64)`, an `async fn`'s
            # parameter) points nowhere unless its type borrows (#116).
            captures = [
                int(root[1])
                for b in bindings
                if b[0] is None
                and (place := body.places.get((name, b[1], b[2], b[4]), ""))
                and (
                    body.coroutine is None
                    or place.startswith("(*(")
                    or _BORROWING.search(b[3] or "&")
                )
                and (root := _ANY_LOCAL.search(place))
            ]
        # A breakpoint's place in the line comes from the IR function's layout, which an
        # inlined copy of it (#116) need not share: then any place of the line counts.
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        if captures:
            positions = self._positions(site, line, facts, block) or ()
            if any(
                body.dropped_through(root, position) for root in captures for position in positions
            ):
                return _false(why, ("captured",))
        verdicts = []
        for candidate in candidates:
            verdicts.append(self._judge(site, candidate, line, facts, block, why))
            if verdicts[-1] is False:
                return False
        if verdicts and all(verdict is True for verdict in verdicts):
            return True
        return None

    def lent_copies(
        self, hashed, variable_name, line, path, breakpoint=False, column=None, block=None
    ):
        """For a name an `async fn`'s body binds twice to one parameter (#116): the
        field its state keeps it in (`debug v => ((*_34).0: &Pt)`), which holds what the
        caller lent for as long as the future lives, and the poll's own copy of it (`_3 =
        copy ((*_34).0: &Pt)`), which a later poll leaves unassigned. lldb-dap shows both
        under one name, and ``reference_valid`` is False when the copy is not proven. Then
        (bytes each holds, how many bindings): where every row of the name holds the same
        bytes, each shows what the caller lent. None where they are not just that."""
        site = self._site(hashed, line, path)
        if site is None or site[0].coroutine is None:
            return None
        body, function = site[0], site[1]
        name, at, declared = str(variable_name).partition(" @ ")
        bindings = body.bindings.get(name, [])
        if at:
            file, _, number = declared.rpartition(":")
            bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
        bindings = body.in_scopes(bindings, self._visible(body, line, path, block, site, bindings))
        fields = [b for b in bindings if b[0] is None]
        copies = [b[0] for b in bindings if b[0] is not None]
        if len(fields) != 1 or not copies:
            return None
        place = body.places.get((name, *fields[0][1:3], fields[0][4]), "")
        found = re.fullmatch(rf"\(\(\*_{body.coroutine}\)\.([0-9]+): (&.+)\)", place)
        lent = body._lent()
        if found is None or int(found[1]) in body._written_fields or _RAW_POINTER.search(found[2]):
            return None
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        positions = self._positions(site, line, facts, block)
        if not positions or any(
            body.dropped_through(local, position)
            for local in (body.coroutine, *copies)
            for position in positions
        ):
            return None
        for local in copies:
            values = body._assignments().get(local, [])
            if (
                local not in lent
                or body.types.get(local) != found[2]
                or local in body._written_through()
                or any(value not in (f"copy {place}", f"move {place}") for value in values)
            ):
                return None
        wide = re.match(r"&(?:mut )?(?:\[|str$|dyn )", found[2])
        return (16 if wide else 8), len(bindings)

    def unassigned(
        self,
        hashed,
        variable_name,
        line,
        path,
        breakpoint=False,
        column=None,
        caller=False,
        block=None,
        shown=None,
    ):
        """#132: which of the rows lldb-dap shows as the frame's ``variable_name`` hold
        no value of their own at ``line`` of ``path``: (how many rows the name has,
        {row: why}), rows in lldb-dap's order, or None where none is told (or nothing
        can be: the site, the binding or the body not understood). For a type that does
        not borrow: a reference's are #110's (an unassigned one is not read through).

        A row is the frame's own local where some path to a place the stop can be on
        the line (``breakpoint``, ``column``, ``block``: see reference_valid), of those
        where lldb-dap can show it and every row of the frame (``shown``: their names;
        see ``Body.scopes_at``), leaves it unassigned since its
        storage began (``Body._assigned_flow``), as rustc's own initialization check
        would say there. A caller frame (``caller``) is inside a call or drop of the
        line: the places before those count (every place of the line must, where none
        is told). ``why`` is "suspended" where only a poll resuming the body after a
        suspension (an `.await`) leaves it so, since the state keeps no copy of it (the
        body does not use it again), else "unassigned" (a `let` without a value, a
        loop's next pass before its `let`).

        An `async fn`'s parameter is bound twice (#116): the state's field, in the
        function's own scope, whose variables lldb-dap lists before any lexical
        block's, and the poll's own copy (`_3 = copy ((*_29).0: u32)`) in a scope
        within it, row 1 where both are shown. The field (row 0) is "passed" where a
        reassignment of the copy (`mut n`) can have run before the stop: it holds what
        the caller passed, not the parameter's value since."""
        site = self._site(hashed, line, path)
        if site is None or not site[0].supported or site[0]._assigned_flow() is None:
            return None
        body, function = site[0], site[1]
        name, at, declared = str(variable_name).partition(" @ ")
        bindings = body.bindings.get(name, [])
        if at:
            file, _, number = declared.rpartition(":")
            bindings = [b for b in bindings if Path(b[1]).name == file and str(b[2]) == number]
        bindings = body.in_scopes(bindings, self._visible(body, line, path, block, site, bindings))
        pair = self._parameter_copy(body, name, bindings)
        if pair is not None:
            place, (local, *_, scope) = pair
            row = 1 if len(bindings) == 2 else None
        elif len(bindings) == 1 and bindings[0][0] is not None:
            row, (local, *_, scope) = 0, bindings[0]
        else:
            return None
        if _BORROWING.search(body.types.get(local) or "&"):
            return None
        facts = self._facts(function, line, column) if breakpoint and not function.copies else None
        foreign = may_invoke_macro(self.texts.get(function.file), line)
        positions = self._positions(site, line, facts, block) or ()
        found = {}
        if pair is not None and body.reassigned_before(local, place, positions):
            found[0] = "passed"

        def placeable():
            # Every row lldb-dap shows has the pc in its binding's lexical block: in one of
            # the scopes each shown name is bound in (``shown``: the frame's row names).
            within = []
            for other in set(shown or ()):
                bound, near, where = str(other).partition(" @ ")
                scopes = {
                    b[4]
                    for b in body.bindings.get(bound, ())
                    if not near or f"{Path(b[1]).name}:{b[2]}" == where
                }
                if scopes:
                    within.append(scopes)
            found = {}
            for position in positions:
                scopes = body.scopes_at(position, line, function.file, foreign, site[2])
                if scopes is None or all(s & scopes for s in within):
                    callers = body.returned_into(position[0], line, function.file, foreign, site[2])
                    found[position] = (scopes, callers if position[1] == 0 else None)
            return found

        # What every row of the frame shares: the places the rows put the pc in.
        places = self._memo(("places", frozenset(shown or ())), site, line, facts, block, placeable)

        def judge(position, resumed=True):
            callers = places[position][1]
            if callers:
                return body.unassigned_after(local, position[0], callers, resumed)
            return body.unassigned_at(local, position, resumed)

        verdicts = {
            position: verdict
            for position, (scopes, _) in (places.items() if row is not None else ())
            if (scopes is None or scope in scopes) and (verdict := judge(position)) is not None
        }
        if caller:
            # Inside one of the line's calls (or drops): before it, if they are told.
            calls = {
                (number, index): verdict
                for (number, index), verdict in verdicts.items()
                if index == len(body.blocks[number].statements) - 1
                and (
                    _call(body.blocks[number], index) or _DROP.match(body.blocks[number].terminator)
                )
                and body.line_code(number, index, line, function.file, foreign)
            }
            # Else every place of the line must leave it unassigned.
            verdicts = calls or (verdicts if all(verdicts.values()) else {})
        unassigned = [position for position, verdict in verdicts.items() if verdict]
        if unassigned:
            first = body.resumes and body._assigned_flow(resumed=False) is not None
            # On the first poll's paths every one of these places has it assigned (or
            # none reaches them): only a resumed poll leaves it so.
            found[row] = (
                "suspended"
                if first and all(judge(position, False) is not True for position in unassigned)
                else "unassigned"
            )
        return (len(bindings), found) if found else None

    @staticmethod
    def _parameter_copy(body, name, bindings):
        """(the field's place, the copy's binding) where ``bindings`` (of ``name``, as
        the stop shows it) are an `async fn`'s parameter: the state's field (`debug n =>
        ((*_29).0: u32)`) in the function's own scope, and, if shown, the one whole local
        declared with it in a scope within it, which the body sets from the field: the
        poll's own copy."""
        fields = [b for b in bindings if b[0] is None]
        if body.coroutine is None or len(fields) != 1 or len(bindings) > 2 or fields[0][4] != 0:
            return None
        field = fields[0]
        place = body.places.get((name, *field[1:3], field[4]), "")
        if not re.fullmatch(rf"\(\(\*_{body.coroutine}\)\.[0-9]+: .+\)", place):
            return None
        copies = [
            b
            for b in body.bindings.get(name, [])
            if b[0] is not None and b[1:3] == field[1:3] and b[4] != 0
        ]
        if len(copies) != 1 or any(b is not field and b != copies[0] for b in bindings):
            return None
        values = body._assignments().get(copies[0][0], [])
        return (
            (place, copies[0]) if f"copy {place}" in values or f"move {place}" in values else None
        )

    def _elsewhere(self, hashed, variable_name, path):
        """For a stop no line of the function's own can place: in another of the crate's
        files than its function's (a `macro_rules!` from another module, `include!`d
        code), at a line its IR does not explain, or anywhere in a function whose body
        was identified but not analyzed (a dataflow past its budget) or not told apart
        from its twins (a macro's methods). False if a binding of the name may borrow,
        else None. A reference parameter never reassigned, written through or dropped
        through (`*v = new`, `*cell.borrow_mut() = new`) is the caller's, so exempt."""
        body, function = self.body(hashed), self.functions.get(hashed)
        bodies = [body] if body is not None else self._unanalyzed.get(hashed, [])
        if not bodies or function is None:
            return None
        shown = str(path or "").replace("\\", "/")
        if not any(shown.endswith("/" + source) for source in self.sources):
            return None
        name = str(variable_name).partition(" @ ")[0]
        for body in bodies:
            for local, _, _, projected, _ in body.bindings.get(name, ()):
                kind = body.types.get(local) if local is not None else projected
                if not kind or not _BORROWING.search(kind) or _FN_POINTER.match(kind):
                    continue
                if (
                    local in body.arguments
                    and kind.startswith("&")
                    and not _RAW_POINTER.search(kind)
                    and local not in body._assignments()
                    and local not in body._written_through()
                    and not body.drops_through(local)
                ):
                    continue
                return False
        return None

    def _memo(self, kind, site, line, facts, block, compute):
        """``compute()``, once per stop: ``site`` is ``_site``'s for the function and
        ``line`` (its body and function live as long as this), and ``facts`` the
        breakpoint's (``_facts``, from them and its column) or None for any other stop:
        a breakpoint's facts without a column still differ from none."""
        column = None if facts is None else facts.column
        key = (kind, id(site[0]), id(site[1]), line, facts is not None, column)
        if block is not None:
            # A frame's rows pass one StopBlock: its contents are read (and numbered,
            # equal ones alike) once.
            known = self._block_ids.get(id(block))
            if known is None or known[0] is not block:
                content = (block.chain, tuple(sorted(block.chains.items())))
                known = (block, self._block_tokens.setdefault(content, len(self._block_tokens)))
                if len(self._block_ids) >= 1024:
                    self._block_ids.clear()
                self._block_ids[id(block)] = known
            key += (known[1],)
        if key not in self._memos:
            if len(self._memos) >= 4096:
                self._memos.clear()
            self._memos[key] = compute()
        return self._memos[key]

    def _positions(self, site, line, facts, block=None):
        """Where in the line's blocks the stop can be; in the DWARF lexical block the
        stop's pc is in, where ``block`` tells it apart (#115)."""
        return self._memo(
            "positions", site, line, facts, block, lambda: self._places(site, line, facts, block)
        )

    def _places(self, site, line, facts, block):
        body, function, blocks, shared = site
        if function.inline_only:
            # Before any statement of the line, after any call or drop of it: more
            # places only make a proof stricter. The prologue is the declaration line's.
            text = self.texts.get(function.file)
            if self.precise or not may_invoke_macro(text, line):
                found = body.stop_positions(
                    blocks, line, function.file, may_invoke_macro(text, line)
                )
            else:
                found = body.anywhere(blocks)
            reachable = body._dataflow() or {}
            found = {position for position in found or () if position[0] in reachable}
            if line == function.line:
                found.add((0, 0))
            return sorted(found)
        text = self.texts.get(function.file)
        if self.precise or not may_invoke_macro(text, line):
            foreign = may_invoke_macro(text, line)
            return body.stop_positions(
                blocks, line, function.file, foreign, facts, shared, block, function
            )
        # `#[collapse_debuginfo]` puts a local macro's code on the line invoking it, while
        # its statements keep the macro's lines: any statement can be the stop.
        return body.anywhere(blocks)

    def _judge(self, site, local, line, facts, block=None, why=None):
        """True, False or None for ``local`` (see reference_valid). Where False, ``why``
        (a list, if given) gets the reason: ("raw",), ("dropped",), ("unassigned",), or
        ("since", gaps): {(kind, line)} of what may have run since its last use on some
        path to the stop ("call", "drop", "write", "end" of a variable's storage;
        "start" for a path with no use at all), up to ``Body.gaps_before``'s limit per
        place, the line None where the statement is not in the function's own file (a
        macro's code)."""
        body, function, blocks, _ = site
        if local is None:
            return None
        kind = body.types.get(local)
        if kind is None or not _BORROWING.search(kind):
            return None
        if _FN_POINTER.match(kind):
            return True
        # A raw pointer (or an array of them) keeps no borrow. A tuple or a generic
        # holding one is judged as a value is (the MIR names no field types); the raw
        # pointer inside it shows only its address (#255).
        if _RAW_POINTER.match(kind.lstrip("[")):
            return _false(why, ("raw",))
        assigned = body._assignments().get(local, [])
        # Written through a `&mut` to it (`*p = owned.as_str()`), it holds what no
        # assignment of its own shows.
        free = local not in body._written_through()
        # A parameter's references point at what the caller lent for the whole call; an
        # `async fn`'s, for as long as its future lives (#116), though a copy of one in
        # a local of the body's own frame is gone at a later poll.
        lent = local in body._lent()
        argument = (
            free
            and not _RAW_POINTER.search(kind)
            and (lent or local in body.arguments and not assigned)
        )
        positions = self._positions(site, line, facts, block)
        if not positions:
            return None
        # Between `drop((*v))` and the store of `*v = new`, it points at a dropped value.
        if any(body.dropped_through(local, position) for position in positions):
            return _false(why, ("dropped",))
        if (
            argument
            and kind.startswith("&")
            and (not lent or local in body.arguments or self._initialized(site, local, line))
        ):
            return True
        if free and assigned and all(value.startswith("const ") for value in assigned):
            return True
        # Not yet assigned (its declaration line), it holds whatever its slot last held.
        # Where the stop's pc tells its places apart (#115), initialized at those.
        if not self._initialized(site, local, line) and not (
            block is not None
            and positions != self._positions(site, line, facts)
            and body.initialized_before(local, positions)
        ):
            return _false(why, ("unassigned",))
        if argument:
            return True
        if free and assigned and all(self._frozen(site, local, value, line) for value in assigned):
            return True
        if free and self._borrows_frozen(site, local, line):
            return True
        # #259: `ptr::eq(f, e.payer)` makes `f` a raw pointer (its last use) before the
        # call, which a stop on the line may be past; the call only compares addresses.
        compares = self._std_compares()

        def late(position):
            return not body.used_ahead(local, position) and not body.used_before(
                local, position, compares
            )

        if why is None:
            return not any(late(position) for position in positions)
        gaps = set()
        for position in positions:
            if late(position):
                for number, index in body.gaps_before(local, position, compares):
                    gaps.add(_gap(body, function, number, index))
        if not gaps:
            return True
        why.append(("since", frozenset(gaps)))
        return False

    def _borrows_frozen(self, site, local, line):
        """True if the shared reference ``local`` (`&str`) points only into what frozen
        owners hold (#156): its every value is made from shared borrows of them, through
        shared reborrows, copies, and calls whose borrowing operands are those (`&_1`,
        `<Vec<String> as Index<usize>>::index(move _21, 2)`, `<String as
        Deref>::deref`, `split`, `into_iter`; and `Iterator::next` on an iterator only
        ever borrowed `&mut` by `next`, whose items never borrow the iterator itself).
        A frozen owner is a variable whose scope encloses the reference's, assigned
        once (or a by-value argument never assigned), never moved, partly assigned or
        borrowed `&mut` or raw, initialized at the stop, of a type with no interior
        mutability and no references of its own (a `Vec<String>`, a `String`, integers,
        arrays and tuples of them); or a shared reference argument to such a type (the
        caller's loan freezes it for the call). An async body is not judged so.
        Nothing can then change or free what it holds while the reference is in scope,
        though the borrow checker no longer holds it after its last use (`for v in
        args[2].split(',')`: `v` past the call it was last used in)."""
        body = site[0]
        kind = body.types.get(local, "")
        if (
            not kind.startswith("&")
            or kind.startswith("&mut ")
            or _RAW_POINTER.search(kind)
            # An async body's later poll holds what its state saved (#116, #132): not
            # judged here.
            or body.coroutine is not None
        ):
            return False
        if self.unsafe_code:
            # Unsafe code may free or write what an owner holds behind the MIR's back.
            return False
        if body._frozen_cache is None:
            body._frozen_cache = {}
        iterator = self._std_iterator()
        if (local, iterator) not in body._frozen_cache:
            body._frozen_cache[(local, iterator)] = _frozen_owners(
                body, local, iterator, self._untrusted_names()
            )
        owners = body._frozen_cache[(local, iterator)]
        return bool(owners) and all(
            argument
            or self._memo(
                ("initialized", owner),
                site,
                line,
                None,
                None,
                lambda owner=owner: self._initialized(site, owner, line),
            )
            for owner, argument in owners
        )

    def _untrusted_names(self):
        """Which of the type names the frozen-owner rule trusts by their short form
        (`Vec`, `String`, `Box`, `str`, the scalars: a function's header prints its
        arguments' types so, `&Vec<String>`) may be the crate's own: one it declares
        (`struct Vec`), imports under that name (`use x::Vec`, `as String`), passes bare
        to a macro (`mk!(String)`), or whose `macro_rules!` body names it (a macro may
        declare it, #156 review). A crate whose sources do not lex trusts none."""
        if "_untrusted_known" not in self.__dict__:
            from peeky.rust_lexer import code_only

            names, untrusted = frozenset(_FROZEN_NAMES), set()
            for source in self.sources:
                code = code_only(self.texts[source]) if source in self.texts else None
                if code is None:
                    untrusted = set(names)
                    break
                untrusted |= _named_own(code, names)
            self._untrusted_known = frozenset(untrusted)
        return self._untrusted_known

    def _std_iterator(self):
        """Whether a call the dump names `<T as Iterator>::next` is the standard
        library's: the crate's code names `Iterator` only as the prelude's (`impl
        Iterator`, `dyn Iterator`, `T: Iterator`, `std::iter::Iterator`), never as an
        item or an import of its own, which would also be named `Iterator`."""
        if "_std_iterator_known" not in self.__dict__:
            from peeky.rust_lexer import code_only

            codes = [
                code_only(self.texts[source]) if source in self.texts else None
                for source in self.sources
            ]
            allowed = re.compile(r"(?:\bimpl|\bdyn|:|\+|<|(?:std|core)::iter::)\s*Iterator\b")
            self._std_iterator_known = bool(codes) and all(
                code is not None
                and len(re.findall(r"\bIterator\b", code)) == len(allowed.findall(code))
                for code in codes
            )
        return self._std_iterator_known

    def _std_compares(self):
        """Whether the calls the dump names `std::ptr::eq`, `core::ptr::eq` and `addr_eq`
        (``_ADDRESS_COMPARISON``) are the standard library's (#259). A `mod std` of the
        crate's own, a macro's too (`make_mod!(std)`), is dumped `std::ptr::eq` as well,
        so all of these must hold:

        - no function the crate's MIR dump holds is named so, with any prefix
          (`fn std::ptr::eq`, `fn inner::addr_eq`): the standard library's bodies are
          never among them, and a function called has a body there;
        - its sources lex, and none names an item or import `std` or `core` of its own
          (`_named_own`: `mod std`, `use x as core`, a `macro_rules!` body naming it),
          renames an `extern crate` so, or passes `std` or `core` to a macro other than
          as a path's start (`println!("{}", std::process::id())` is fine);
        - its code names `addr_eq` only after `ptr::`, so it declares none."""
        if "_std_compares_known" not in self.__dict__:
            names = {body.path for body in self.bodies} | self.defined
            codes = self._crate_codes()
            self._std_compares_known = (
                self._std_own()
                and not any(_COMPARISON_NAME.search(name) for name in names)
                and all(
                    len(re.findall(r"\baddr_eq\b", code))
                    == len(re.findall(r"\bptr\s*::\s*addr_eq\b", code))
                    for code in codes
                )
            )
        return self._std_compares_known

    def _crate_codes(self):
        """The crate's sources with comments and literals blanked (``code_only``), each
        None where it does not lex."""
        if "_crate_codes_known" not in self.__dict__:
            from peeky.rust_lexer import code_only

            self._crate_codes_known = [
                code_only(self.texts[source]) if source in self.texts else None
                for source in self.sources
            ]
        return self._crate_codes_known

    def _std_own(self):
        """Whether a path the dump starts `std::` or `core::` is the standard library's:
        the crate's sources lex, and none names an item or import `std` or `core` of its
        own (``_named_own``), renames an `extern crate` so, or passes `std` or `core` to a
        macro other than as a path's start (#259, #311)."""
        if "_std_own_known" not in self.__dict__:
            codes = self._crate_codes()
            self._std_own_known = bool(codes) and all(
                code is not None
                and not _named_own(code, {"std", "core"})
                and not _EXTERN_AS_STD.search(code)
                and not _passes_std(code)
                for code in codes
            )
        return self._std_own_known

    def _std_transfers(self):
        """Whether the calls the dump names as the Box-to-Vec conversion `vec!` makes
        (``_STD_TRANSFER``: `std::boxed::box_assume_init_into_vec_unsafe`) are the standard
        library's (#311), as ``_std_compares`` proves `ptr::eq`: its paths are std's
        (``_std_own``), and no function the crate's MIR dump holds is so named."""
        if "_std_transfers_known" not in self.__dict__:
            names = {body.path for body in self.bodies} | self.defined
            self._std_transfers_known = self._std_own() and not any(
                _TRANSFER_NAME.search(name) for name in names
            )
        return self._std_transfers_known

    def _frozen(self, site, local, value, line):
        """True if ``local`` assigned ``value`` points where nothing can move or free
        while it is in scope: into what a shared reference argument (never reassigned or
        borrowed) points at, or at a variable (or a field or element of it) declared in a
        scope enclosing its own and initialized at the stop."""
        body = site[0]
        if value.startswith(("&raw ", "&fake ")):
            return False
        if found := re.fullmatch(r"(?:copy|move) _([0-9]+)", value):
            return self._shared_argument(body, int(found[1]))
        if not value.startswith("&") or " as " in value:
            return False
        place = re.sub(r"\[_[0-9]+\]", "", value[1:].removeprefix("mut "))
        mentioned = {int(n) for n in _MENTIONED.findall(place)}
        if len(mentioned) != 1:
            return False
        (base,) = mentioned
        if place.count("(*") == 1 and f"(*_{base})" in place:
            return self._shared_argument(body, base)
        if "(*" in place:
            return False
        kind = body.types.get(base)
        if kind is None or _BORROWING.search(kind):
            return False
        own = _binding_scopes(body, local)
        theirs = _binding_scopes(body, base)
        return (
            len(own) == 1
            and len(theirs) == 1
            and _encloses(body, theirs[0], own[0])
            and self._initialized(site, base, line)
        )

    @staticmethod
    def _shared_argument(body, local):
        kind = body.types.get(local, "")
        return (
            (local in body._lent() or local in body.arguments and local not in body._assignments())
            and kind.startswith("&")
            and not kind.startswith("&mut ")
            and local not in body._written_through()
        )

    def _facts(self, function, line, column):
        """What the stop's line holds, for ``stop_positions``: the IR's calls, branches
        and inlined calls on it, its macros' columns, and the stop's column (bytes), when
        the source text is known."""
        text = self.texts.get(function.file)
        lines = _source_lines(text) if isinstance(text, str) else ()
        own = lines[line - 1] if 1 <= line <= len(lines) else None

        def to_bytes(character):
            if own is None:
                return character
            return len(own[: max(character - 1, 0)].encode("utf-8")) + 1

        macros = frozenset(
            to_bytes(found.start(1) + 1) for found in _MACRO_CALL.finditer(own or "")
        )
        return LineFacts(
            column if own is not None and type(column) is int and column > 0 else None,
            function,
            frozenset(function.inlined.get(line, ())),
            macros,
            to_bytes,
            own is not None,
        )

    def _site(self, hashed, line, path):
        """(body, function, the line's blocks, their merges) for a proof at ``line``."""
        body = self.body(hashed)
        function = self.functions.get(hashed)
        if body is None or function is None or type(line) is not int:
            return None
        shown = str(path or "").replace("\\", "/")
        if [source for source in self.sources if shown.endswith("/" + source)] != [function.file]:
            return None
        if function.inline_only:
            # No IR shows where its inlined copies' code is (#116): every block with a
            # statement of the line, or with code another file's macro may put on it,
            # and with `#[collapse_debuginfo]` any block, on a line that may invoke one.
            foreign = may_invoke_macro(self.texts.get(function.file), line)
            if foreign and not self.precise:
                blocks = set(body._dataflow() or ())
            else:
                blocks = body.covering(line, function.file, foreign)
            return (body, function, blocks, None) if blocks or line == function.line else None
        blocks = function.mir_blocks(line)
        if blocks is None:
            return None
        shared = _shared(body, function, blocks)
        return body, function, set(shared), shared

    def _initialized(self, site, local, line):
        body, function, blocks, shared = site
        if function.inline_only:
            return body.initialized_before(local, self._positions(site, line, None))
        if not self.precise:
            return body.initialized_throughout(local, blocks)
        return body.initialized_at(
            local,
            blocks,
            line,
            function.file,
            function.line,
            shared,
            foreign=may_invoke_macro(self.texts.get(function.file), line),
        )


def _first_places(body, function, positions):
    """Of the places a line breakpoint's stop can be, the first in each lexical block
    by address (``Body._address_order``): LLDB stops at the line's lowest address in
    each, before the line's first code there. A temporary a statement before it sets
    without code of its own (`_4 = copy _9`) copies one that is used there, so it is
    judged. A block whose places' order is not known keeps them all."""
    groups = {}
    for position in positions:
        number, index = position
        scope = _scope(body.blocks[number], index) if number in body.blocks else None
        groups.setdefault(None if scope is None else body.lexical(scope), []).append(position)
    kept = []
    for scope, members in groups.items():
        orders = [(body._address_order(position, function), position) for position in members]
        if scope is None or any(order is None for order, _ in orders):
            kept += members
        else:
            kept.append(min(orders)[1])
    return sorted(kept)


def _same_path(body, function):
    """True if the body's path ends as the function's namespaces do, up to an impl (the
    MIR's `<impl at src/a.rs:2:1: 2:11>`, the debug info's `{impl#0}`): a closure in an
    async block (`serve::{closure#0}::{closure#0}::{closure#0}`) is not the block."""
    ancestors = function.ancestors or ((function.parent,) if function.parent else ())
    for mine, theirs in zip(reversed(_segments(body.path)[:-1]), reversed(ancestors)):
        if mine.startswith("<"):
            return True
        if mine != _mir_name(theirs):
            return False
    return True


def _merge_target(body, function, number, predecessors):
    """The MIR block codegen emits inside ``number``'s IR block, if any.

    rustc appends a block to its predecessor's IR block (no label of its own) when the
    predecessor has that one successor and it has that one predecessor: after a
    `goto`, or a call that cannot unwind (an `extern "C"` function).
    """
    successors = body.blocks[number].successors
    if len(successors) != 1:
        return None
    target = successors[0]
    if target in function.labeled() or predecessors.get(target) != {number}:
        return None
    return target


def _merged(body, function, blocks):
    """``blocks`` plus every block codegen merged into them."""
    predecessors, found = body.predecessors(), set(blocks)
    for number in blocks:
        seen = {number}
        while (target := _merge_target(body, function, number, predecessors)) is not None:
            if target in seen:
                break
            seen.add(target)
            found.add(target)
            number = target
    return found


# A line that ends inside a path (an identifier, `::`, a raw identifier's `#`) before
# any line comment: a macro invocation's path may start here and reach its `!` later.
_PATH_END = re.compile(r"[A-Za-z0-9_#:]\s*(?://.*)?$")
# What may continue such a path: whitespace, line comments, `::` and ASCII identifiers.
_PATH_TAIL = re.compile(r"(?:\s+|//[^\n]*|::|[A-Za-z0-9_#])*")


@functools.lru_cache(maxsize=16)
def _source_lines(text):
    """``text``'s lines, split once per source text: a frame's every row asks."""
    return tuple(text.split("\n"))


def may_invoke_macro(text, line):
    """False only if source ``line`` cannot start a macro invocation.

    rustc collapses a macro from another crate onto the line where its invocation's
    path starts (#[collapse_debuginfo]), so code spanning another file can be on such a
    line only. Any `!`, block comment or non-ASCII character on the line counts; so does
    a line ending in a path that a later `!` may complete, or that continues past a
    block comment or a non-ASCII identifier. Unknown text means any line can.

    ``text`` must be split into lines as rustc does, on `\n` alone (never a lone `\r`).
    """
    lines = _source_lines(text) if isinstance(text, str) else ()
    if not 1 <= line <= len(lines):
        return True
    own = lines[line - 1]
    if "!" in own or "/*" in own or not own.isascii():
        return True
    # A trailing `// comment` ends no path (`let n = x * 2; // doubled`), unless a quote
    # before it may hold the `//`.
    code, comment, after = own.partition("//")
    # A quote before it may hold the `//`; a quote or `*/` after it, a string or block
    # comment that began on an earlier line and closes here.
    plain = comment and not any(mark in code for mark in "\"'") and '"' not in after
    if not _PATH_END.search(code if plain and "*/" not in after else own):
        return False
    rest = "\n".join(lines[line:])
    following = rest[_PATH_TAIL.match(rest).end() :][:1]
    return following in ("!", "/") or not following.isascii()


def _shared(body, function, blocks):
    """{block: the blocks in its IR block} for ``blocks`` and every block codegen merged
    into them."""
    predecessors, shared = body.predecessors(), {}
    for number in blocks:
        chain = [number]
        while (target := _merge_target(body, function, chain[-1], predecessors)) is not None:
            if target in chain:
                break
            chain.append(target)
        for member in chain:
            shared.setdefault(member, set()).update(chain)
    return shared


def _covered(body, function):
    """Every reachable MIR block has its own IR block or was merged into one; any other
    layout is not understood, so nothing in the function is proven."""
    entry = body._dataflow()
    if entry is None:
        return False
    covered = _merged(body, function, function.labeled() & set(entry))
    return all(
        number in covered or body.blocks[number].terminator == "unreachable;" for number in entry
    )
