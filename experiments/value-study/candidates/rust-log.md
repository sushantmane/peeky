# Rust candidates examined (in order)

| instance | decision | note |
|---|---|---|
| uutils__coreutils-6690 | accepted (E) | control slot; zsh completion for cp: PATHS arg lacks num_args/required; build 1.4 min, grade 4 s |
| astral-sh__ruff-15543 | skipped | category slot full (E: UP028 fix text lacks parens for an unparenthesized tuple, clear from report and code; edit-build-test 11 s, not A) |
| tokio-rs__tokio-6752 | accepted (D) | DelayQueue::remove of the last item never wakes the waker parked by poll_expired; build 0.3 min, grade 1 s |
| tokio-rs__axum-1934 | accepted (D) | fallback Router with a service nested at "/" panics on MatchedPath debug_assert; build 0.9 min, grade 6 s |
| sharkdp__bat-3108 | accepted (B) | `--plain`/`--paging` declared with clap overrides_with each other, so one resets the other; build 0.8 min, grade 2 s |
| nushell__nushell-13605 | skipped | category slot full (E: `ls -f` leaves the symlink `target` column relative; clear from report and dir_entry_dict; edit-build-test 34 s, not A) |
| burntsushi__ripgrep-2209 | accepted (D) | -U with --replace: printer replacer ignores the match range end and copies the rest of the buffer; build 0.4 min, grade 1 s |
| uutils__coreutils-6377 | skipped | category slot full (E: feature request, implement `env --ignore-signal`; what is missing is clear from the report and env.rs; coreutils edit-build-test is seconds, not A) |
| astral-sh__ruff-15330 | skipped | category slot full (E: ERA001 script-metadata block never closes when a `#` line follows `# ///`; clear from report and commented_out_code.rs; ruff edit-build-test ~11 s, not A) |
| tokio-rs__tokio-4867 | accepted (C) | broadcast "closed" is a marker slot in the ring buffer that a resubscribed receiver starts after; build 0.4 min, grade 1 s |
