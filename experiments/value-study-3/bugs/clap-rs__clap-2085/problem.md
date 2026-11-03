-h, --help generate trailing spaces
<!-- Issuehunt Badges -->
[<img alt="Issuehunt badges" src="https://img.shields.io/badge/IssueHunt-%2410%20Rewarded-%237E24E3.svg" />](https://issuehunt.io/r/clap-rs/clap/issues/1094)
<!-- /Issuehunt Badges -->


### Rust Version

rustc 1.21.0 (3b72af97e 2017-10-09)

### Affected Version of clap

clap 2.27.1

### Expected Behavior Summary

The help info should not contain unexpected trailing spaces for any lines of text.

### Actual Behavior Summary

Some text ends with spaces, especially after the flag names.

### Steps to Reproduce the issue

`program --help` or `program -h`.

### Sample Code

```rust
extern crate clap;
use clap::{App, Arg};

fn main() {
    let _ = App::new("clap-space")
        .arg(Arg::with_name("a").long("flag-a").help("The first flag."))
        .arg(Arg::with_name("b").long("flag-b").help("The second flag."))
        .print_long_help();
}
```

### Debug output

```
DEBUG:clap:Parser::propagate_settings: self=clap-space, g_settings=AppFlags(
    NEEDS_LONG_HELP | NEEDS_LONG_VERSION | NEEDS_SC_HELP | UTF8_NONE | COLOR_AUTO
)
DEBUG:clap:Parser::create_help_and_version;
DEBUG:clap:Parser::create_help_and_version: Building --help
DEBUG:clap:Parser::create_help_and_version: Building --version
DEBUG:clap:Parser::propagate_settings: self=clap-space, g_settings=AppFlags(
    NEEDS_LONG_HELP | NEEDS_LONG_VERSION | NEEDS_SC_HELP | UTF8_NONE | COLOR_AUTO
)
DEBUG:clap:Parser::create_help_and_version;
DEBUG:clap:Help::write_app_help;
DEBUG:clap:Help::write_parser_help;
DEBUG:clap:Help::write_parser_help;
DEBUG:clap:Parser::color;
DEBUG:clap:Parser::color: Color setting...Auto
DEBUG:clap:is_a_tty: stderr=false
DEBUG:clap:Help::new;
DEBUG:clap:Help::write_help;
DEBUG:clap:Help::write_default_help;
DEBUG:clap:Help::write_bin_name;
DEBUG:clap:Help::write_version;
DEBUG:clap:usage::create_usage_no_title;
DEBUG:clap:usage::get_required_usage_from: reqs=[], extra=None
DEBUG:clap:usage::get_required_usage_from: after init desc_reqs=[]
DEBUG:clap:usage::get_required_usage_from: no more children
DEBUG:clap:usage::get_required_usage_from: final desc_reqs=[]
DEBUG:clap:usage::get_required_usage_from: args_in_groups=[]
DEBUG:clap:usage::needs_flags_tag;
DEBUG:clap:usage::needs_flags_tag:iter: f=a;
DEBUG:clap:Parser::groups_for_arg: name=a
DEBUG:clap:Parser::groups_for_arg: No groups defined
DEBUG:clap:usage::needs_flags_tag:iter: [FLAGS] required
DEBUG:clap:usage::create_help_usage: usage=clap-space [FLAGS]
DEBUG:clap:Help::write_all_args;
DEBUG:clap:Help::write_args;
DEBUG:clap:Help::write_args: Current Longest...2
DEBUG:clap:Help::write_args: New Longest...8
DEBUG:clap:Help::write_args: Current Longest...8
DEBUG:clap:Help::write_args: New Longest...8
DEBUG:clap:Help::write_args: Current Longest...8
DEBUG:clap:Help::write_args: New Longest...8
DEBUG:clap:Help::write_args: Current Longest...8
DEBUG:clap:Help::write_args: New Longest...9
DEBUG:clap:Help::write_arg;
DEBUG:clap:Help::short;
DEBUG:clap:Help::long;
DEBUG:clap:Help::val: arg=--flag-a
DEBUG:clap:Help::spec_vals: a=--flag-a
DEBUG:clap:Help::val: Has switch...Yes
DEBUG:clap:Help::val: force_next_line...false
DEBUG:clap:Help::val: nlh...false
DEBUG:clap:Help::val: taken...21
DEBUG:clap:Help::val: help_width > (width - taken)...15 > (120 - 21)
DEBUG:clap:Help::val: longest...9
DEBUG:clap:Help::val: next_line...No
DEBUG:clap:write_spaces!: num=5
DEBUG:clap:Help::help;
DEBUG:clap:Help::help: Next Line...true
DEBUG:clap:Help::help: Too long...No
DEBUG:clap:Help::write_arg;
DEBUG:clap:Help::short;
DEBUG:clap:Help::long;
DEBUG:clap:Help::val: arg=--flag-b
DEBUG:clap:Help::spec_vals: a=--flag-b
DEBUG:clap:Help::val: Has switch...Yes
DEBUG:clap:Help::val: force_next_line...false
DEBUG:clap:Help::val: nlh...false
DEBUG:clap:Help::val: taken...21
DEBUG:clap:Help::val: help_width > (width - taken)...16 > (120 - 21)
DEBUG:clap:Help::val: longest...9
DEBUG:clap:Help::val: next_line...No
DEBUG:clap:write_spaces!: num=5
DEBUG:clap:Help::help;
DEBUG:clap:Help::help: Next Line...true
DEBUG:clap:Help::help: Too long...No
DEBUG:clap:Help::write_arg;
DEBUG:clap:Help::short;
DEBUG:clap:Help::long;
DEBUG:clap:Help::val: arg=--help
DEBUG:clap:Help::spec_vals: a=--help
DEBUG:clap:Help::val: Has switch...Yes
DEBUG:clap:Help::val: force_next_line...false
DEBUG:clap:Help::val: nlh...false
DEBUG:clap:Help::val: taken...21
DEBUG:clap:Help::val: help_width > (width - taken)...23 > (120 - 21)
DEBUG:clap:Help::val: longest...9
DEBUG:clap:Help::val: next_line...No
DEBUG:clap:write_spaces!: num=7
DEBUG:clap:Help::help;
DEBUG:clap:Help::help: Next Line...true
DEBUG:clap:Help::help: Too long...No
DEBUG:clap:Help::write_arg;
DEBUG:clap:Help::short;
DEBUG:clap:Help::long;
DEBUG:clap:Help::val: arg=--version
DEBUG:clap:Help::spec_vals: a=--version
DEBUG:clap:Help::val: Has switch...Yes
DEBUG:clap:Help::val: force_next_line...false
DEBUG:clap:Help::val: nlh...false
DEBUG:clap:Help::val: taken...21
DEBUG:clap:Help::val: help_width > (width - taken)...26 > (120 - 21)
DEBUG:clap:Help::val: longest...9
DEBUG:clap:Help::val: next_line...No
DEBUG:clap:write_spaces!: num=4
DEBUG:clap:Help::help;
DEBUG:clap:Help::help: Next Line...true
DEBUG:clap:Help::help: Too long...No
clap-space 

USAGE:
    clap-space [FLAGS]

FLAGS:
        --flag-a     
            The first flag.

        --flag-b     
            The second flag.

    -h, --help       
            Prints help information

    -V, --version    
            Prints version information
```


<!-- Issuehunt content -->

---

<details>
<summary>
<b>IssueHunt Summary</b>
</summary>

#### [<img src='https://avatars2.githubusercontent.com/u/28928944?v=4' alt='knightpp' width=24 height=24> knightpp](https://issuehunt.io/u/knightpp) has been rewarded.

### Backers (Total: $10.00)

- [<img src='https://avatars0.githubusercontent.com/u/174703?v=4' alt='pksunkara' width=24 height=24> pksunkara](https://issuehunt.io/u/pksunkara) ($10.00)

### Submitted pull Requests
- [#2085 Don't print spaces in long help](https://issuehunt.io/r/clap-rs/clap/pull/2085)
---

### Tips

- Checkout the [Issuehunt explorer](https://issuehunt.io/r/clap-rs/clap/) to discover more funded issues.
- Need some help from other developers? [Add your repositories](https://issuehunt.io/r/new) on IssueHunt to raise funds.
</details>
<!-- /Issuehunt content-->