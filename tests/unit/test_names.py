from peeky.names import display_function


def test_rust_frames_read_as_describe_shows_them():
    assert display_function("topk::insert_sorted::h0123456789abcdef") == "topk::insert_sorted"
    closure = "topk::insert_sorted::_$u7b$$u7b$closure$u7d$$u7d$::h0123456789abcdef"
    assert display_function(closure) == "topk::insert_sorted::{{closure}}"
    trait = "_$LT$topk..Item$u20$as$u20$core..fmt..Debug$GT$::fmt::hfedcba9876543210"
    assert display_function(trait) == "<topk::Item as core::fmt::Debug>::fmt"


def test_other_languages_are_unchanged():
    java = "Restock.lambda$needsRestock$0(Restock$Item)"
    assert display_function(java) == java
    assert display_function("Ring::push(int)") == "Ring::push(int)"
    assert display_function(None) == ""
