Change default help and version message to infinitive form
### Please complete the following tasks

- [X] I have searched the [discussions](https://github.com/clap-rs/clap/discussions)
- [X] I have searched the existing issues

### Describe your use case

There are two problems here.

1. `clap` uses an unconventional sentence form in its auto-generated help messages (i.e. `"Prints a help message"` vs `"Print a help message"`). A lot of users (as shown in the links above) would like to change this, because practically every other UNIX program shows it in the infinitive form:

```sh
$ grep --help
-b, --byte-offset    print the byte offset with output lines

$ git init --help
--bare    create a bare repository

$ rg -h
--debug    Show debug messages.
```
2. There's no way to set this help message manually if you use `clap_derive`.

### Describe the solution you'd like

I'd like a way to set `global_help_message` and `global_version_message` in my app.
### Alternatives, if applicable

`app.mut_arg("help", |a| a.about("help info"))` is a solution, but it does not work for apps using `clap_derive`.

### Additional Context

There's plenty of demand for this feature:

- https://github.com/clap-rs/clap/issues/2080
- https://github.com/clap-rs/clap/issues/1640
- https://github.com/clap-rs/clap/issues/1646
- https://github.com/clap-rs/clap/issues/1512
- https://github.com/clap-rs/clap/issues/889
- https://github.com/clap-rs/clap/issues/1665
- https://github.com/clap-rs/clap/discussions/2152