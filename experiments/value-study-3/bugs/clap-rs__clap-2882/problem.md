Section headings don't match contents
### Please complete the following tasks

- [X] I have searched the [discussions](https://github.com/clap-rs/clap/discussions)
- [X] I have searched the existing issues

### Rust Version

rustc 1.55.0 (c8dfcfe04 2021-09-06)

### Clap Version

master

### Minimal reproducible code

```rust
use clap::Clap;

#[derive(Debug, Clone, Clap)]
struct CliOptions {
    #[clap(flatten)]
    options_a: OptionsA,

    #[clap(flatten)]
    options_b: OptionsB,
}

#[derive(Debug, Clone, Clap)]
#[clap(help_heading = "HEADING A")]
struct OptionsA {
    #[clap(long)]
    should_be_in_section_a: Option<u32>,
}

#[derive(Debug, Clone, Clap)]
#[clap(help_heading = "HEADING B")]
struct OptionsB {
    #[clap(long)]
    should_be_in_section_b: Option<u32>,
}

fn main() {
    CliOptions::parse();
}
```


### Steps to reproduce the bug with the above code

```
cargo run -- --help
```

### Actual Behaviour

```
clap-failure-example

USAGE:
    clap-failure-example [OPTIONS]

FLAGS:
    -h, --help       Print help information
    -V, --version    Print version information

OPTIONS:
        --should-be-in-section-a <SHOULD_BE_IN_SECTION_A>

HEADING A:
        --should-be-in-section-b <SHOULD_BE_IN_SECTION_B>
```

### Expected Behaviour

```
clap-failure-example

USAGE:
    clap-failure-example

FLAGS:
    -h, --help       Print help information
    -V, --version    Print version information

HEADING A:
        --should-be-in-section-a <SHOULD_BE_IN_SECTION_A>

HEADING B:
        --should-be-in-section-b <SHOULD_BE_IN_SECTION_B>
```

### Additional Context

This was broken by #2531.

### Debug Output

_No response_