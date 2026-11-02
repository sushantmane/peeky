Wait for messages in the error queue
**Is your feature request related to a problem? Please describe.**

I work on [ntpd-rs](https://github.com/pendulum-project/ntpd-rs), an NTP implementation in rust. A big part of how it works is to configure a UDP socket to record timestamps when a message is sent or received, and then retrieving these timestamps. The timestamp is not available immediately (we've observed this for external hardware clocks, software timestamps do seem to be effectively instant).

These timestaps end up in the error queue (libc::MSG_ERRQUEUE) of the socket. Tokio does not have a way to await or read these messages currently. The reading of these messages is probably too low-level for tokio, but being able to await the messages appearing would be extremely useful. 

**Describe the solution you'd like**

With https://github.com/tokio-rs/tokio/pull/5566 heading in a promising direction, I think from a user perspective, the best API would be

```rust
let socket: AsyncFd<std::net::UdpSocket> = ...;

loop { 
    let mut guard = socket.ready(Interest::ERROR).await?;

    if guard.ready().is_error() {
        match read_from_error_queue(socket.get_ref()) {
            Ok(msg) => {
                println!("read {msg}");
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                guard.clear_ready_exact(Ready::ERROR);
                continue;
            }
            Err(e) => {
                return Err(e.into());
            }
        }
    }
}
```

Adding `Ready::ERROR` is straightforward. But adding an error interest is really not. Currently, a tokio interest is just a wrapper around the `mio::Interest`:

```rust
pub struct Interest(mio::Interest);

impl Interest {
    /// Interest in all readable events.
    ///
    /// Readable interest includes read-closed events.
    pub const READABLE: Interest = Interest(mio::Interest::READABLE);

    ...
```

But `mio::Interest::ERROR` does not exist, with a good reason: the error "interest" is always registered by the OS (see  https://github.com/tokio-rs/mio/issues/1672#issuecomment-1553456643). Therefore (not) using `mio::Interest::ERROR` would have no effect either way, and would be confusing.

But for public tokio APIs, the interest is specifically what you listen for, and hence what you'll be notified for. That would mean that tokio would have to implement `Interest` itself (sort of inlining the mio implementation). I don't see any technical issues with that approach, and I don't think there are performance downsides really: all of the logic to convert to `mio::Interest` could use `const fn`. 

But it's kind of inelegant because it duplicates a bunch of logic from mio, and increases maintenance burden and the change of inconsistencies. I'd say it's worth it, but there is a cost.

**Describe alternatives you've considered**

Another option would be to always include the error readiness, like mio:

```rust
impl Ready { 
    pub(crate) fn from_interest(interest: Interest) -> Ready {
        let mut ready = Ready::EMPTY;

        if interest.is_readable() {
            ready |= Ready::READABLE;
            ready |= Ready::READ_CLOSED;
        }

        // ...

        // just always include the error readiness
        ready |= Ready::ERROR

        ready
    }
}
```

this would sort of work, but it's extremely inelegant. The example from earlier would have to pick a different, arbitrary `Interest`, and then just ignore this interest and just see if the readiness includes the error flag.

```rust
loop { 
    let mut guard = socket.ready(Interest::READABLE).await?;

    if guard.ready().is_error() {
        // ...
    }
}
```

I also think this will lead to problems with e.g. `readable` firing but a read stil blocking. Code should be robust against that, but it's still inefficient to read when we know it would block.

**Additional context**

The error queue is used for many things besides timestamps. Here is [an example](https://gist.github.com/folkertdev/9bb8abeb5fcd7f32a8bfdc86f2a6fc3b) that puts a explicitly puts a message on the error queue. It may come in handy for testing. The examples that I know of are all from unix systems, but I don't see any harm in exposing error readiness across platforms (mio already supports it anyway). 

I'm happy to work on this, but would like some confirmation on this path not being totally unreasonable before starting.