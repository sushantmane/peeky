LocalSet and Runtime cannot be stored in thread_local at the same time
**Version**

tokio 1.21.2

**Platform**
x86_64-unknown-linux-gnu

**Description**
It seems similar to #4973 

I tried this code:

```rust
// src/main.rs
use tokio::runtime::Runtime;
use tokio::task::LocalSet;

thread_local! {
    pub static LOCAL_SET: TokioLocalSet = TokioLocalSet::new();
}

// the order here seems wrong, 
// have revised, see the following comments
pub struct TokioLocalSet {
    rt: Runtime,
    local: LocalSet,
}

impl TokioLocalSet {
    pub fn new() -> TokioLocalSet {
        TokioLocalSet {
            rt: tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap(),
            local: LocalSet::new(),
        }
    }

    async fn inner_method(&self) {
        self.local
            .run_until(async move {
                tokio::task::spawn_local(async {});
            })
            .await
    }

    pub fn method(&self) {
        self.rt.block_on(self.inner_method());
    }
}

fn main1(){
    // will panic
    LOCAL_SET.with(|f|{
        f.method();
    });
}

fn main2(){
    // work well
    let ls = TokioLocalSet::new();
    ls.method();
}

fn main3(){
    // work well
    let ls = TokioLocalSet::new();
    ls.method();
    LOCAL_SET.with(|f|{
        f.method();
    });
}

```

```toml
# Cargo.toml
[package]
name = "tokio_local_set"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1.21.2", features = ["full"] }
```

I expected to see this happen: `main1` works well.

Instead, this happened: `main3` works well, `main1` panics. And reported the same error as #4973 

```bash
thread 'main' panicked at 'cannot access a Thread Local Storage value during or after destruction: AccessError'
```

Is this expected? 

Here is the link to the playground: https://play.rust-lang.org/?version=stable&mode=debug&edition=2021&gist=e26d1f993906627a219d304e849cc70b