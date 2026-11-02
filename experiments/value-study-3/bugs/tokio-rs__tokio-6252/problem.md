Add `mark_unchanged` or `mark_seen` for `tokio::sync::watch::Receiver`.
**Is your feature request related to a problem? Please describe.**

In developing of a web server, I need to wait a new value when handling every http requests. I create a `watch` channel and clone the `Receiver` for every individual request.

What I want is to call `.changed().await` and wait for a new value. However the receiver I clone from is always outdated. So I have to mark it as seen before waiting new value, otherwise the `changed()` return immediately.

Currently, the only way to mark the receiver is seen is the `borrow_and_update()` method. But it not only update the internal version, but also lock the internal RwLock for getting the value, which is unnecessary in this case.

**Describe the solution you'd like**
I want to add a method called `mark_unchanged` or `mark_seen`, which just update the Receiver internal version, without locking the shared RwLock anyway.

**Describe alternatives you've considered**
`drop(borrow_and_update())`

**Additional context**
No additional context.

I can explain more details of how I expect the web server works. When a new GET request incoming, it blocks to wait. In the background, a coroutine is scanning bluetooth devices in an infinite loop. Every time it found one, it sends a value to the channel. And all currently waiting request handlers received the value and return.

I suggest to add this method for avoid an unnecessary read-lock because we can. It's not a critical optimization for me. But it will be useful for someone who is building high-performance services.