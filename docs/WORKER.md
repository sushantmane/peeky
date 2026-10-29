# The HTTP worker

`peeky-worker` runs the debugger next to the program, and answers authenticated JSON requests over
HTTP. The `peeky` CLI starts and calls a worker for you. This page is for code that calls a worker
itself.

- To run a worker on another host, see [REMOTE.md](REMOTE.md).
- Every operation, with its inputs and results, is in [DESIGN.md](DESIGN.md#http-contract).

## What the C and C++ worker does

A native worker runs a fixed C or C++ target through lldb-dap:

- It builds the declared sources in a fresh workspace, with debug information
  ([what it checks](lang/c-cpp-cookbook.md#what-peeky-checks-about-a-build)).
- It checks the source's, the binary's and the symbols' identity, and verifies the loaded
  executable and each stopped location.
- It changes integers and texts, within Peeky's rules
  ([C and C++ 7.1](lang/c-cpp-cookbook.md#71-c-and-c)). Rust uses the same debugger family.
- The [investigation loop](AGENT_LOOP.md) rebuilds a candidate fix and verifies it
  independently. A change at runtime is an experiment, not a fix to the source.

It doesn't attach to a running program, evaluate expressions, change a pointer, or deploy a remote
debug server. It writes memory only to set a text
([C and C++ 7.1](lang/c-cpp-cookbook.md#change-a-text)).

## Compare the sources from another host

A client on another host must show that its sources are the worker's. Protocol 3 added this, and
protocols 4 to 8 keep it.

1. Call `version`. Its `sources` says which files to hash:
   - `{"kind": "python"}`: every `.py` file under the root, except in virtual environments (a
     directory holding `pyvenv.cfg`);
   - `{"kind": "declared", "names": [...]}`: the files listed, relative to the root.

   Its `root` is the worker's `--root`.
2. With `launch` or `probe`, send `protocol`: 8, `expect_sha256` (`program`, and `entry` for a
   `--run` script) and `expect_inputs`: `{path: sha256}` for every one of those files, at least
   one.
3. The worker refuses with `PROVENANCE_CHANGED`, naming the first file that differs or is on one
   side only:
   - before launching;
   - for a compiled target, again after the build, before the debugger starts;
   - for Python, before and after each later operation that resumes the program or returns a
     stop or values, `wait` included;
   - for Python, at a stack frame whose code names a file under the root that isn't a compared
     `.py` file. A virtual environment inside the root is excepted.

   After a Python refusal, the session is closed.
4. A request with `expect_sha256` but without `expect_inputs`, or with an empty one, is refused
   with `INVALID_REQUEST`. So is one with `expect_inputs` and another `protocol`, or none.
5. Check the answer to `launch`, or the probe's result:
   - `inputs_checked` is the sha256 of `json.dumps(sorted(expect_inputs.items()))`, with Python's
     default separators;
   - `inputs_sha256` must match your own digest of the sources. For Python, it is the sha256 of
     `json.dumps` of the `[path, sha256]` pairs in walk order: a directory's files, sorted, then
     its subdirectories, sorted. Otherwise, it is computed as `inputs_checked` is.

   If either is missing or different, discard the result, or close the session.

6. Hash your own sources again after each answer. If they changed, discard the result, or close
   the session.
7. Read the paths in each answer. After a request with `expect_inputs`, a `location`, or a
   `source`'s `path`, that is one of those files is `source:NAME`, with its `:LINE`. NAME is
   relative to the root. Show your copy of NAME only if you hashed a file of that name, and it is
   still a regular file under your root. Mark any other path as the worker host's
   ([REMOTE.md](REMOTE.md#paths-in-results)).

`peeky probe --worker` and `peeky session start --worker` do all of this. On every later
`session` command, they also check the worker's protocol again. They hash your sources before
each request except `close`, and again after each one that succeeds
([REMOTE.md](REMOTE.md#source-identity)).

## Start a C or C++ worker

The worker takes the same config file as the CLI's `--config`
([C and C++ 7.1](lang/c-cpp-cookbook.md#name-the-tools-in-a-config)). Save it as
`work/native-target.json`, set a random worker token, and start the worker on the bundled example:

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root "$PWD/examples/c" --native-config "$PWD/work/native-target.json" --port 8765
```

- The config is trusted startup input. HTTP requests choose only a program it already declares,
  and the debugger operations Peeky supports.
- A request can't supply a compiler command, an LLDB initialization command, or a launch
  configuration of its own.
- `--native-config` and `--java-config` are mutually exclusive.
- A config field that Peeky doesn't know is refused, and `sources` is required.

## Inspect and change a C value

`examples/c/branch_bug.c` has an integer `value`, a pointer to it, an integer array, and a branch.
With the `Client` API, launch `branch_bug.c` with a breakpoint on the line marked `FIRST`, wait,
inspect, set `value` to `5000`, and continue. The program prints:

```text
RESULT=success value=5000 array=4 pointer=5000
```

- The original source and the compiled executable don't change during that experiment.
- A pointer or an array shows its type and its debugger reference. `expand` lists its children
  ([below](#read-a-values-children-expand-and-preview)).
- Changing a pointer is refused.

## Use the current session, stop and handles

- Stopped operations take the current `session_id` and `stop_id`, and a scope handle from the
  latest inspection.
- Scope handles are opaque. Another inspection, a stop transition, `continue` or `close`
  invalidates them.
- LLDB may reuse a raw reference for a different frame. That never makes an old worker handle
  valid again.

## Read breakpoints and stops as evidence

- LLDB may first return a breakpoint as pending. Use the verified breakpoint events and the actual
  stopped frame and location as evidence. The worker keeps those events bounded.
- A compiled target runs from a private build copy of its sources. Breakpoints, `location` and
  `frames` name your source file instead of the copy, where the copy is unchanged. A candidate's
  edited file, and a file only the build has, keep the copy's path, since their lines are the
  copy's.
- Registers aren't treated as local variables.
- The worker turns off LLDB's init files, shell expansion of the program's arguments, and LLDB's
  automatic variable summaries. That removes hidden configuration and behavior. It isn't isolation
  from hostile code.

## Start a Java worker

Java worker support is experimental. Over HTTP, the worker serves one fixed Java target, declared
in advance in its trusted startup config.

The worker takes a launched target's config, the same as the CLI's `--config`
([Java 7.3](lang/java-cookbook.md#set-up-a-launched-target)). For example, a `work/java-target.json`:

```json
{
  "bundle": "java-adapter",
  "sources": ["demo/BranchBug.java"],
  "main_class": "demo.BranchBug",
  "arguments": []
}
```

- `bundle` resolves relative to the config file, and source paths relative to the worker root.
- The config is trusted local input, not a request from an agent. A request can't supply adapter
  commands or build recipes.

```sh
export DEBUG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
peeky-worker --root /absolute/path/to/java-sources --java-config "$PWD/work/java-target.json" --port 8765
```

The bundled example starts with
`peeky-worker --root examples/java --java-config examples/java/target.json`, after setting
`DEBUG_TOKEN`.

## Inspect and change a Java value

`examples/java/demo/BranchBug.java` branches on line 6:
`String result = timeoutMs > 0 ? "ready" : "timed out";`. At that stop, change `timeoutMs` from
`0` to `5000`, and continue: the program prints `ready` instead of `timed out`.

From another process, with the same token:

```python
import os
from peeky.client import Client

client = Client("http://127.0.0.1:8765", os.environ["DEBUG_TOKEN"])
launch = client.call("launch", program="demo/BranchBug.java", breakpoints=[{"line": 6}])
session = {"session_id": launch["session_id"]}
stop = client.call("wait", **session, timeout=2)
while stop["state"] == "running":
    stop = client.call("wait", **session, timeout=2)
if stop["state"] == "stopped":
    snapshot = client.call("inspect", **session, stop_id=stop["stop_id"])
    print(snapshot)
client.call("close", **session)
```

- For your own program, choose a line that runs code.
- A breakpoint may start pending while its class loads. `wait` and `report` show bounded
  breakpoint events, including the adapter's verified location.
- `inspect` returns the actual stopped source path and line, checked against the compiler's
  metadata. Source paths in the stack point to the worker's fresh workspace.
- An array shows its type, its length and its debugger reference. `expand` lists its elements
  ([below](#read-a-values-children-expand-and-preview)).
- Sessions can step over, into and out. The launcher fixes the step filters
  ([Java 5.2](lang/java-cookbook.md#what---into-and---out-skip)).
- A change at runtime is an experiment, never a verified fix. The
  [Java investigation loop](AGENT_LOOP.md) rebuilds a candidate fix and verifies it.

## Java adapter markers

Peeky's launcher tells the worker what the bundle supports. A bundle without a marker refuses the
change that needs it.

| Marker | What the launcher does |
| --- | --- |
| `peekyCharSet` | Sends java-debug a char between single quotes, only for a variable declared `char` |
| `peekyTextSet` | Sends a text, only for a variable declared `String` |
| `peekyJdkOwners` | Names each listed field's declaring class (`peekyOwner`), and whether it is the JDK's (`peekyJdkOwner`): by its module, or on Java 8 by its class loader |
| `peekyArrayHolders` | Answers Peeky's `peekyArrayHolder` request: whether a JDK class's field holds an array, and whether every thread is suspended (`allSuspended`), asked before its elements are marked `[settable]`. Suspends every thread while it checks and writes an element. Without it, elements read `[maybe settable: …]`; changes still work ([Java 7.3](lang/java-cookbook.md#arrays-the-jdk-holds)) |
| `peekyPauseLeftMs` | Bounds the wait for what refers to an array to what the pause budget leaves ([Java 7.3](lang/java-cookbook.md#when-peeky-cant-tell-who-holds-an-array)) |

## Configure a C++ target

A native target's config, passed with `--native-config`, sets `"language": "cpp"` for C++. Its
sources are `.cpp`, `.cc` or `.cxx` files, and it names an explicit C++ compiler. This is a trusted
config for the C++ fixture, with the worker root set to `examples/cpp`:

```json
{
  "language": "cpp",
  "sources": ["pagination_bug.cpp"],
  "headers": ["pagination.hpp"],
  "compiler": "/usr/bin/clang++-18",
  "adapter": "/usr/bin/lldb-dap-18",
  "dwarfdump": "/usr/bin/llvm-dwarfdump-18"
}
```

- Start the worker with `peeky-worker --native-config config.json`. Set `DEBUG_TOKEN` first, and
  pass the intended `--root`.
- On macOS, also set `dsymutil` and the SDK's `sysroot`. Take the compiler's and debugger's paths
  from `xcrun --find`.
- The compiler must be clang
  ([C and C++ 7.1](lang/c-cpp-cookbook.md#which-c-builds-peeky-supports)).
- A C++ target gets the same source and build identity checks as C, and the same temporary build
  lifecycle ([what Peeky checks](lang/c-cpp-cookbook.md#what-peeky-checks-about-a-build)).

## Read a value's children: `expand` and `preview`

Both operations are read-only.

- **`expand`** takes a variable's opaque `variablesReference` as `handle`, with the current session
  and stop. It lists up to `count` children, at most 64, from `start`. Instead of `start` and
  `count`, `index` reads one element of an array of any length.
- With `previews: true`, `expand` previews the values it lists.
- **`inspect`** takes `previews` too: `true` (the default), `false`, or a list of the roots to
  preview, up to 32 names.
- **`preview`** gives one value's preview by its `handle`, or nothing. With `pointee: true`, it
  previews what a pointer points at.
- A probe asks `inspect` to preview only the roots it captures (`previews: [names]`). A selector
  walks to a field without previews on its way, and `preview` previews the value it reaches.
- Collections are bounded to 512 entries, with limits on depth and on inspection nodes
  ([DESIGN.md](DESIGN.md#bounded-child-inspection)).
- Each expansion rechecks the source and build, the frame, the scope and the child's ancestry.
- Any expansion revokes the mutable scope handles. To change a scalar after one, inspect again.
- A fresh inspection invalidates earlier child handles too.

## What the worker adds to C and C++ values

| Field | On | Meaning |
| --- | --- | --- |
| `whole_string_summaries` | a stop | `true` when LLDB's summary of a `std::string` is all of it: libc++, known by `std::__1` in the debug information. libstdc++ (`std::__cxx11`) gets no such field |
| `statics` | a stop | The names the frame's function keeps in static storage, and Peeky doesn't read here: a `static` local, or a `const` local GCC places in read-only data. A selector rooted at one is `not found`, and says why. Absent where there are none |
| `static_notes` | a stop | Why Peeky doesn't read each of `statics` here, by name: the stop is above its declaration or past its block, or Peeky can't tell which one the code names. A client shows it after `declares it in static storage`. An earlier worker gives none, and reads no static local |
| `static_storage: true` | a scope | The `Statics` scope: the frame's variables in static storage that the code at the stop names, which lldb-dap lists apart from its locals. They are read, and never changed: `set_variable` on its reference is refused (`MUTATION_REJECTED`) |
| `closure_captures` | a stop | The captures a GCC lambda's call operator reads through `__closure`. lldb-dap lists none of them among the frame's locals. The worker reads them in a `Captures` scope. Absent where there are none, and for clang's lambdas |
| `closure_unread` | a stop | `{capture: why}` for each of `closure_captures` with no row in the `Captures` scope: an optimized build, a stop before the lambda stores `__closure`, the lambda's own local of that name in scope there, what the build doesn't say, or which read of the debugger's failed (the hover of `__closure`, or the listing of what it points at: no answer in time, an error, malformed, past 512 members). A selector of one is `not found`, with that reason. Protocol 8 |
| A `Captures` scope | a stop | A GCC lambda's captures, read as members of what `__closure` points at. A capture by reference shows what it refers to, with LLDB's reference type as `debugger_type`. `[*this]`'s `this` is the lambda's copy of the object. What is read through `__closure`, or through such a reference, is checked as through a pointer (`view: unproven`). A capture listed here replaces any row of its name the debugger lists: a local of the lambda's whose scope hasn't begun, or `[*this]`'s `this`, which it gives no value. Its `reference` changes nothing |
| `not_settable: capture` | a variable | A GCC lambda's capture, or a member of one: its object holds it, so no stop changes it |
| `string_length` | a variable | A libstdc++ string's length, checked against its `_M_string_length`. A map key matches only a string shown whole and of its length, or one read from memory |
| `view: memory` | a variable | Peeky read the string's text from memory, or an unordered container's entries from its own nodes |
| `text: true` | a variable | A standard string shown as its text, or as Peeky's reason it has none. It gets no handle, as Rust's text views get none |
| `aliased_type` | a variable | The class behind a typedef's name, such as a map under another name |
| `pointee_aliased_type` | a variable | The same, for what a pointer points at: `IntSet *` |
| `past_end` | a variable, or a stop's `returned` | A pointer that holds the address one past the end of an array a listed frame holds: `{"selector": "queue", "length": 4}`, with `frame` and `function` where another frame holds it, and `also`, what of its type starts there too. Its value is its address alone, and it gets no handle: nothing there belongs to the array. A returned value says it only for a character pointer one past a character array's end |
| `view: unproven`, `unproven_read: true` | a variable, or a stop's `returned` | Read through a pointer that Peeky can't show points at a live object, or a pointer whose text LLDB read through it (a `char *`). Its `note` says why and what to do. The value is still shown. Anything read through it is marked too |
| `unproven_read` | an `expand` reply | The note its values carry, where they are read through such a pointer. A client reading the pointee whole (`p[0]`) marks it with it |
