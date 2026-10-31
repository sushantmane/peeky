"""A call into a dependency's code, which Peeky reads none of (ADR 0008, phase 2): what the
MIR proofs take it may do, how a change it may keep a copy of is refused, and what a
workspace's session reads as its program."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky.rust import _foreign_note, _foreign_row
from peeky.rust_mir import (
    OwnedLocals,
    _bound_names,
    _call_head,
    _crate_function,
    _foreign_root,
    _Forwarding,
    _qualified_call,
    parse_body,
)
from peeky.rust_workspace import _crate_names
from peeky.rust_workspace_session import expanded_code
from peeky.worker import _inside_program

FOREIGN = frozenset({"smallnum", "tokio"})


def body(name, header, declarations, blocks):
    """A `-Zdump-mir` body of src/lib.rs: ``blocks`` {number: [statement, ...]}, each
    statement of block n on line 10 + n."""
    lines = [f"// MIR for `{name}` after PreCodegen", "", header]
    lines += [f"    {each}  // in scope 0 at src/lib.rs:2:9: 2:10" for each in declarations]
    lines.append("")
    for number, statements in blocks.items():
        lines.append(f"    bb{number}: {{")
        for statement in statements:
            lines.append(f"        {statement}  // scope 0 at src/lib.rs:{10 + number}:5: 10:9")
        lines += ["    }", ""]
    return parse_body("\n".join([*lines, "}", ""]))


def matched(*bodies, foreign=FOREIGN):
    """The bodies as OwnedLocals matches them, with the build's other crates ``foreign``."""
    owned = OwnedLocals(list(bodies), {}, (), foreign=foreign)
    for each in bodies:
        each.foreign, each.forwarding, each.generics = owned.foreign, owned._forwarding(), ()
    return owned


def forward():
    return body(
        "forward",
        "fn forward(_1: &u32) -> () {",
        ["let mut _0: ();", "let mut _2: &u32;"],
        {
            0: ["_2 = copy _1;", "_0 = smallnum::keep(move _2) -> [return: bb1, unwind continue];"],
            1: ["return;"],
        },
    )


def run(call="forward"):
    """`let x = arg; {call}(&x); let y = 2; {call}(&y); x`, the stop at bb2."""
    return body(
        "run",
        "fn run(_1: u32) -> u32 {",
        [
            "let mut _0: u32;",
            "let _2: u32;",
            "let mut _3: &u32;",
            "let mut _4: ();",
            "let _5: u32;",
            "let mut _6: &u32;",
            "let mut _7: ();",
        ],
        {
            0: [
                "_2 = copy _1;",
                "_3 = &_2;",
                f"_4 = {call}(move _3) -> [return: bb1, unwind continue];",
            ],
            1: [
                "_5 = const 2_u32;",
                "_6 = &_5;",
                f"_7 = {call}(move _6) -> [return: bb2, unwind continue];",
            ],
            2: ["_0 = copy _2;", "return;"],
        },
    )


@pytest.mark.parametrize(
    ("text", "root"),
    [
        ("smallnum::keep", "smallnum"),
        ("std::mem::drop::<smallnum::Holder>", "smallnum"),
        ("<tokio::runtime::Runtime as std::ops::Drop>::drop", "tokio"),
        ("std::vec::Vec::<u32>::push", None),
        ("<dyn std::any::Any as std::any::Any>::type_id", "?"),
        ("<T as std::fmt::Display>::fmt", "?"),
        # A span's file names no crate by its root (``_foreign_call`` judges a closure the
        # crate read no body of); a dependency's closure is named by its path.
        ("<{closure@smallnum/src/lib.rs:3:5: 3:9} as std::ops::FnOnce<()>>::call_once", None),
        ("<{closure@smallnum::keeper::{closure#0}} as std::ops::Fn<(&u32,)>>::call", "smallnum"),
        ("<impl at smallnum/src/lib.rs:5:1: 5:17>::new", None),
        ('core::panicking::panic("smallnum")', None),
    ],
)
def test_a_path_is_another_crates_code_by_its_root(text, root):
    assert _foreign_root(text, FOREIGN, ("T",)) == root
    # A crate built alone has no other crate.
    assert _foreign_root(text, frozenset(), ("T",)) is None


@pytest.mark.parametrize(
    ("path", "head"),
    [
        ("<smallnum::Holder as std::clone::Clone>::clone", "smallnum::Holder as std::clone::Clone"),
        ("std::mem::drop::<smallnum::Holder>", "std::mem::drop"),
        ("smallnum::spawn::<{closure@util/src/lib.rs:33:29: 33:36}>", "smallnum::spawn"),
        ("std::vec::Vec<smallnum::Holder>", "std::vec::Vec"),
    ],
)
def test_what_a_call_runs_is_named_by_its_head(path, head):
    assert _call_head(path) == head


def test_a_dependency_call_given_the_local_before_the_stop_is_named():
    keeps = run("smallnum::keep")
    matched(keeps)
    # At bb2: `smallnum::keep(&x)` ran, given a pointer into `x` (_2); the second call was
    # given `&y` only.
    assert keeps.foreign_given(2, [(2, 0)]) == [
        ((0, 2), "`smallnum::keep`", "smallnum", "pointer", None)
    ]
    # Before the call, nothing has been given it.
    assert keeps.foreign_given(2, [(0, 2)]) == []
    # `y`'s own call.
    assert [found[0] for found in keeps.foreign_given(5, [(2, 0)])] == [(1, 2)]


def test_code_of_the_crates_that_hands_on_what_it_is_given_counts_by_its_first_call():
    caller = run()
    owned = matched(forward(), caller)
    assert owned._forwarding().names == {
        "forward": ("`smallnum::keep`", "smallnum"),
        # `run` hands on a copy of its own argument, through `forward`.
        "run": ("`smallnum::keep`", "smallnum"),
    }
    assert caller.foreign_given(2, [(2, 0)]) == [
        ((0, 2), "`forward`", "smallnum", "pointer", ("`smallnum::keep`", "smallnum"))
    ]


def test_code_of_the_crates_that_keeps_what_it_is_given_to_itself_counts_for_nothing():
    alone = body(
        "forward",
        "fn forward(_1: &u32) -> () {",
        ["let mut _0: ();", "let mut _2: u32;"],
        {
            0: [
                "_2 = smallnum::one() -> [return: bb1, unwind continue];",
            ],
            1: ["_0 = std::hint::black_box::<&u32>(copy _1) -> [return: bb2, unwind continue];"],
            2: ["return;"],
        },
    )
    caller = run()
    owned = matched(alone, caller)
    # `smallnum::one()` is given nothing of `forward`'s argument.
    assert "forward" not in owned._forwarding().names
    assert caller.foreign_given(2, [(2, 0)]) == []


def test_a_new_value_of_the_local_is_out_of_an_earlier_calls_reach():
    restarted = body(
        "run",
        "fn run(_1: u32) -> u32 {",
        ["let mut _0: u32;", "let mut _2: u32;", "let mut _3: &u32;", "let mut _4: ();"],
        {
            0: [
                "_2 = copy _1;",
                "_3 = &_2;",
                "_4 = smallnum::keep(move _3) -> [return: bb1, unwind continue];",
            ],
            1: ["_2 = const 7_u32;", "_0 = copy _2;", "return;"],
        },
    )
    matched(restarted)
    # Past `x = 7`, the copy the dependency may keep is of an earlier value.
    assert restarted.foreign_given(2, [(1, 1)]) == []
    assert [found[3] for found in restarted.foreign_given(2, [(1, 0)])] == ["pointer"]


def test_a_crate_built_alone_has_no_foreign_calls():
    keeps = run("smallnum::keep")
    owned = matched(keeps, foreign=frozenset())
    assert owned._forwarding() is None
    assert keeps.foreign_calls() == {} and keeps.foreign_given(2, [(2, 0)]) == []


def test_a_drop_runs_a_dependencys_drop_by_what_it_holds():
    keeps = run("smallnum::keep")
    matched(keeps)
    assert keeps._foreign_held("smallnum::Holder") == "smallnum"
    assert keeps._foreign_held("std::vec::Vec<smallnum::Holder>") == "smallnum"
    assert keeps._foreign_held("std::vec::Vec<u32>") is None
    assert keeps._foreign_held("(u32, std::string::String)") is None
    assert keeps._foreign_held("T", generics=("T",)) == "?"
    assert keeps._foreign_held("std::boxed::Box<dyn std::any::Any>") == "?"
    # Another crate's function (std's) dropping it is named as one that may run it.
    edge = ("drop", "", ("std::vec::Vec<smallnum::Holder>",), "")
    assert keeps._foreign_edge(edge, None, FOREIGN, ()) == (
        "the drop of a `std::vec::Vec<smallnum::Holder>`",
        "~smallnum",
        None,
    )


def test_the_note_names_the_call_its_line_and_its_crate():
    home = "util/src/lib.rs"
    direct = [(home, 17, "`smallnum::keep`", "smallnum", "pointer", None)]
    assert _foreign_note(direct, "`x`", home) == (
        "a call to `smallnum::keep` (dependency `smallnum`) on line 17 is given a pointer "
        "into `x`: Peeky reads no dependency's code, so it may keep a copy, which a change "
        "would not reach"
    )
    via = [(home, 54, "`forward`", "smallnum", "value", ("`smallnum::keep`", "smallnum"))]
    assert _foreign_note(via, "`x`", home).startswith(
        "a call to `forward` on line 54 is given `x`, and may hand it on to `smallnum::keep` "
        "(dependency `smallnum`): "
    )
    typed = [(home, 9, "`std::hint::black_box::<&smallnum::Holder>`", "~smallnum", "copy", None)]
    assert "(which may run code of a type of dependency `smallnum`) on line 9" in _foreign_note(
        typed, "`x`", home
    )
    # Another analysed crate is proven apart, not unread.
    sibling = [(home, 3, "`util::double`", "util", "value", None)]
    assert _foreign_note(sibling, "`x`", home, {"util"}) == (
        "a call to `util::double` (crate `util`, which Peeky proves apart) on line 3 is given "
        "`x`: Peeky proves each crate apart and reads none of its code here, so it may keep a "
        "copy, which a change would not reach"
    )
    many = direct * 5
    assert "; 2 more such calls:" in _foreign_note(many, "`x`", home)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (
            "#![feature(prelude_import)]\nextern crate std;\n#[prelude_import]\n"
            "use std::prelude::rust_2021::*;\nfn main() {}\n",
            "fn main() {}\n",
        ),
        (
            "#![feature(prelude_import)]\n#![forbid(unsafe_code)]\n//! doc\nextern crate std;\n"
            "#[prelude_import]\nuse std::prelude::rust_2024::*;\npub fn f() {}\n",
            "#![forbid(unsafe_code)]\n//! doc\npub fn f() {}\n",
        ),
        (
            "#![feature(prelude_import)]\n#![no_std]\nextern crate core;\n#[prelude_import]\n"
            "use core::prelude::rust_2021::*;\n",
            "#![no_std]\n",
        ),
        # Not as rustc writes it: as it is, so the feature reads as one.
        (
            "#![feature(prelude_import)]\nfn main() {}\n",
            "#![feature(prelude_import)]\nfn main() {}\n",
        ),
        ("fn main() {}\n", "fn main() {}\n"),
    ],
)
def test_the_expansion_is_read_without_what_rustc_adds(tmp_path, text, kept):
    path = tmp_path / "expanded.rs"
    path.write_text(text)
    assert expanded_code(path) == kept
    assert expanded_code(tmp_path / "missing.rs") is None


def test_a_builds_crates_are_its_units_and_the_names_crates_call_them_by():
    units = [
        {"target": {"name": "app"}, "dependencies": [{"index": 1, "extern_crate_name": "util"}]},
        {"target": {"name": "util"}, "dependencies": [{"index": 2, "extern_crate_name": "num"}]},
        {"target": {"name": "small-num"}, "dependencies": []},
    ]
    assert _crate_names(units) == ("app", "num", "small_num", "util")


def test_a_workspace_sessions_program_is_its_analysed_files(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "util/src").mkdir(parents=True)
    (workspace / "vendor/smallnum/src").mkdir(parents=True)
    own = workspace / "util/src/lib.rs"
    vendored = workspace / "vendor/smallnum/src/lib.rs"
    for path in (own, vendored):
        path.write_text("")

    class Session:
        prepared = SimpleNamespace(workspace=workspace, source_root=workspace)
        root = workspace

        def owns_source(self, path):
            return Path(path) == own.resolve()

    def frame(path):
        return {"source": {"path": str(path)}}

    assert _inside_program(Session(), frame(own))
    # Under the workspace, but a dependency's.
    assert not _inside_program(Session(), frame(vendored))
    # Without the hook, everything under the workspace is the program's.
    plain = SimpleNamespace(prepared=Session.prepared, root=workspace)
    assert _inside_program(plain, frame(vendored))


def test_a_refused_change_says_to_make_it_before_the_dependency_is_given_the_value():
    from peeky.errors import classify

    note = _foreign_note(
        [("util/src/lib.rs", 17, "`smallnum::keep`", "smallnum", "pointer", None)],
        "`x`",
        "util/src/lib.rs",
    )
    for message in (
        f"'x' cannot be changed here: {note}. Change it before the program hands it to that "
        "code (a stop on an earlier line)",
        f"`x` cannot be changed at util/src/lib.rs:18, at any stop of this build: in util::kept, "
        f"{note}. Refused before the program runs: nothing ran",
    ):
        found = classify(ValueError(message))
        assert (found["code"], found["retryable"]) == ("MUTATION_REJECTED", False)
        assert found["hint"].startswith("Change it before the program hands it to that code")


def test_a_rows_mark_names_the_first_call_and_where_it_hands_it_on():
    direct = ("util/src/lib.rs", 17, "`smallnum::keep`", "smallnum", "pointer", None)
    assert _foreign_row(direct) == "a dependency may hold a copy (`smallnum::keep`, line 17)"
    via = (
        "util/src/lib.rs",
        54,
        "`forward`",
        "smallnum",
        "pointer",
        ("`smallnum::keep`", "smallnum"),
    )
    assert _foreign_row(via) == (
        "a dependency may hold a copy (`forward`, line 54, which may hand it on to "
        "`smallnum::keep`)"
    )
    unread = ("util/src/lib.rs", 3, "`helper`", "!", "value", None)
    assert _foreign_row(unread).startswith("code Peeky can't read may hold a copy")
    assert _foreign_row(direct, {"smallnum"}).startswith("another crate may hold a copy")


def state(blocks=(), traits=frozenset(), uses=(), known=frozenset()):
    """A ``_Forwarding`` with nothing that hands on what it is given, but the crate's
    `impl` blocks, traits, `use` trees and the closures whose bodies it read."""
    return _Forwarding(
        {},
        {},
        None,
        frozenset(),
        frozenset({"std", "core", "alloc"}),
        None,
        known,
        blocks,
        traits,
        uses,
    )


def call(path, kinds=(), forwarding=None):
    keeps = run("smallnum::keep")
    matched(keeps)
    return keeps._foreign_edge(("call", "", kinds, path), forwarding or state(), FOREIGN, ())


def test_a_call_through_a_function_pointer_or_a_closure_peeky_read_none_of_may_be_a_dependencys():
    # `let f: fn(&u32) = smallnum::maker(); f(&x);`: its target isn't followed.
    assert call("move _6", ("for<'a> fn(&'a u32)",)) == (
        "a call through a function pointer",
        "?fn",
        None,
    )
    # A closure of the crate's own the dump holds, given to the standard library's code: no
    # dependency's code; one whose body Peeky read none of may be a dependency's.
    span = "{closure@util/src/lib.rs:3:5: 3:9}"
    path = f"<std::ops::Range<u32> as std::iter::Iterator>::map::<u32, {span}>"
    assert call(path, forwarding=state(known=frozenset({span}))) is None
    assert call(path)[1] == "?closure"


@pytest.mark.parametrize(
    ("path", "parts"),
    [
        ("<Mine<'_> as smallnum::Keep>::keep_it", ("Mine<'_>", "smallnum::Keep", "keep_it")),
        (
            "<std::vec::Vec<u32> as std::clone::Clone>::clone",
            ("std::vec::Vec<u32>", "std::clone::Clone", "clone"),
        ),
        ("<[u32]>::len", ("[u32]", "", "len")),
        ("smallnum::keep", None),
    ],
)
def test_a_qualified_call_names_its_type_and_trait(path, parts):
    assert _qualified_call(path) == parts


MINE = ("Mine", ("smallnum::Keep", False), frozenset({"keep_it", "value"}))


def test_a_dependency_traits_method_the_crates_own_impl_defines_is_the_crates_code():
    own = state(blocks=(MINE,))
    assert call("<Mine<'_> as smallnum::Keep>::keep_it", ("&Mine<'_>",), own) is None
    # Its default method, which that `impl` leaves as it is: the dependency's code.
    assert call("<Mine<'_> as smallnum::Keep>::noted", ("&Mine<'_>",), own)[1] == "smallnum"
    # No `impl` of the crate's: a dependency's own, or its blanket one.
    assert call("<u32 as smallnum::Stash>::stash", ("&u32",), own)[1] == "smallnum"
    # The crate's trait, for a dependency's type: only the crate implements it.
    assert call("<smallnum::Holder as Show>::show", ("&smallnum::Holder", "&u32"), own) is None
    assert _crate_function("<smallnum::Holder as Show>::show", FOREIGN)
    assert not _crate_function("<Mine as std::string::ToString>::to_string", FOREIGN)


def test_an_impl_by_a_bare_trait_name_is_the_dependencys_traits_only_where_a_use_binds_it():
    bare = ("Mine", ("Keep", False), frozenset({"keep_it"}))
    path = "<Mine as smallnum::Keep>::keep_it"
    assert call(path, (), state(blocks=(bare,), uses=("smallnum::Keep",))) is None
    assert call(path, (), state(blocks=(bare,), uses=("smallnum::{Keep, one}",))) is None
    aliased = ("Mine", ("Kept", False), frozenset({"keep_it"}))
    assert call(path, (), state(blocks=(aliased,), uses=("smallnum::Keep as Kept",))) is None
    assert call(path, (), state(blocks=(bare,), uses=("smallnum::*",))) is None
    # The crate's own trait of that name, or no `use` that says: Peeky can't tell.
    assert call(path, (), state(blocks=(bare,), traits=frozenset({"Keep"})))[1] == "?impl"
    assert call(path, (), state(blocks=(bare,), uses=("other::Keep",)))[1] == "?impl"
    # A header Peeky can't read.
    assert call(path, (), state(blocks=((None, None, frozenset({"keep_it"})),)))[1] == "?impl"
    # A trait object or a type parameter: any type's `impl`.
    assert call("<dyn smallnum::Keep as smallnum::Keep>::keep_it")[1] == "?"


def test_the_names_a_use_binds_to_a_trait():
    trees = ("smallnum::{Keep, Stash as Stashed}", "other::Keep", "::smallnum::nested::*")
    assert _bound_names(trees, "smallnum::Keep") == (frozenset({"Keep"}), True)
    assert _bound_names(trees, "smallnum::Stash") == (frozenset({"Stashed"}), True)
    assert _bound_names(("tokio::Keep",), "smallnum::Keep") == (frozenset(), False)


@pytest.mark.parametrize(
    ("root", "said"),
    [
        ("?fn", "whose target Peeky can't identify, so it may run a dependency's code"),
        ("?closure", "a closure or async block whose code Peeky read none of"),
        ("?impl", "where Peeky can't tell the crate's own `impl` of it from a dependency's"),
    ],
)
def test_the_note_says_why_peeky_cant_tell_whose_code_runs(root, said):
    row = ("util/src/lib.rs", 7, "a call through a function pointer", root, "pointer", None)
    note = _foreign_note([row], "`x`", "util/src/lib.rs")
    assert said in note, note
    assert note.startswith("a call through a function pointer (") if root == "?fn" else True
