cannot specify fields and columns in record and table i/o type annotations
### Describe the bug

i wanted to write some interesting commands to play with the new features from the input/output type annotations.
we can use `table` and `record` but the parser is not happy when we try to specify the types of columns and fields respectively :thinking: 

### How to reproduce

## tables
let's say i want all the names of the files in a given table, that could look like
```nushell
def get_file_name []: table<type: string, name: string> -> table<name: string> {
    where type == file | get name
}
```
however, i get
```
Error: nu::parser::unclosed_delimiter

  × Unclosed delimiter.
   ╭─[entry #5:1:1]
 1 │ def get_file_name []: table<type: string, name: string> -> table<name: string> {
   ·                       ─────┬─────
   ·                            ╰── unclosed >
 2 │     where type == file | get name
   ╰────
```

## records
the exact same thing goes with record, e.g. with merging two records
```nushell
def merge_records [other: record<bar: int>]: record<foo: string> -> record<foo: string, bar: int> {
    merge $other
}
```

### Expected behavior

## tables
i'd like to get no error and the following output in the root of the Nushell repo
```nushell
> ls | get_file_name
╭───┬─────────────────────╮
│ 0 │ CODE_OF_CONDUCT.md  │
│ 1 │ CONTRIBUTING.md     │
│ 2 │ Cargo.lock          │
│ 3 │ Cargo.toml          │
│ 4 │ Cross.toml          │
│ 5 │ LICENSE             │
│ 6 │ README.md           │
│ 7 │ codecov.yml         │
│ 8 │ rust-toolchain.toml │
│ 9 │ toolkit.nu          │
╰───┴─────────────────────╯
```

> in order to fix this, i need to remove the annotations, either with
> ```nushell
> def get_file_name []: table -> table {
>     where type == file | get name
> }
> ```
> or
> ```nushell
> def get_file_name []: table<> -> table<> {
>     where type == file | get name
> }
> ```

## records
same expected behaviour
> same kind of fix

### Screenshots

_No response_

### Configuration

| key                | value                                    |
| ------------------ | ---------------------------------------- |
| version            | 0.82.1                                   |
| branch             | main                                     |
| commit_hash        | 57d96c09fa78f6231093cb241da4ddbd545f84bf |
| build_os           | linux-x86_64                             |
| build_target       | x86_64-unknown-linux-gnu                 |
| rust_version       | rustc 1.69.0 (84c898d65 2023-04-16)      |
| rust_channel       | 1.69.0-x86_64-unknown-linux-gnu          |
| cargo_version      | cargo 1.69.0 (6e9a83356 2023-04-12)      |
| build_time         | 2023-07-15 12:59:41 +02:00               |
| build_rust_channel | release                                  |
| allocator          | standard                                 |
| features           | default, sqlite, trash, which, zip       |
| installed_plugins  | hist                                     |

### Additional context

_No response_