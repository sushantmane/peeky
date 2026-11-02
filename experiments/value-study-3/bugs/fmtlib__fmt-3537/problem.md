Consider format_string as a string for enable-if
I was writing a forwarding wrapper around fmt, something like this (prints "ERROR: " and makes the text red):

```
template <typename... Args>
void error(fmt::format_string<Args...> fmt_str, Args&&... args) {
    const fmt::text_style ts = fmt::emphasis::bold | fg(fmt::color::red);
    fmt::print(stderr, ts, "ERROR: ");
    fmt::print(stderr, ts, fmt_str, std::forward<Args>(args)...);
    fmt::print(stderr, "\n");
}
```

That results in the following errors:

```
In file included from common/logging_test.cc:2:
./common/logging.h:28:5: error: no matching function for call to 'print'
    fmt::print(stderr, ts, fmt_str, std::forward<Args>(args)...);
    ^~~~~~~~~~
common/logging_test.cc:35:33: note: in instantiation of function template specialization 'error<int>' requested here
          EXPECT_NO_MEMORY_ALLOCATIONS(error("foo {}", 42));
                                       ^
external/com_github_fmtlib_fmt/include/fmt/core.h:3293:17: note: candidate function [with T = <const fmt::text_style &, fmt::basic_format_string<char, int> &, int>] not viable: no known conversion from 'FILE *' (aka '_IO_FILE *') to 'format_string<const fmt::text_style &, fmt::basic_format_string<char, int> &, int>' (aka 'basic_format_string<char, const fmt::text_style &, fmt::basic_format_string<char, int> &, int>') for 1st argument
FMT_INLINE void print(format_string<T...> fmt, T&&... args) {
                ^
external/com_github_fmtlib_fmt/include/fmt/core.h:3310:17: note: candidate function [with T = <fmt::basic_format_string<char, int> &, int>] not viable: no known conversion from 'const fmt::text_style' to 'format_string<fmt::basic_format_string<char, int> &, int>' (aka 'basic_format_string<char, fmt::basic_format_string<char, int> &, int>') for 2nd argument
FMT_INLINE void print(std::FILE* f, format_string<T...> fmt, T&&... args) {
                ^
external/com_github_fmtlib_fmt/include/fmt/color.h:508:6: note: candidate template ignored: requirement 'detail::is_string<fmt::basic_format_string<char, int>>::value' was not satisfied [with S = fmt::basic_format_string<char, int>, Args = <int>]
void print(std::FILE* f, const text_style& ts, const S& format_str,
     ^
external/com_github_fmtlib_fmt/include/fmt/color.h:527:6: note: candidate template ignored: requirement 'detail::is_string<fmt::text_style>::value' was not satisfied [with S = fmt::text_style, Args = <fmt::basic_format_string<char, int>, int>]
void print(const text_style& ts, const S& format_str, const Args&... args) {
     ^
```

The failure comes from the fact that the targeted overload (3rd candidate in the above error messages, taking `FILE*`, `const text_style&` and `const S&`) is ignored (by Sfinae) because `fmt::basic_format_string` is not a string (as checked by `detail::is_string`).

I can easily fix this in my code with an explicit conversion to string_view, with `static_cast<fmt::string_view>(fmt_str)`, but it seems like this should be supported, and that it would be an easy fix by just letting `basic_format_string` pass the `detail::is_string` test, unless there is some unintended side-effect I can't see.