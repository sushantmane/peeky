"""The Box gate's type tests: LLDB's C-style declarators and the MIR's non-owning types;
and which pointer inside a value is a raw pointer."""

from types import SimpleNamespace

import pytest

from peeky.fields import Unsettable
from peeky.rust import (
    _NON_OWNING,
    RustSession,
    _constructors,
    _holds_data_pointers,
    _is_wide_box,
    _may_hold_raw,
    _may_own,
)


@pytest.mark.parametrize(
    "kind",
    [
        "Pt *",
        "vb6a::Pt *",
        "Pt *const",
        "alloc::boxed::Box<str> *",
        "(long, long) *",
        "&[unsigned char] *",
        "unsigned char (*)[8]",
        "unsigned char (*)[2][4]",
        "(long, long) (*)[2]",
        "long (**)()",
        "long (*const)(long)",
        "long (*)(vb6a::Pt *)",
        "long *(*)(long)",
        "unsigned char (*(*)[2])[2]",
        "long (*(**)(long))[2]",
        # A tuple base that starts like a group, and a `->` inside a generic's argument.
        "(*const u8, i64) (*)[2]",
        "(*const u8, i64) *",
        "vb7a::W<(u8, fn() -> u8)> (*)[2]",
        "vb7a::W<(u8, fn() -> u8)> *",
        # A parameter list that opens with a fat raw pointer.
        "void (**)(*const [u8])",
        "unsigned char (*(*)[2])(*const str)",
        "void (*(*)[2])(*const [u8])",
        "void (*(**)())(*const [u8])",
        "void (**)(*const [u8] *)",
        "void (**)(*mut dyn core::fmt::Debug)",
        "void (**)(*mut (dyn core::ops::function::Fn<(), Output=()> + core::marker::Send))",
        "*const [u8] (**)(*const [u8])",
    ],
)
def test_top_level_pointers(kind):
    assert _constructors(kind)[:1] == ["pointer"]


@pytest.mark.parametrize(
    "kind",
    [
        None,
        "",
        "long",
        "Vec<Pt *>",
        "alloc::boxed::Box<str>",
        "&[unsigned char]",
        "(long, long)",
        "(long (*)(long), int)",
        "unsigned char *[2]",
        "long (*[2])(long)",
        "long *(*[2])(long)",
        # A parameter's `(*)` is not the declarator.
        "unsigned char (*[2])(unsigned char (*)[4])",
        "void (*[2])(void (*)(long))",
        "long (*(*[3])(long))[2]",
        "(*const u8, i64)",
        "(*const u8, i64)[2]",
        "vb7a::W<(u8, fn() -> u8)>",
        "(u8, fn() -> u8) (*[2])(long)",
        "void (*[2])(*const [u8])",
        "void (*const [u8])",
    ],
)
def test_arrays_and_non_pointers(kind):
    assert _constructors(kind)[:1] != ["pointer"]


@pytest.mark.parametrize(
    "mir",
    [
        "&Pt",
        "&mut [u8]",
        "*const u8",
        "*mut Pt",
        "fn(i64) -> i64",
        "unsafe fn()",
        'extern "C" fn(i64)',
        # A fn pointer taking a reference has a binder.
        "for<'a> fn(&'a Pt) -> i64",
        "for<'a, 'b> unsafe extern \"C\" fn(&'a u8, &'b u8)",
    ],
)
def test_non_owning_mir_types(mir):
    assert _NON_OWNING.match(mir)


@pytest.mark.parametrize(
    "mir", ["std::boxed::Box<Pt>", "Box<Pt>", "[fn(i64); 2]", "T", "(Box<u8>, &u8)"]
)
def test_owning_or_unknown_mir_types(mir):
    assert not _NON_OWNING.match(mir)


@pytest.mark.parametrize(
    "kind",
    [
        "alloc::boxed::Box<str>",
        "alloc::boxed::Box<dyn core::fmt::Debug>",
        "alloc::boxed::Box<[vb::Pt], alloc::alloc::Global>",
        "alloc::boxed::Box<str>[2]",
        "alloc::boxed::Box<str>[2][3]",
    ],
)
def test_wide_boxes(kind):
    assert _is_wide_box(kind)


@pytest.mark.parametrize(
    "kind",
    [
        # Arrays of fn pointers returning a Box, and of references
        # to one, begin with the Box's name too.
        "alloc::boxed::Box<dyn vb8c::Shape> (*[2])()",
        "alloc::boxed::Box<str> *[2]",
        "alloc::boxed::Box<str> *",
        "alloc::boxed::Box<dyn vb8c::Shape> (*)()",
        "vb::Pt *",
        "core::option::Option<alloc::boxed::Box<str> >",
    ],
)
def test_not_wide_boxes(kind):
    assert not _is_wide_box(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "Pt *",
        "long (**)(long)",
        "unsigned char (*)[8]",
        # Arrays of data pointers: what `[Box<Pt>; 2]` is.
        "vb9a::Pt *[2]",
        "vb9d::Pt *[1][2]",
        "unsigned char (*[2])[4]",
        "long (**[2])(long)",
        "alloc::boxed::Box<str> *[2]",
    ],
)
def test_data_pointers_and_arrays_of_them(kind):
    assert _holds_data_pointers(kind)


@pytest.mark.parametrize(
    "kind",
    [
        # A fn pointer points at code, never at a Box's allocation.
        "long (*)(long)",
        "long (*[2])(long)",
        "alloc::boxed::Box<dyn S> (*[2])()",
        "unsigned char[4]",
        "(long, long)",
        "&[unsigned char]",
        "Vec<Pt *>",
    ],
)
def test_not_data_pointers(kind):
    assert not _holds_data_pointers(kind)


@pytest.mark.parametrize("mir", ["[&Pt; 2]", "[[&Pt; 2]; 3]", "[fn(i64) -> i64; 2]"])
def test_arrays_of_non_owning_mir_types(mir):
    assert _NON_OWNING.match(mir.lstrip("["))


@pytest.mark.parametrize(
    "mir",
    [
        "i64",
        "usize",
        "()",
        "&Holder",
        "&mut std::vec::Vec<i64>",
        "*const Pt",
        "fn(i64) -> i64",
        "(i64, char)",
        "(&Pt, i64)",
        "(fn(i64) -> i64, &str)",
        "[i64; 4]",
        "[(u8, bool); 2]",
        "[&std::boxed::Box<Pt>; 2]",
        "((i64, f64), [u8; 3])",
    ],
)
def test_values_that_own_no_memory(mir):
    # Primitives, references and raw or fn pointers, and tuples or arrays of them.
    assert not _may_own(mir)


@pytest.mark.parametrize(
    "mir",
    [
        None,
        "Holder",
        "Flat",
        "T",
        "std::boxed::Box<Pt>",
        "std::vec::Vec<i64>",
        "std::string::String",
        "std::rc::Rc<Pt>",
        "std::option::Option<std::boxed::Box<Pt>>",
        "std::pin::Pin<std::boxed::Box<Pt>>",
        "(std::boxed::Box<Pt>, i64)",
        "(i64, (u8, std::string::String))",
        "(fn(i64) -> i64, Holder)",
        "[Holder; 2]",
        "[(std::boxed::Box<Pt>, i64); 2]",
        "{closure@src/main.rs:27:22: 27:35}",
    ],
)
def test_values_that_may_own_memory(mir):
    # A struct's name lists no fields: it may hold a Box.
    assert _may_own(mir)


@pytest.mark.parametrize(
    "mir, generics",
    [
        ("i64", ("i64",)),
        ("(i8, i8)", ("i8",)),
        ("[u64; 2]", ("u64",)),
        ("i64", None),
        ("(i64, char)", None),
    ],
)
def test_a_generic_bodys_primitive_names_may_be_type_parameters(mir, generics):
    # `fn keep<i64>(h: i64)` called with a struct owning a Box.
    assert _may_own(mir) is False
    assert _may_own(mir, generics)
    # A reference never owns, whatever it points at.
    assert not _may_own(f"&{mir}", generics)


@pytest.mark.parametrize(
    "mir",
    ["i64", "()", "&i64", "&&str", "&'static str", "&mut [u8]", "(i64, &i64)", "[&u8; 2]"]
    + ["fn(&Pt) -> i64", "for<'a> fn(&'a Pt) -> i64", "(&[i64], (u8, &&char))"],
)
def test_values_that_hold_no_raw_pointer(mir):
    assert not _may_hold_raw(mir)


@pytest.mark.parametrize(
    "mir",
    [None, "*const i64", "*mut u8", "(i64, *const i64)", "[*mut u8; 2]", "&[*const u8]"]
    + ["Holder", "&Holder", "&mut app::Holder", "std::ptr::NonNull<i64>", "&dyn Debug", "T"],
)
def test_values_that_may_hold_a_raw_pointer(mir):
    # A struct's or a generic's name says nothing of its fields.
    assert _may_hold_raw(mir)


def test_a_generic_bodys_primitive_name_may_hold_a_raw_pointer():
    assert _may_hold_raw("i64", ("i64",)) and _may_hold_raw("&i64", None)


def test_a_pointer_in_a_value_is_raw_by_its_debug_information_else_by_the_mir(monkeypatch):
    # The field's Rust type from the build's debug information decides; where
    # that says nothing, any pointer in a local whose MIR type may hold a raw pointer is
    # withheld as maybe one, and one in a local of references and primitives is not.
    import peeky.fields

    frame = {"name": "app::work::h0123456789abcdef", "line": 9, "source": {"path": "main.rs"}}
    said, mir = [None], [["(i64, &i64)"]]
    monkeypatch.setattr(peeky.fields, "rust_raw_pointer", lambda graph, root, steps: said[0])
    session = SimpleNamespace(
        _field_types=lambda: ({"h0123456789abcdef": {}}, {}),
        _declared_type=lambda types, name, where: (1, True),
        _frame_scope=lambda frame, root: None,
        _field_cache=({}, {}, {}),
        _binding_types=lambda variable, frame: mir[0],
        owned=SimpleNamespace(type_parameters=lambda hashed: ()),
    )

    def raw(kind="long *"):
        variable = {"name": "__1", "type": kind}
        return RustSession._raw_pointer(session, variable, frame, [("t", "(i64, &i64)")])

    said[0] = True
    assert raw() == "raw"
    said[0] = False
    assert raw() is None
    said[0] = None
    assert raw() is None
    for types in (["app::Holder"], [], ["(i64, &i64)", "*const i64"]):
        mir[0] = types
        assert raw() == "unknown", types

    def unread():
        raise Unsettable("Peeky could not read the build's debug information")

    session._field_types, mir[0] = unread, ["(i64, &i64)"]
    assert raw() is None
    session.owned = None
    assert raw() == "unknown"
    # Not a data pointer as LLDB shows it: an array of them is judged by element, a fn
    # pointer points at code.
    for kind in ("long *[2]", "long (*)(long)", "app::Holder", "i64"):
        assert raw(kind) is None, kind


def test_a_non_null_local_shows_only_its_address_as_a_raw_pointer_does():
    # A `NonNull<T>` local was withheld as `<not shown: …>`, its note saying it showed
    # its pointer. Its one field, `pointer`, is a `*const T`: that address is the local's
    # own value, shown as a raw pointer local's is (by its MIR type, before any liveness
    # check), and never followed.
    from peeky.adapters import ViewBudgetExhausted

    frame = {"name": "app::work::h0123456789abcdef", "line": 9, "source": {"path": "main.rs"}}
    mir = [["std::ptr::NonNull<app::Pt>"]]
    session = SimpleNamespace(
        layouts={},
        target=SimpleNamespace(opt_level="0"),
        parameters={},
        unsafe_code=None,
        owned=SimpleNamespace(
            declared_type=lambda *args: mir[0][0],
            proven=lambda *args: True,
            type_parameters=lambda hashed: (),
            reference_valid=lambda *args, **kwargs: False,
        ),
        _binding_types=lambda variable, frame: mir[0],
        _stop_block=lambda hashed, frame: None,
    )
    pointer = {"name": "pointer", "type": "app::Pt *", "value": "0x10", "variablesReference": 7}
    listed = [[pointer]]

    def fetch(reference):
        assert reference == 3
        if isinstance(listed[0], Exception):
            raise listed[0]
        return listed[0]

    def view(kind="core::ptr::non_null::NonNull<app::Pt>", **frames):
        variable = {"name": "n", "type": kind, "value": "", "variablesReference": 3}
        found = RustSession.view_variable(
            session, variable, fetch, None, frame=frame, top=True, **frames
        )
        return found and {key: found.get(key) for key in ("value", "rust_view", "pointer", "why")}

    address = {"value": "0x10", "rust_view": "unproven", "pointer": "raw", "why": None}
    for frames in ({"innermost": True}, {"innermost": False}):
        assert view(**frames) == address, frames
    session.target.opt_level = "2"
    assert view() == address
    session.target.opt_level = "0"
    # LLDB's summary of a `char *` reads through it: only the address shows.
    pointer.update(type="unsigned char *", value='0x10 "abc"')
    assert view() == address
    # A wide one's pointer is listed in parts: nothing shows, saying why.
    pointer.update(type="*const [i64]", value="*const [i64] @ 0x16f382f38")
    wide = view()
    assert wide["value"] == "<not shown: may point at freed memory>", wide
    assert wide["pointer"] == "raw" and wide["why"].startswith("this one is a wide pointer")
    listed[0] = ValueError("Variable is not expandable")
    assert view()["why"] == "the debugger gave no address for this one"
    listed[0] = ViewBudgetExhausted("Value view budget exhausted: 128 debugger reads")
    budget = view()
    assert budget["rust_view"] == "budget-exhausted" and "128 debugger reads" in budget["value"]
    # Only std's NonNull, which the MIR names too: a crate's own type is not judged so.
    listed[0] = [{**pointer, "type": "app::Pt *", "value": "0x10"}]
    mir[0] = ["NonNull<app::Pt>"]
    assert (view("app::NonNull<app::Pt>") or {}).get("value") != "0x10"


def test_a_value_shown_by_no_address_claims_a_pointer_only_where_it_surely_holds_one():
    # Withheld before its variant is read, an `Option<&T>` that is now `None` showed
    # `<not shown: may point at freed memory>`. Only a reference or a NonNull, whose
    # gate says why, surely holds a pointer; anything else is said of its type.
    from peeky.rust import _unproven

    option = {"name": "o", "type": "core::option::Option<&app::Pt>", "value": "None"}
    assert _unproven(option)["value"] == "<not shown: its type can hold a pointer>"
    text = {"name": "s", "type": "&str", "value": '"abc"'}
    why = "the build's MIR does not show that what it points at still stands here"
    assert _unproven(text, why)["value"] == "<not shown: may point at freed memory>"
    assert _unproven({**text, "value": "0x10"}, why)["value"] == "0x10"
