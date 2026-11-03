Inconsistent exit code handling in pipeline?
### Describe the bug

I just downloaded v0.98.0 to play around with it from the releases and noticed this seemingly inconsistent behavior.

If I understand it correctly, when running a script without a config Nushell is supposed to throw an error if an external command's exit code is non-zero and if the external command is the last in a pipeline, but that does not happen here.

### How to reproduce

Without a config, run each of these lines as a script separately
```nu
"1\n2" | grep -E "[[:alpha:]]" # throws error about exit code 1
"1\n2" | grep -E "[[:alpha:]]" | lines # prints empty list
"1\n2" | grep -E "[[:alpha:]]" | detect columns # throws error about exit code 1
```

### Expected behavior

I expected the final line to return `null` like it does on v0.97.1

### Configuration

| key                  | version                                           |
| ------------------ | ------------------------------------------- |
| version            | 0.98.0                                      |
| major              | 0                                           |
| minor              | 98                                          |
| patch              | 0                                           |
| branch             |                                             |
| commit_hash        | 6e1e824473e15eba246e1c43704c5d88fa237a17    |
| build_os           | linux-x86_64                                |
| build_target       | aarch64-unknown-linux-gnu                   |
| rust_version       | rustc 1.79.0 (129f3b996 2024-06-10)         |
| rust_channel       | 1.79.0-x86_64-unknown-linux-gnu             |
| cargo_version      | cargo 1.79.0 (ffa9cf99a 2024-06-03)         |
| build_time         | 2024-09-18 07:55:36 +00:00                  |
| build_rust_channel | release                                     |
| allocator          | mimalloc                                    |
| features           | default, sqlite, static-link-openssl, trash |
| installed_plugins  |                                             |