Default value not filled in on ignored error
### Please complete the following tasks

- [X] I have searched the [discussions](https://github.com/clap-rs/clap/discussions)
- [X] I have searched the [open](https://github.com/clap-rs/clap/issues) and [rejected](https://github.com/clap-rs/clap/issues?q=is%3Aissue+label%3AS-wont-fix+is%3Aclosed) issues

### Rust Version

rustc 1.83.0 (90b35a623 2024-11-26)

### Clap Version

4.5.23

### Minimal reproducible code

```rust
#!/usr/bin/env -S cargo +nightly -Zscript
---
[dependencies]
clap = { version = "4.5.23", features = ["derive"] }
---

use clap::{parser::ValueSource, CommandFactory};

#[derive(clap::Parser, Clone, Debug)]
pub struct Args {
    #[command(flatten)]
    global_args: GlobalArgs,
}

#[derive(clap::Args, Clone, Debug)]
struct GlobalArgs {
    #[arg(long)]
    ignore_immutable: bool,
}

fn get_value_source_of_ignore_immutable(args: &[&str]) -> ValueSource {
    let app = Args::command();
    let arg_matches = app
        .clone()
        .disable_version_flag(true)
        .disable_help_flag(true)
        .ignore_errors(true)
        .try_get_matches_from(args)
        .unwrap();
    arg_matches.value_source("ignore_immutable").unwrap()
}

fn main() {
    // --i is not parsed as ignore-immutable, so far so expected
    let source = get_value_source_of_ignore_immutable(&["jj", "--i"]);
    assert_eq!(source, ValueSource::DefaultValue);

    // This will fail, the actual value is ValueSource::CommandLine
    // 
    // --ig is somehow parsed as ignore-immutable? But the value is actually not set.
    // This prevents the default value from being set and results in an error later:
    //
    // "The following required argument was not provided: ignore_immutable"
    //
    let source = get_value_source_of_ignore_immutable(&["jj", "--ig"]);
    assert_eq!(source, ValueSource::DefaultValue);
}
```


### Steps to reproduce the bug with the above code

copy code to file, make file executable, execute file

### Actual Behaviour

```
thread 'main' panicked at .../main.rs:47:5:
assertion `left == right` failed
  left: CommandLine
 right: DefaultValue
```

### Expected Behaviour

asserts don't fail, `ignore_immutable` is properly filled in with default value.

### Additional Context

I think this is related to #5813 

I've run into the problem in a similar place as the original discussion there. But the error occors only when a global flag (e.g. `--ignore-immutable`) is _partially_ present.

### Debug Output

```
[clap_builder::builder::command]Command::_do_parse
[clap_builder::builder::command]Command::_build: name="main"
[clap_builder::builder::command]Command::_propagate:main
[clap_builder::builder::command]Command::_check_help_and_version:main expand_help_tree=false
[clap_builder::builder::command]Command::long_help_exists
[clap_builder::builder::command]Command::_propagate_global_args:main
[clap_builder::builder::debug_asserts]Command::_debug_asserts
[clap_builder::builder::debug_asserts]Arg::_debug_asserts:ignore_immutable
[clap_builder::builder::debug_asserts]Command::_verify_positionals
[clap_builder::parser::parser]Parser::get_matches_with
[clap_builder::parser::parser]Parser::parse
[clap_builder::parser::parser]Parser::get_matches_with: Begin parsing '"--i"'
[clap_builder::parser::parser]Parser::possible_subcommand: arg=Ok("--i")
[clap_builder::parser::parser]Parser::get_matches_with: sc=None
[clap_builder::parser::parser]Parser::parse_long_arg
[clap_builder::parser::parser]Parser::parse_long_arg: Does it contain '='...
[clap_builder::parser::parser]Parser::possible_long_flag_subcommand: arg="i"
[clap_builder::parser::parser]Parser::get_matches_with: After parse_long_arg NoMatchingArg { arg: "i" }
[clap_builder::parser::parser]Parser::did_you_mean_error: arg=i
[clap_builder::parser::parser]Parser::did_you_mean_error: longs=["ignore-immutable"]
[ clap_builder::output::usage]Usage::create_usage_with_title
[ clap_builder::output::usage]Usage::create_usage_no_title
[ clap_builder::output::usage]Usage::write_help_usage
[ clap_builder::output::usage]Usage::write_arg_usage; incl_reqs=true
[ clap_builder::output::usage]Usage::needs_options_tag
[ clap_builder::output::usage]Usage::needs_options_tag:iter: f=ignore_immutable
[clap_builder::builder::command]Command::groups_for_arg: id="ignore_immutable"
[ clap_builder::output::usage]Usage::needs_options_tag:iter:iter: grp_s="GlobalArgs"
[ clap_builder::output::usage]Usage::needs_options_tag:iter: [OPTIONS] required
[ clap_builder::output::usage]Usage::write_args: incls=[]
[ clap_builder::output::usage]Usage::get_args: unrolled_reqs=[]
[ clap_builder::output::usage]Usage::write_subcommand_usage
[ clap_builder::output::usage]Usage::create_usage_with_title: usage=Usage: jj [OPTIONS]
[clap_builder::builder::command]Command::color: Color setting...
[clap_builder::builder::command]Auto
[clap_builder::builder::command]Command::color: Color setting...
[clap_builder::builder::command]Auto
[clap_builder::parser::parser]Parser::add_defaults
[clap_builder::parser::parser]Parser::add_defaults:iter:ignore_immutable:
[clap_builder::parser::parser]Parser::add_default_value: doesn't have conditional defaults
[clap_builder::parser::parser]Parser::add_default_value:iter:ignore_immutable: has default vals
[clap_builder::parser::parser]Parser::add_default_value:iter:ignore_immutable: wasn't used
[clap_builder::parser::parser]Parser::react action=SetTrue, identifier=None, source=DefaultValue
[clap_builder::parser::arg_matcher]ArgMatcher::start_custom_arg: id="ignore_immutable", source=DefaultValue
[clap_builder::parser::parser]Parser::push_arg_values: ["false"]
[clap_builder::parser::parser]Parser::add_single_val_to_arg: cur_idx:=1
[clap_builder::builder::command]Command::_do_parse: ignoring error: error: unexpected argument '--i' found

Usage: jj [OPTIONS]

Backtrace:
   0: clap_builder::error::Backtrace::new
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:901:19
   1: clap_builder::error::Error<F>::new
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:140:28
   2: clap_builder::error::Error<F>::unknown_argument
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:716:23
   3: clap_builder::parser::parser::Parser::did_you_mean_error
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:1602:9
   4: clap_builder::parser::parser::Parser::parse
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:183:40
   5: clap_builder::parser::parser::Parser::get_matches_with
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:57:13
   6: clap_builder::builder::command::Command::_do_parse
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:4281:29
   7: clap_builder::builder::command::Command::try_get_matches_from_mut
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:857:9
   8: clap_builder::builder::command::Command::try_get_matches_from
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:767:9
   9: main::get_value_source_of_ignore_immutable
             at /home/remo/.cargo/target/93/ed3cdf6ba59cec/main.rs:24:23
  10: main::main
             at /home/remo/.cargo/target/93/ed3cdf6ba59cec/main.rs:36:18
  11: core::ops::function::FnOnce::call_once
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/core/src/ops/function.rs:250:5
  12: std::sys::backtrace::__rust_begin_short_backtrace
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/sys/backtrace.rs:152:18
  13: std::rt::lang_start::{{closure}}
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:195:18
  14: core::ops::function::impls::<impl core::ops::function::FnOnce<A> for &F>::call_once
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/core/src/ops/function.rs:284:13
      std::panicking::try::do_call
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:584:40
      std::panicking::try
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:547:19
      std::panic::catch_unwind
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panic.rs:358:14
      std::rt::lang_start_internal::{{closure}}
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:174:48
      std::panicking::try::do_call
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:584:40
      std::panicking::try
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:547:19
      std::panic::catch_unwind
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panic.rs:358:14
      std::rt::lang_start_internal
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:174:20
  15: std::rt::lang_start
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:194:17
  16: main
  17: __libc_start_call_main
  18: __libc_start_main_alias_1
  19: _start


[clap_builder::parser::arg_matcher]ArgMatcher::get_global_values: global_arg_vec=[]
[clap_builder::builder::command]Command::_do_parse
[clap_builder::builder::command]Command::_build: name="main"
[clap_builder::builder::command]Command::_propagate:main
[clap_builder::builder::command]Command::_check_help_and_version:main expand_help_tree=false
[clap_builder::builder::command]Command::long_help_exists
[clap_builder::builder::command]Command::_propagate_global_args:main
[clap_builder::builder::debug_asserts]Command::_debug_asserts
[clap_builder::builder::debug_asserts]Arg::_debug_asserts:ignore_immutable
[clap_builder::builder::debug_asserts]Command::_verify_positionals
[clap_builder::parser::parser]Parser::get_matches_with
[clap_builder::parser::parser]Parser::parse
[clap_builder::parser::parser]Parser::get_matches_with: Begin parsing '"--ig"'
[clap_builder::parser::parser]Parser::possible_subcommand: arg=Ok("--ig")
[clap_builder::parser::parser]Parser::get_matches_with: sc=None
[clap_builder::parser::parser]Parser::parse_long_arg
[clap_builder::parser::parser]Parser::parse_long_arg: Does it contain '='...
[clap_builder::parser::parser]Parser::possible_long_flag_subcommand: arg="ig"
[clap_builder::parser::parser]Parser::get_matches_with: After parse_long_arg NoMatchingArg { arg: "ig" }
[clap_builder::parser::parser]Parser::did_you_mean_error: arg=ig
[clap_builder::parser::parser]Parser::did_you_mean_error: longs=["ignore-immutable"]
[clap_builder::parser::parser]Parser::remove_overrides: id="ignore_immutable"
[clap_builder::parser::arg_matcher]ArgMatcher::start_custom_arg: id="ignore_immutable", source=CommandLine
[clap_builder::builder::command]Command::groups_for_arg: id="ignore_immutable"
[clap_builder::parser::arg_matcher]ArgMatcher::start_custom_arg: id="GlobalArgs", source=CommandLine
[ clap_builder::output::usage]Usage::create_usage_with_title
[ clap_builder::output::usage]Usage::create_usage_no_title
[ clap_builder::output::usage]Usage::create_smart_usage
[ clap_builder::output::usage]Usage::write_arg_usage; incl_reqs=true
[ clap_builder::output::usage]Usage::write_args: incls=["ignore_immutable", "GlobalArgs"]
[ clap_builder::output::usage]Usage::get_args: unrolled_reqs=[]
[clap_builder::builder::command]Command::unroll_args_in_group: group="GlobalArgs"
[clap_builder::builder::command]Command::unroll_args_in_group:iter: entity="ignore_immutable"
[clap_builder::builder::command]Command::unroll_args_in_group:iter: this is an arg
[clap_builder::builder::command]Command::unroll_args_in_group: group="GlobalArgs"
[clap_builder::builder::command]Command::unroll_args_in_group:iter: entity="ignore_immutable"
[clap_builder::builder::command]Command::unroll_args_in_group:iter: this is an arg
[ clap_builder::output::usage]Usage::create_usage_with_title: usage=Usage: jj <--ignore-immutable>
[clap_builder::builder::command]Command::color: Color setting...
[clap_builder::builder::command]Auto
[clap_builder::builder::command]Command::color: Color setting...
[clap_builder::builder::command]Auto
[clap_builder::parser::parser]Parser::add_defaults
[clap_builder::parser::parser]Parser::add_defaults:iter:ignore_immutable:
[clap_builder::parser::parser]Parser::add_default_value: doesn't have conditional defaults
[clap_builder::parser::parser]Parser::add_default_value:iter:ignore_immutable: has default vals
[clap_builder::parser::parser]Parser::add_default_value:iter:ignore_immutable: was used
[clap_builder::builder::command]Command::_do_parse: ignoring error: error: unexpected argument '--ig' found

  tip: a similar argument exists: '--ignore-immutable'

Usage: jj <--ignore-immutable>

Backtrace:
   0: clap_builder::error::Backtrace::new
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:901:19
   1: clap_builder::error::Error<F>::new
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:140:28
   2: clap_builder::error::Error<F>::unknown_argument
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/error/mod.rs:716:23
   3: clap_builder::parser::parser::Parser::did_you_mean_error
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:1602:9
   4: clap_builder::parser::parser::Parser::parse
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:183:40
   5: clap_builder::parser::parser::Parser::get_matches_with
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/parser/parser.rs:57:13
   6: clap_builder::builder::command::Command::_do_parse
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:4281:29
   7: clap_builder::builder::command::Command::try_get_matches_from_mut
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:857:9
   8: clap_builder::builder::command::Command::try_get_matches_from
             at /home/remo/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/clap_builder-4.5.23/src/builder/command.rs:767:9
   9: main::get_value_source_of_ignore_immutable
             at /home/remo/.cargo/target/93/ed3cdf6ba59cec/main.rs:24:23
  10: main::main
             at /home/remo/.cargo/target/93/ed3cdf6ba59cec/main.rs:46:18
  11: core::ops::function::FnOnce::call_once
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/core/src/ops/function.rs:250:5
  12: std::sys::backtrace::__rust_begin_short_backtrace
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/sys/backtrace.rs:152:18
  13: std::rt::lang_start::{{closure}}
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:195:18
  14: core::ops::function::impls::<impl core::ops::function::FnOnce<A> for &F>::call_once
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/core/src/ops/function.rs:284:13
      std::panicking::try::do_call
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:584:40
      std::panicking::try
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:547:19
      std::panic::catch_unwind
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panic.rs:358:14
      std::rt::lang_start_internal::{{closure}}
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:174:48
      std::panicking::try::do_call
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:584:40
      std::panicking::try
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panicking.rs:547:19
      std::panic::catch_unwind
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/panic.rs:358:14
      std::rt::lang_start_internal
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:174:20
  15: std::rt::lang_start
             at /rustc/3f43b1a636738f41c48df073c5bcb97a97bf8459/library/std/src/rt.rs:194:17
  16: main
  17: __libc_start_call_main
  18: __libc_start_main_alias_1
  19: _start


[clap_builder::parser::arg_matcher]ArgMatcher::get_global_values: global_arg_vec=[]

thread 'main' panicked at /home/remo/.cargo/target/93/ed3cdf6ba59cec/main.rs:47:5:
assertion `left == right` failed
  left: CommandLine
 right: DefaultValue
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
```