# C++ candidates examined (in order)

| instance | outcome | note |
|---|---|---|
| nlohmann__json-4237 | accepted (E) | enum to_json always stores number_integer; control slot |
| fmtlib__fmt-3248 | skipped | category slot full (E: report states the rule, one-line fix in specs_setter::on_zero; format-test build+run ~20 s) |
| fmtlib__fmt-2317 | skipped | category slot full (E: hex-float path calls write_bytes, whose padding defaults to left alignment; simple state) |
| fmtlib__fmt-3901 | skipped | category slot full (E: group_digits formatter casts the signed value straight to unsigned; simple state) |
| fmtlib__fmt-3272 | accepted (C) | localized float padding counts separators over all significant digits (decimal_fp + digit_grouping); Release -O3 build; report blames the wrong values |
| fmtlib__fmt-3729 | skipped | category slot full (E: feature request for 'n'/'g' path presentation types; formatter<path> is short, simple state) |
| fmtlib__fmt-3863 | skipped | category slot full (E: feature request for C++23 's'/'?s' char-range formatting; clear from report, simple state) |
| fmtlib__fmt-3750 | skipped | category slot full (E: localized integer writer always formats decimal, ignoring the type; simple state) |
| fmtlib__fmt-2457 | skipped | category slot full (E: tuple_join_view formatter's parse ignores the specs; simple state) |
| fmtlib__fmt-3158 | accepted (C) | range_formatter::parse skips the element formatter, so nested char ranges lose debug quoting; nested formatter state; Release build |
| fmtlib__fmt-1683 | skipped | category slot full (E: printf_arg_formatter forces right alignment for char, overriding '-'; simple state) |
| fmtlib__fmt-2310 | skipped | category slot full (E: '0' flag sets numeric alignment and '0' fill, which write_nonfinite then applies to inf/nan; simple state) |

List exhausted: 3 of 4 accepted (1 E, 2 of 3 A-D slots filled); one C++ A-D slot is open.
