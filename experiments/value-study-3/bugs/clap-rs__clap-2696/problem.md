Self override and multiple values don't interact well together
### Affected Version of clap

master and v3-master (did not try crates.io versions)

### Bug or Feature Request Summary

Arguments overriding themselves with multiple values have... surprising behavior.  Consider the sample code below:

### Sample Code or Link to Sample Code

```rust
extern crate clap;
use clap::{App, Arg};

fn main() {
    let matches = App::new("MyApp")
        .arg(
            Arg::with_name("input")
                .takes_value(true)
                .long("input")
                .overrides_with("input")
                .min_values(0),
        ).get_matches();

    if let Some(vs) = matches.values_of("input") {
        println!("{:?}", vs.collect::<Vec<_>>());
    }
}
```

### Expected Behavior Summary

One would expect this output, where each new occurrence of the `--input` argument overrides the preceding one:

```shell
$ ./target/debug/example --input a b c --input d
["d"]
$ ./target/debug/example --input a b --input c d
["c", "d"]
```

### Actual Behavior Summary

However on current clap master 33f908df9b1eba26aadb8634104a135e58ea6912 there is no difference between those two invocations:

```shell
$ ./target/debug/example --input a b c --input d
["c", "d"]
$ ./target/debug/example --input a b --input c d
["c", "d"]
```

Interestingly, Clap v3 seems to have changed *something*, since on v3-master eaa0700e7ed76f37d245a6878deb3b8e81754d6c I get this:
```shell
$ ./target/debug/example --input a b c --input d
["d"]
$ ./target/debug/example --input a b --input c d
["d"]
```

which is unfortunately not much better.

### Consideration and thoughts

This seem to be caused by the self-override code being written with the use case of arguments with a single value in mind.  This also causes other issues, for instance if I change `min_values(0)` to `number_of_values(2)` in the above code I get:

```shell
$ ./target/debug/example --input a b --input c d
error: Found argument 'd' which wasn't expected, or isn't valid in this context
```
on master and:

```shell
$ ./target/debug/example --input a b --input c d
error: The argument '--input <input> <input>' requires 2 values, but 1 was provided
```

on v3-master.

The offending code is here:

https://github.com/clap-rs/clap/blob/33f908df9b1eba26aadb8634104a135e58ea6912/src/args/arg_matcher.rs#L66-L75

Interestingly, it somehow gets called twice (before parsing the overriding argument, and after parsing it), which is why we need `--input a b c --input d` to trigger the bug.  Doing only `--input a b --input c` would properly remove the `a` and `b` (if you are wondering: yes, `--input --input a b` only prints `["b"]`).

The corresponding code in v3 is here, and it now pops off and only keeps the final argument value (hence the behavior change).  It is also now called only once, after the overriding argument has been parsed: 

https://github.com/clap-rs/clap/blob/eaa0700e7ed76f37d245a6878deb3b8e81754d6c/src/parse/parser.rs#L1328-L1338

I am not familiar with the code at all, but from my understanding properly fixing this would require a way of knowing how much arguments were parsed in the current occurrence, in order to only keep that amount.  Unfortunately, there doesn't seem to be a way of getting that information currently.

One solution would be to add an `occurrences` vector to the `MatchedArg` which keeps track of the position in the `vals` vector when a new occurrence is encountered, and can be used to properly pop off all values but the ones associated with the last occurrence when an override occurs.  This can be extended by also keeping track of the position in the `indices` vector, since currently self-overrides "leak" through the `indices` (i.e. the indices of the overridden occurrences are kept), and can be used as a basis to solve #1026.  This may also be the opportunity to change the behavior of `min_values` and `max_values` in clap 3 (or add a setting) to be per-occurrence instead of sum-of-occurrences (at least for me, that would be more intuitive -- but this would probably require discussion beforehand).

I have a proof-of-concept implementation of the above which I can make into a proper PR if the proposed solution seems reasonable, or I can try and implement another suggestion :)