Consistency between various *default methods
## Feature Request

- `tracing::dispatcher::set_default` takes `&Dispatch`
- `tracing::dispatcher::set_global_default` takes `Dispatch`
- `tracing::dispatcher::with_default` takes `&Dispatch`

These should all take one or the other. `&Dispatch` is definitely more ergonomic (and we clone internally), `Dispatch` saves us a clone in certain cases. Either way, we should stick with one.