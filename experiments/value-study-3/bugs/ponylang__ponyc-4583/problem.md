quiescence issue with actor pinning
The expectation when we merged actor pinning was that it worked the same for quiescence as a program without pinning. However that isn't the case at the moment. Given the quiescence rules, we would expect the following program to never end as the `Main` actor will always have a message in its queue.

```pony
use "actor_pinning"

actor Main
  let _env: Env
  let _auth: PinUnpinActorAuth

  new create(env: Env) =>
    _env = env
    _auth = PinUnpinActorAuth(env.root)
    ActorPinning.request_pin(_auth)
    check_pinned()

  be check_pinned() =>
    if ActorPinning.is_successfully_pinned(_auth) then
      do_stuff(10)
    else
      check_pinned()
    end

  be do_stuff(i: I32) =>
    do_stuff(i - 1)

  be done() =>
    ActorPinning.request_unpin(_auth)
```

However, the program does in fact exit.

With a slight tweak, it doesn't:

```pony
use "actor_pinning"

actor Main
  let _env: Env
  let _auth: PinUnpinActorAuth

  new create(env: Env) =>
    _env = env
    _auth = PinUnpinActorAuth(env.root)
    ActorPinning.request_pin(_auth)
    check_pinned()

  be check_pinned() =>
    if ActorPinning.is_successfully_pinned(_auth) then
      do_stuff(10)
    else
      check_pinned()
    end

  be do_stuff(i: I32) =>
    _env.out.print("Doing stuff: " + i.string())
    do_stuff(i - 1)

  be done() =>
    ActorPinning.request_unpin(_auth)
```

So long as regular scheduler threads have an actor that is busy, the program will continue to run. Here we introduced sending a message to the unpinned outstream actor and we continue to process.

This was brought to my attention by @redvers who was working with early actor pinning work from @dipinhora's branch and his programs were working great. When he switched to an official ponyc release, they would exit.

I did a bit of spelunking and as one might guess, this started happening before we merged. I did some testing and we start seeing this in commit https://github.com/ponylang/ponyc/pull/4547/commits/8c338ae76552bad5e49d108d5566a0200dd31df6 aka "make pinned actor thread participate in CNF/ACK for termination".