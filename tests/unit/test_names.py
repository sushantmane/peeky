import pytest

from peeky.names import display_function, function_labels, short_function


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


def test_impossible_escapes_stay_as_written():
    name = "odd::_$uffffff$$ud800$x::h0123456789abcdef"
    assert display_function(name) == "odd::$uffffff$$ud800$x"


def test_an_optimized_rust_frame_reads_as_on_linux():
    # lldb-dap on macOS appends ` [opt]` to an optimized frame's name.
    assert display_function("topk::insert_sorted::h0123456789abcdef [opt]") == "topk::insert_sorted"
    assert display_function("main [opt]") == "main [opt]"


# Eval 6, finding 16: a C++ frame's signature (`trim_left(std::__1::basic_string<char,
# std::__1::char_traits<char>, std::__1::allocator<char>> const&)`) doubled a table's width.
LIBCXX_STRING = (
    "std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>>"
)
LIBSTDCXX_STRING = "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >"


@pytest.mark.parametrize(
    "name, shown",
    [
        (f"trim_left({LIBCXX_STRING} const&)", "trim_left(std::string const&)"),
        (f"trim_left({LIBSTDCXX_STRING} const&)", "trim_left(std::string const&)"),
        (
            "sum(std::__1::vector<int, std::__1::allocator<int>> const&)",
            "sum(std::vector<int> const&)",
        ),
        (
            f"f(std::__1::map<{LIBCXX_STRING}, long, std::__1::less<{LIBCXX_STRING}>, "
            f"std::__1::allocator<std::__1::pair<{LIBCXX_STRING} const, long>>>&)",
            "f(std::map<std::string, long>&)",
        ),
        (
            "g(std::unordered_map<int, int, std::hash<int>, std::equal_to<int>, "
            "std::allocator<std::pair<int const, int> > >&)",
            "g(std::unordered_map<int, int>&)",
        ),
        # A comparator that is not the default stays.
        (
            "k(std::map<int, int, std::greater<int>, std::allocator<std::pair<int const, int> > >)",
            "k(std::map<int, int, std::greater<int>>)",
        ),
        (
            "h(std::__1::unique_ptr<Node, std::__1::default_delete<Node>>)",
            "h(std::unique_ptr<Node>)",
        ),
        (
            "operator<<(std::__1::basic_ostream<char, std::__1::char_traits<char>>&, Foo const&)",
            "operator<<(std::ostream&, Foo const&)",
        ),
        (
            "std::vector<int, MyAllocator<int> >::size() const",
            "std::vector<int, MyAllocator<int>>::size() const",
        ),
        # Brackets that do not balance: as written.
        ("odd(std::vector<int)", "odd(std::vector<int)"),
    ],
)
def test_a_cpp_signature_names_standard_types_as_code_does(name, shown):
    assert display_function(name) == shown


@pytest.mark.parametrize(
    "name, short",
    [
        (f"trim_left({LIBCXX_STRING} const&)", "trim_left"),
        ("int same<int>(int)", "same<int>"),
        (f"{LIBCXX_STRING} make<int>(int)", "make<int>"),
        ("geo::Box::area(int) const", "geo::Box::area"),
        ("Box<int>::get() const &", "Box<int>::get"),
        ("main::$_0::operator()(int) const", "main::$_0::operator()"),
        ("main::{lambda(int)#1}::operator()(int) const", "main::{lambda(int)#1}::operator()"),
        ("operator<<(std::ostream&, Foo const&)", "operator<<"),
        ("operator<(Foo const&, Foo const&)", "operator<"),
        ("Foo::operator bool() const", "Foo::operator bool"),
        ("Foo::operator new(unsigned long)", "Foo::operator new"),
        ("(anonymous namespace)::helper(int)", "(anonymous namespace)::helper"),
        ("f(int) [opt]", "f [opt]"),
        ("main", "main"),
        ("topk::insert_sorted::h0123456789abcdef", "topk::insert_sorted"),
        # Java keeps its own short signature; Python's names have none.
        ("Main.main(String[])", "Main.main(String[])"),
        ("<module>", "<module>"),
        (None, ""),
    ],
)
def test_a_table_shows_a_frame_without_its_parameters(name, short):
    assert short_function(name) == short


def test_overloads_in_one_table_keep_their_parameters():
    one, other = "twice(int)", f"twice({LIBCXX_STRING} const&)"
    labels = function_labels([one, other, "main", one, None])
    assert labels == {one: "twice(int)", other: "twice(std::string const&)", "main": "main"}
    assert function_labels([f"trim_left({LIBCXX_STRING} const&)"]) == {
        f"trim_left({LIBCXX_STRING} const&)": "trim_left"
    }
    # Names alike even with their parameters spelled as code does: in full.
    alike = ["f(std::string)", f"f({LIBCXX_STRING})"]
    assert function_labels(alike) == {name: name for name in alike}


def test_the_note_says_how_sql_matches_a_short_name():
    from peeky.names import shortened_note

    assert shortened_note({"main": "main"}) is None
    labels = function_labels(["main", "twice(int)", f"twice({LIBCXX_STRING} const&)"])
    note = shortened_note(labels)
    assert "short_function(function) = 'twice'" in note and "whole name" in note
    assert "''" in shortened_note({"o'k(int)": "o'k"})
    # #151 review 4: a closure or generic instance the table shows is matched by
    # `instance`; short_function merges a function's closures.
    note = shortened_note({"cz::main::h0123456789abcdef": "cz::main"}, "cz::main::{closure#0}")
    assert "short_function(function) = 'cz::main'" in note, note
    assert "instance = 'cz::main::{closure#0}'" in note, note
    note = shortened_note({}, "cz::gen_apply<i32>")
    assert note.endswith(
        "use instance = 'cz::gen_apply<i32>' for a closure or generic instance as shown "
        "(short_function merges a function's closures and a generic's instances)"
    ), note


def test_a_gcc_static_function_reads_as_clangs():
    # Twelfth evaluation's preparation: GCC's `static int list(...)` showed as `::list` in
    # the function column and in the note's `short_function(function) = '::list'`.
    from peeky.names import shortened_note

    gcc = "::list(const std::vector<int, std::allocator<int> > &)"
    assert display_function(gcc) == "list(const std::vector<int> &)"
    assert short_function(gcc) == "list"
    assert function_labels([gcc, "main"]) == {gcc: "list", "main": "main"}
    assert "short_function(function) = 'list'" in shortened_note(function_labels([gcc]))
    # A name of a namespace's own keeps it; `::` alone, or before no name, stays.
    assert short_function("::geo::area(int)") == "geo::area"
    assert display_function("::") == "::" and display_function("::(x)") == "::(x)"
