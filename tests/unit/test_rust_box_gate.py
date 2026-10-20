"""The Box gate's type tests: LLDB's C-style declarators and the MIR's non-owning types."""

import pytest

from peeky.rust import (
    _NON_OWNING,
    _constructors,
    _holds_data_pointers,
    _is_wide_box,
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
        # A tuple base that starts like a group, and a `->` inside a generic's argument
        # (verification of #103).
        "(*const u8, i64) (*)[2]",
        "(*const u8, i64) *",
        "vb7a::W<(u8, fn() -> u8)> (*)[2]",
        "vb7a::W<(u8, fn() -> u8)> *",
        # A parameter list that opens with a fat raw pointer (verification of #103).
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
        # A parameter's `(*)` is not the declarator (verification of #103).
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
        # A fn pointer taking a reference has a binder (verification of #103).
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
        # Verification of #103: arrays of fn pointers returning a Box, and of references
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
        # Arrays of data pointers: what `[Box<Pt>; 2]` is (verification of #103).
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
    # #108: primitives, references and raw or fn pointers, and tuples or arrays of them.
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
    # Review of #106: `fn keep<i64>(h: i64)` called with a struct owning a Box.
    assert _may_own(mir) is False
    assert _may_own(mir, generics)
    # A reference never owns, whatever it points at.
    assert not _may_own(f"&{mir}", generics)
