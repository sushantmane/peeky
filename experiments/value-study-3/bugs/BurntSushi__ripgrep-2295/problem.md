Only some ignores applied when searching a sub-directory
#### What version of ripgrep are you using?

```
ripgrep 12.1.1
-SIMD -AVX (compiled)
+SIMD +AVX (runtime)
```

#### How did you install ripgrep?

System package https://www.archlinux.org/packages/community/x86_64/ripgrep/

#### What operating system are you using ripgrep on?

Arch Linux

#### Describe your bug.

When giving a sub-directory of the current directory to search as a second command-line argument to ripgrep, only some patterns of `.ignore` in the current directory are applied.

#### What are the steps to reproduce the behavior?

```sh
mkdir tmp
cd tmp
mkdir rust
mkdir rust/target
echo needle > rust/source.rs
echo needle > rust/target/rustdoc-output.html
echo rust/target > .ignore
rg --files-with-matches needle
rg --files-with-matches needle rust
echo target >> .ignore
rg --files-with-matches needle rust
```

#### What is the actual behavior?

The first and third `rg` commands only print `rust/source.rs` as expected. However the second one prints both `rust/source.rs` and `rust/target/rustdoc-output.html`.

With `--debug` added, and `-j1` for more readable output:


```
$ rg --files-with-matches needle --debug -j1
DEBUG|grep_regex::literal|crates/regex/src/literal.rs:58: literal prefixes detected: Literals { lits: [Complete(needle)], limit_size: 250, limit_class: 10 }
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 0 literals, 0 basenames, 12 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 1 literals, 0 basenames, 0 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
DEBUG|ignore::walk|crates/ignore/src/walk.rs:1730: ignoring ./.ignore: Ignore(IgnoreMatch(Hidden))
rust/source.rs
DEBUG|ignore::walk|crates/ignore/src/walk.rs:1730: ignoring ./rust/target: Ignore(IgnoreMatch(Gitignore(Glob { from: Some("./.ignore"), original: "rust/target", actual: "rust/target", is_whitelist: false, is_only_dir: false })))

```
```
$ rg --files-with-matches needle rust --debug -j1
DEBUG|grep_regex::literal|crates/regex/src/literal.rs:58: literal prefixes detected: Literals { lits: [Complete(needle)], limit_size: 250, limit_class: 10 }
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 0 literals, 0 basenames, 12 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 1 literals, 0 basenames, 0 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
rust/source.rs
rust/target/rustdoc-output.html
```

After `echo target >> .ignore`:

```
$ rg --files-with-matches needle rust --debug -j1
DEBUG|grep_regex::literal|crates/regex/src/literal.rs:58: literal prefixes detected: Literals { lits: [Complete(needle)], limit_size: 250, limit_class: 10 }
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 0 literals, 0 basenames, 12 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
DEBUG|globset|crates/globset/src/lib.rs:431: built glob set; 1 literals, 1 basenames, 0 extensions, 0 prefixes, 0 suffixes, 0 required extensions, 0 regexes
rust/source.rs
DEBUG|ignore::walk|crates/ignore/src/walk.rs:1730: ignoring rust/target: Ignore(IgnoreMatch(Gitignore(Glob { from: Some("/tmp/tmp/.ignore"), original: "target", actual: "**/target", is_whitelist: false, is_only_dir: false })))
```

#### What is the expected behavior?

Each command should only print `rust/source.rs`, as the HTML file should be ignored.

One might imagine that when searching a sub-directory only `.ignore` files under that sub-directory are considered, but the third command shows that is not the case. (The more general ignore pattern is not the desired one, it only serves to show that the `.ignore` file is indeed read.)

#### Work-around

I’ve managed to find a set up that does what I want by creating a `.ignore` file in the `rust` sub-directory of the project instead, with a `/target` pattern.

#### Other suggestion

After trying a few things and seeing unexpected results I wanted to find out details of the syntax of `.ignore` files. For example, is an initial slash meaningful?

I had to dig to find the answer in https://docs.rs/ignore/0.4.17/ignore/struct.WalkBuilder.html:

> `.ignore` files have the same semantics as `gitignore` files 

and

> `.gitignore` files have match semantics as described in the `gitignore` man page.

It would be good to repeat these two facts in ripgrep’s own docs.