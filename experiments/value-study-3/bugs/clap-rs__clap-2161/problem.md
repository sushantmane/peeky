Obscure panic with heading and default value
### Make sure you completed the following tasks

- [x] Searched the [discussions](https://github.com/clap-rs/clap/discussions)
- [x] Searched the closes issues

### Code

```rust
use clap::Clap;

fn main() {
	#[derive(Clap)]
	#[clap(help_heading = "HEAD")]
	struct Opt2 {
		#[clap(long, name = "size")]
		pub b: Option<usize>,
	}
	#[derive(Clap)]
	struct Opt {
		#[clap(flatten)]
		opt2: Opt2,

		#[clap(long, default_value = "32")]
		a: i32,
	}
	Opt::parse_from(&["test"]);
}
```

### Steps to reproduce the issue

1. Run `cargo run`

### Version

* **Rust**: rustc 1.46.0-nightly (346aec9b0 2020-07-11)
* **Clap**: master at 48d308a8ab9e96d4b21336e428feee420dbac51d

### Actual Behavior Summary

Code is panicking with
```
thread 'auto_default_value' panicked at 'called `Option::unwrap()` on a `None` value', clap_derive/tests/default_arg_value.rs:18:12
...
  13: core::panicking::panic
             at src/libcore/panicking.rs:50
  14: core::option::Option<T>::unwrap
             at /home/lach/.rustup/toolchains/nightly-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/src/libcore/macros/mod.rs:10
  15: <default_arg_value::auto_default_value::Opt as clap::derive::FromArgMatches>::from_arg_matches
             at clap_derive/tests/default_arg_value.rs:18
  16: clap::derive::Clap::parse_from
             at /home/lach/jsonnet-rs/clap/src/derive.rs:29
  17: default_arg_value::auto_default_value
             at clap_derive/tests/default_arg_value.rs:20
  18: default_arg_value::auto_default_value::{{closure}}
             at clap_derive/tests/default_arg_value.rs:5
  19: core::ops::function::FnOnce::call_once
             at /home/lach/.rustup/toolchains/nightly-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/src/libcore/ops/function.rs:233
  20: <alloc::boxed::Box<F> as core::ops::function::FnOnce<A>>::call_once
             at /rustc/346aec9b02f3c74f3fce97fd6bda24709d220e49/src/liballoc/boxed.rs:1081
  21: <std::panic::AssertUnwindSafe<F> as core::ops::function::FnOnce<()>>::call_once
             at /rustc/346aec9b02f3c74f3fce97fd6bda24709d220e49/src/libstd/panic.rs:318
  22: std::panicking::try::do_call
             at /rustc/346aec9b02f3c74f3fce97fd6bda24709d220e49/src/libstd/panicking.rs:348
  23: std::panicking::try
             at /rustc/346aec9b02f3c74f3fce97fd6bda24709d220e49/src/libstd/panicking.rs:325
  24: std::panic::catch_unwind
             at /rustc/346aec9b02f3c74f3fce97fd6bda24709d220e49/src/libstd/panic.rs:394
  25: test::run_test_in_process
             at src/libtest/lib.rs:541
  26: test::run_test::run_test_inner::{{closure}}
             at src/libtest/lib.rs:450
```

**If a project of yours is blocked due to this bug, please, mention it explicitly.**

Found this while extracting argument parsing from main crate in https://github.com/CertainLach/jsonnet-rs

### Expected Behavior Summary

It shouldn't panic, and successfully parse input

### Additional context

`default_arg_value.rs:18:12` points at `a: i32` line

With no `default_value` on `a: i32`, or  without `help_heading` on `Opt2` struct it works as expected

### Debug output

Compile clap with `debug` feature:

```toml
[dependencies]
clap = { version = "*", features = ["debug"] }
```

The output may be very long, so feel free to link to a gist or attach a text file

<details>
<summary> Debug Output </summary>
<pre>
<code>
[            clap::build::app]  App::_do_parse
[            clap::build::app]  App::_build
[            clap::build::app]  App::_derive_display_order:clap_derive
[            clap::build::app]  App::_create_help_and_version
[            clap::build::app]  App::_create_help_and_version: Building --help
[            clap::build::app]  App::_create_help_and_version: Building --version
[            clap::build::app]  App::_debug_asserts
[            clap::build::arg]  Arg::_debug_asserts:size
[            clap::build::arg]  Arg::_debug_asserts:a
[            clap::build::arg]  Arg::_debug_asserts:help
[            clap::build::arg]  Arg::_debug_asserts:version
[         clap::parse::parser]  Parser::get_matches_with
[         clap::parse::parser]  Parser::_build
[         clap::parse::parser]  Parser::_verify_positionals
[         clap::parse::parser]  Parser::remove_overrides
[      clap::parse::validator]  Validator::validate
[         clap::parse::parser]  Parser::add_defaults
[      clap::parse::validator]  Validator::validate_conflicts
[      clap::parse::validator]  Validator::validate_exclusive
[      clap::parse::validator]  Validator::gather_conflicts
[      clap::parse::validator]  Validator::validate_required: required=ChildGraph([])
[      clap::parse::validator]  Validator::gather_requirements
[      clap::parse::validator]  Validator::validate_required_unless
[      clap::parse::validator]  Validator::validate_matched_args
[    clap::parse::arg_matcher]  ArgMatcher::get_global_values: global_arg_vec=[]
</code>
</pre>
</details>