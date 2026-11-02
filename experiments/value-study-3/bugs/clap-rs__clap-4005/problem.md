Argument that requires another argument in an argument group gives empty error
### Please complete the following tasks

- [X] I have searched the [discussions](https://github.com/clap-rs/clap/discussions)
- [X] I have searched the [open](https://github.com/clap-rs/clap/issues) and [rejected](https://github.com/clap-rs/clap/issues?q=is%3Aissue+label%3AS-wont-fix+is%3Aclosed) issues

### Rust Version

rustc 1.64.0-nightly (9067d5277 2022-07-28)

### Clap Version

3.2.15

### Minimal reproducible code

```rust
use clap::{ArgGroup, Parser};

#[derive(Parser, Debug)]
#[clap(author, version, about, long_about = None)]
#[clap(group(ArgGroup::new("either_or_both").multiple(true).required(true).args(&["first", "second"])))]
struct Args {
    #[clap(short, long, requires("first"))]
    require_first: bool,
    #[clap(short, long)]
    first: bool,
    #[clap(short, long)]
    second: bool,
}
fn main() {
    let args: Args = Args::parse();

    dbg!(args);
}
```


### Steps to reproduce the bug with the above code

`cargo run -- --require-first --second`

### Actual Behaviour

```
error: The following required arguments were not provided:

USAGE:
    clap_example --require-first <--first|--second>

For more information try --help
```

### Expected Behaviour

Error message should provide context, e.g.
```
error: The following required arguments were not provided:
    --first

USAGE:
    clap_example --require-first <--first|--second>

For more information try --help
```

### Additional Context

Removing the `multiple(true)` part unintuitively works:
```
[src/main.rs:17] args = Args {
    require_first: true,
    first: false,
    second: true,
}
```
This leads me to believe I'm probably misusing `ArgGroup` in this case, however the error message should still say _something_ IMO.

### Debug Output

```
[      clap::builder::command] 	Command::_do_parse
[      clap::builder::command] 	Command::_build: name="clap_example"
[      clap::builder::command] 	Command::_propagate:clap_example
[      clap::builder::command] 	Command::_check_help_and_version: clap_example
[      clap::builder::command] 	Command::_propagate_global_args:clap_example
[      clap::builder::command] 	Command::_derive_display_order:clap_example
[clap::builder::debug_asserts] 	Command::_debug_asserts
[clap::builder::debug_asserts] 	Arg::_debug_asserts:help
[clap::builder::debug_asserts] 	Arg::_debug_asserts:version
[clap::builder::debug_asserts] 	Arg::_debug_asserts:require-first
[clap::builder::debug_asserts] 	Arg::_debug_asserts:first
[clap::builder::debug_asserts] 	Arg::_debug_asserts:second
[clap::builder::debug_asserts] 	Command::_verify_positionals
[        clap::parser::parser] 	Parser::get_matches_with
[        clap::parser::parser] 	Parser::get_matches_with: Begin parsing 'RawOsStr("--require-first")' ([45, 45, 114, 101, 113, 117, 105, 114, 101, 45, 102, 105, 114, 115, 116])
[        clap::parser::parser] 	Parser::possible_subcommand: arg=Ok("--require-first")
[        clap::parser::parser] 	Parser::get_matches_with: sc=None
[        clap::parser::parser] 	Parser::parse_long_arg
[        clap::parser::parser] 	Parser::parse_long_arg: Does it contain '='...
[        clap::parser::parser] 	Parser::parse_long_arg: Found valid arg or flag '--require-first'
[        clap::parser::parser] 	Parser::parse_long_arg("require-first"): Presence validated
[        clap::parser::parser] 	Parser::react action=IncOccurrence, identifier=Some(Long), source=CommandLine
[        clap::parser::parser] 	Parser::react: cur_idx:=1
[        clap::parser::parser] 	Parser::remove_overrides: id=require-first
[   clap::parser::arg_matcher] 	ArgMatcher::start_occurrence_of_arg: id=require-first
[      clap::builder::command] 	Command::groups_for_arg: id=require-first
[        clap::parser::parser] 	Parser::get_matches_with: After parse_long_arg ValuesDone
[        clap::parser::parser] 	Parser::get_matches_with: Begin parsing 'RawOsStr("--second")' ([45, 45, 115, 101, 99, 111, 110, 100])
[        clap::parser::parser] 	Parser::possible_subcommand: arg=Ok("--second")
[        clap::parser::parser] 	Parser::get_matches_with: sc=None
[        clap::parser::parser] 	Parser::parse_long_arg
[        clap::parser::parser] 	Parser::parse_long_arg: Does it contain '='...
[        clap::parser::parser] 	Parser::parse_long_arg: Found valid arg or flag '--second'
[        clap::parser::parser] 	Parser::parse_long_arg("second"): Presence validated
[        clap::parser::parser] 	Parser::react action=IncOccurrence, identifier=Some(Long), source=CommandLine
[        clap::parser::parser] 	Parser::react: cur_idx:=2
[        clap::parser::parser] 	Parser::remove_overrides: id=second
[   clap::parser::arg_matcher] 	ArgMatcher::start_occurrence_of_arg: id=second
[      clap::builder::command] 	Command::groups_for_arg: id=second
[   clap::parser::arg_matcher] 	ArgMatcher::start_occurrence_of_group: id=either_or_both
[        clap::parser::parser] 	Parser::get_matches_with: After parse_long_arg ValuesDone
[        clap::parser::parser] 	Parser::add_defaults
[        clap::parser::parser] 	Parser::add_defaults:iter:help:
[        clap::parser::parser] 	Parser::add_default_value:iter:help: doesn't have default missing vals
[        clap::parser::parser] 	Parser::add_default_value: doesn't have conditional defaults
[        clap::parser::parser] 	Parser::add_default_value:iter:help: doesn't have default vals
[        clap::parser::parser] 	Parser::add_defaults:iter:version:
[        clap::parser::parser] 	Parser::add_default_value:iter:version: doesn't have default missing vals
[        clap::parser::parser] 	Parser::add_default_value: doesn't have conditional defaults
[        clap::parser::parser] 	Parser::add_default_value:iter:version: doesn't have default vals
[        clap::parser::parser] 	Parser::add_defaults:iter:require-first:
[        clap::parser::parser] 	Parser::add_default_value:iter:require-first: doesn't have default missing vals
[        clap::parser::parser] 	Parser::add_default_value: doesn't have conditional defaults
[        clap::parser::parser] 	Parser::add_default_value:iter:require-first: doesn't have default vals
[        clap::parser::parser] 	Parser::add_defaults:iter:first:
[        clap::parser::parser] 	Parser::add_default_value:iter:first: doesn't have default missing vals
[        clap::parser::parser] 	Parser::add_default_value: doesn't have conditional defaults
[        clap::parser::parser] 	Parser::add_default_value:iter:first: doesn't have default vals
[        clap::parser::parser] 	Parser::add_defaults:iter:second:
[        clap::parser::parser] 	Parser::add_default_value:iter:second: doesn't have default missing vals
[        clap::parser::parser] 	Parser::add_default_value: doesn't have conditional defaults
[        clap::parser::parser] 	Parser::add_default_value:iter:second: doesn't have default vals
[     clap::parser::validator] 	Validator::validate
[     clap::parser::validator] 	Validator::validate_conflicts
[     clap::parser::validator] 	Validator::validate_exclusive
[     clap::parser::validator] 	Validator::validate_exclusive:iter:require-first
[     clap::parser::validator] 	Validator::validate_exclusive:iter:second
[     clap::parser::validator] 	Validator::validate_exclusive:iter:either_or_both
[     clap::parser::validator] 	Validator::validate_conflicts::iter: id=require-first
[     clap::parser::validator] 	Conflicts::gather_conflicts: arg=require-first
[      clap::builder::command] 	Command::groups_for_arg: id=require-first
[     clap::parser::validator] 	Conflicts::gather_direct_conflicts id=require-first, conflicts=[]
[      clap::builder::command] 	Command::groups_for_arg: id=second
[     clap::parser::validator] 	Conflicts::gather_direct_conflicts id=second, conflicts=[]
[     clap::parser::validator] 	Conflicts::gather_direct_conflicts id=either_or_both, conflicts=[]
[     clap::parser::validator] 	Conflicts::gather_conflicts: conflicts=[]
[     clap::parser::validator] 	Validator::validate_conflicts::iter: id=second
[     clap::parser::validator] 	Conflicts::gather_conflicts: arg=second
[     clap::parser::validator] 	Conflicts::gather_conflicts: conflicts=[]
[     clap::parser::validator] 	Validator::validate_required: required=ChildGraph([Child { id: either_or_both, children: [] }])
[     clap::parser::validator] 	Validator::gather_requires
[     clap::parser::validator] 	Validator::gather_requires:iter:require-first
[     clap::parser::validator] 	Validator::gather_requires:iter:second
[     clap::parser::validator] 	Validator::gather_requires:iter:either_or_both
[     clap::parser::validator] 	Validator::gather_requires:iter:either_or_both:group
[     clap::parser::validator] 	Validator::validate_required: is_exclusive_present=false
[     clap::parser::validator] 	Validator::validate_required:iter:aog=first
[     clap::parser::validator] 	Validator::validate_required:iter: This is an arg
[     clap::parser::validator] 	Validator::is_missing_required_ok: first
[     clap::parser::validator] 	Conflicts::gather_conflicts: arg=first
[      clap::builder::command] 	Command::groups_for_arg: id=first
[     clap::parser::validator] 	Conflicts::gather_direct_conflicts id=first, conflicts=[]
[     clap::parser::validator] 	Conflicts::gather_conflicts: conflicts=[]
[     clap::parser::validator] 	Validator::missing_required_error; incl=[]
[     clap::parser::validator] 	Validator::missing_required_error: reqs=ChildGraph([Child { id: either_or_both, children: [] }, Child { id: first, children: [] }])
[         clap::output::usage] 	Usage::get_required_usage_from: incls=[], matcher=true, incl_last=true
[         clap::output::usage] 	Usage::get_required_usage_from: unrolled_reqs={either_or_both, first}
[      clap::builder::command] 	Command::unroll_args_in_group: group=either_or_both
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=first
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=second
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[      clap::builder::command] 	Command::unroll_args_in_group: group=either_or_both
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=first
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=second
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[         clap::output::usage] 	Usage::get_required_usage_from: ret_val={}
[     clap::parser::validator] 	Validator::missing_required_error: req_args=[]
[         clap::output::usage] 	Usage::create_usage_with_title
[         clap::output::usage] 	Usage::create_usage_no_title
[         clap::output::usage] 	Usage::create_smart_usage
[         clap::output::usage] 	Usage::get_required_usage_from: incls=[require-first, second, either_or_both], matcher=false, incl_last=true
[         clap::output::usage] 	Usage::get_required_usage_from: unrolled_reqs={either_or_both, first}
[      clap::builder::command] 	Command::unroll_args_in_group: group=either_or_both
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=first
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=second
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[         clap::output::usage] 	Usage::get_required_usage_from:iter:require-first
[      clap::builder::command] 	Command::unroll_args_in_group: group=either_or_both
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=first
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[      clap::builder::command] 	Command::unroll_args_in_group:iter: entity=second
[      clap::builder::command] 	Command::unroll_args_in_group:iter: this is an arg
[         clap::output::usage] 	Usage::get_required_usage_from: ret_val={"--require-first", "<--first|--second>"}
[      clap::builder::command] 	Command::color: Color setting...
[      clap::builder::command] 	Auto
error: The following required arguments were not provided:

USAGE:
    clap_example --require-first <--first|--second>

For more information try --help
```