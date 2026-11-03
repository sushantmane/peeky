`tracing::subscriber::set_default(NoSubscriber::default())` not working as expected
## Bug Report

### Version

```
$ cargo tree | grep tracing
── tracing v0.1.32
│   ├── tracing-attributes v0.1.20 (proc-macro)
│   └── tracing-core v0.1.23
└── tracing-subscriber v0.3.9
    ├── tracing-core v0.1.23 (*)
    └── tracing-log v0.1.2
        └── tracing-core v0.1.23 (*)
```

### Platform

```
$ uname -a
Linux <hostname> 5.16.12-arch1-1 #1 SMP PREEMPT Wed, 02 Mar 2022 12:22:51 +0000 x86_64 GNU/Linux
```

### Crates

tracing

### Description

I tried to temporarily stop logging with by setting a `NoSubscriber` as the default subscriber. Logging continued nevertheless.

Minimal reproduction:

Cargo.toml:
```toml
[package]
name = "blurb"
version = "0.1.0"
edition = "2021"

[dependencies]
tracing = "0.1.32"
tracing-subscriber = "0.3.9"
```

src/main.rs
```rs
use tracing::subscriber::NoSubscriber;
use tracing_subscriber::fmt::Layer;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
fn main() {
    tracing_subscriber::registry()
        .with(Layer::new())
        .init();
    let _default_guard = tracing::subscriber::set_default(NoSubscriber::default());
    tracing::error!("you shouldn't see this!");
}
```
Output:
```
$ cargo run
    Finished dev [unoptimized + debuginfo] target(s) in 0.07s
     Running `target/debug/blurb`
2022-03-17T22:41:51.974464Z ERROR blurb: you shouldn't see this!
```