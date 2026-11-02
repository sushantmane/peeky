UTF-8 truncation
I've done some tests with UTF-8 strings, and think string formatting with precision doesn't work as expected.

fmt::format(u8"{:.4}", u8"cafés");

The format call above, even if done properly with char8_t based strings returns "caf\xc3" instead of "café", where "\xc3" is the first byte of the UTF-8 sequence "\xc3\xa9" for "é".