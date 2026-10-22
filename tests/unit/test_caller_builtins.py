import pytest

from peeky.native import _ADDRESSES


@pytest.mark.parametrize(
    "body",
    [
        "saved = __builtin_addressof(x);",
        "char *cfa = __builtin_dwarf_cfa();",  # #211 review, round 4
        "void *f = __builtin_frame_address(0);",
        "void *r = __builtin_return_address(0);",
        "__builtin_va_start(ap, x);",
        "__builtin_some_future_builtin(x);",
    ],
)
def test_a_builtin_not_known_safe_may_take_an_address(body):
    assert _ADDRESSES.search(body)


@pytest.mark.parametrize(
    "body",
    [
        "if (__builtin_expect(n > 0, 1)) return;",
        "__builtin_unreachable();",
        "return __builtin_popcountll(mask) + __builtin_clz(v);",
        "__builtin___memcpy_chk(d, s, n, __builtin_object_size(d, 0));",
        "if (__builtin_add_overflow(a, b, &c)) return;",
        "my__builtin_addressof(x);",
    ],
)
def test_builtins_that_reach_no_memory_are_not_counted(body):
    assert not _ADDRESSES.search(body)
