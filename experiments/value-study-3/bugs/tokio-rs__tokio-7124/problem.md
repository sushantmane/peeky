Use `[lints.rust.unexpected_cfgs.check-cfg]` instead of hacky check-cfg workaround
With the release of rust-lang/cargo#13913 (in nightly-2024-05-19), there is no longer any need for the kind of workarounds employed in [#6538](https://github.com/tokio-rs/tokio/pull/6538). Cargo has gain the ability to declare `--check-cfg` args directly inside the `[lints]` table with [`[lints.rust.unexpected_cfgs.check-cfg]`](https://doc.rust-lang.org/nightly/rustc/check-cfg/cargo-specifics.html#check-cfg-in-lintsrust-table)[^1]:

`Cargo.toml`:
```toml
[lints.rust]
unexpected_cfgs = { level = "warn", check-cfg = ['cfg(foo)'] }
```

> Note that the diagnostic output of the lint has been updated to suggest the `[lints]` approach first. You can use it to guide you through the `--check-cfg` arguments that may need to be added.

[^1]: take effect on Rust 1.80 (current nightly), is ignored on Rust 1.79 (current beta), and produce an unused warning below

_Originally posted by @Urgau in https://github.com/tokio-rs/tokio/issues/6538#issuecomment-2128036174_