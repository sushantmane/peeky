"""The Box gate's type tests: LLDB's C-style declarators and the MIR's non-owning types."""

import pytest

from peeky.rust import _NON_OWNING, _is_pointer


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
    ],
)
def test_top_level_pointers(kind):
    assert _is_pointer(kind)


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
    ],
)
def test_arrays_and_non_pointers(kind):
    assert not _is_pointer(kind)


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
