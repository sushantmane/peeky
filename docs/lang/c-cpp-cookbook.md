# Peeky Cookbook: C and C++

This cookbook covers what is different for C and C++. Read the [COOKBOOK](../COOKBOOK.md) first:
it has the recipes every language shares. Each section here has the number and title of the
COOKBOOK section it adds to. For a one-page summary of the C and C++ rules, see
[c-cpp.md](c-cpp.md).

Start with [7.1](#71-c-and-c): what Peeky reads and changes in C and C++.

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
  - [See each name's type](#see-each-names-type)
  - [How a C++ function's name is shown](#how-a-c-functions-name-is-shown)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
  - [Read through a pointer](#read-through-a-pointer)
  - [Read a C++ map or set](#read-a-c-map-or-set)
- [2.2 Every Iteration of a Loop](#22-every-iteration-of-a-loop)
  - [Loop headers in a GCC build](#loop-headers-in-a-gcc-build)
- [4.7 C++ References and Pointers in SQL](#47-c-references-and-pointers-in-sql)
  - [Compare a reference's text in SQL](#compare-a-references-text-in-sql)
- [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish)
  - [Read a caller's local](#read-a-callers-local)
- [7.1 C and C++](#71-c-and-c)
  - [Quick start: what you can read and change](#quick-start-what-you-can-read-and-change)
  - [Read structs, arrays and standard containers](#read-structs-arrays-and-standard-containers)
  - [Build a program from several files](#build-a-program-from-several-files)
  - [Which C++ builds Peeky supports](#which-c-builds-peeky-supports)
  - [Name the tools in a config](#name-the-tools-in-a-config)
  - [What Peeky checks about a build](#what-peeky-checks-about-a-build)
  - [Why a local shows `no value yet`](#why-a-local-shows-no-value-yet)
  - [Read C++ members](#read-c-members)
  - [Read a C++ string that holds a NUL byte](#read-a-c-string-that-holds-a-nul-byte)
  - [Change a text](#change-a-text)
  - [Read a C++ map or set by key](#read-a-c-map-or-set-by-key)
  - [Change a field or an element](#change-a-field-or-an-element)
  - [Change a field behind a pointer](#change-a-field-behind-a-pointer)
  - [Same-named locals: which one a name means](#same-named-locals-which-one-a-name-means)
  - [Change at a statement's first instruction](#change-at-a-statements-first-instruction)

## 1.1 What Can I Ask?

Read [COOKBOOK 1.1](../COOKBOOK.md#11-what-can-i-ask) first.

### See each name's type

`describe` shows each name with its type, the way C or C++ code writes it:

- In C, a struct, union or enum that no typedef names is written with its tag:
  `r (struct ring *)`, `c (enum color)`. A typedef's name stays: `t (box_t)`.
- In C++, the standard library's types are written as code and probe tables write them:
  `parts (std::vector<std::string>)`, `p (const std::string &, lines 6-7)`.
- In C++, none of the compiler's own locals are listed, such as a range `for`'s `__range1`,
  `__begin1` and `__end1`.

For example, for `static int push(struct ring *r, int v)` on line 13 of a `ring.c`, `describe`
prints:

```
names in push (ring.c:13-19): r (struct ring *), v (int)
```

Probe rows, `session inspect` and `session get` show a value's type as LLDB names it, without the
tag: `r: ring * = 0x…`. `describe` reads the build's debug information, and writes the tag as C
code does. Both name the same type.

### How a C++ function's name is shown

LLDB names a C++ frame by its whole signature, about 100 characters long:
`trim_left(std::__1::basic_string<char, std::__1::char_traits<char>, std::__1::allocator<char>> const&)`.
So Peeky shows shorter names. [COOKBOOK 1.1](../COOKBOOK.md#how-a-functions-name-is-shown) shows
where each one appears.

- **A table's short name** drops the parameters, qualifiers such as `const` and `&`, and a
  template's return type: `int same<int>(int)` reads `same<int>`.
- It keeps the qualified name, the template arguments and operators: `main::$_0::operator()`,
  `Foo::operator bool`, `(anonymous namespace)::helper`.
- A Java frame keeps its own name: `Main.run(int)`.
- Two frames that would read alike, such as overloads stopped at one line, show the signature as a
  session shows it. If those still read alike, they show the whole name.
- **A session's text** keeps the parameters, and writes the standard library's types as code
  does:
  - inline namespaces such as `std::__1` and `std::__cxx11` are left out;
  - `std::basic_string<char, …>` reads `std::string`, and so do `std::wstring`, `std::u16string`,
    `std::u32string`, `std::string_view` and the streams;
  - a default template argument is left out where it is the default: `std::allocator`,
    `std::less`, `std::hash`, `std::equal_to`, `std::default_delete`.
- Frame names of other standard templates keep their default arguments.
- A name whose brackets don't balance is left as written.
- Sessions show the signature, and save nothing to the store. JSON, the store and SQL keep LLDB's
  whole name.

## 2.1 Fields and Indexes

Read [COOKBOOK 2.1](../COOKBOOK.md#21-fields-and-indexes) first.

### Read through a pointer

Follow a pointer with `.`, not `->`: `stats.total_us`.

A pointer to a scalar carries no length. So `values[0]` reads what it points at. To read the other
elements, read the array itself where it is declared, in the caller.

The same holds for a pointer to an array, a container or a struct. `pa[0]` is what it points at,
so `pa[0][1]` is `(*pa)[1]`. C would read `pa[1]` past what the pointer points at, so Peeky
refuses it, naming that selector.

- A typedef'd pointer (`RowPtr`, `const RowPtr`) works the same. The build's debug information
  tells it from a typedef'd reference, which indexes what it refers to.
- A pointer under a type name Peeky can't resolve is refused for indexing, rather than read either
  way. An example is a template's member typedef, such as `std::vector<T>::pointer` to a
  container.
- A NULL or unreadable pointer is refused: `p is NULL`.

### Read a C++ map or set

On a `std::map` or `std::unordered_map`, `m[1]` is the value for key 1, as in C++. It is never the
debugger's second entry. `m[#0]` is the first entry. A set has only `s[#0]`, `s[#1]`, and so on.
See [7.1](#read-a-c-map-or-set-by-key) for the details.

## 2.2 Every Iteration of a Loop

Read [COOKBOOK 2.2](../COOKBOOK.md#22-every-iteration-of-a-loop) first.

### Loop headers in a GCC build

GCC gives `for (;;)` and `do {` no code of their own. So LLDB moves their breakpoint into the
loop's body, which stops at each pass. The result's `moved` says so.

## 4.7 C++ References and Pointers in SQL

Read [COOKBOOK 4.7](../COOKBOOK.md#47-c-references-and-pointers-in-sql) first.

### Compare a reference's text in SQL

Use this to see which arguments `trim_left` changed. A `const std::string &`, or a pointer, is
saved as the debugger shows it, with its address first: `0x000000016d78b298 "apple"`. It never
equals a plain string's `"apple"`. So take the text after the address before you compare.

`examples/cpp/refs/trim.cpp` trims each argument's leading spaces. Capture the reference parameter
`s` in `trim_left`, then the result `name` in `main`, one run each:

```sh
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:6 --arg apple --arg " pear" --arg "fig " \
  --capture s --hits all --label argument
peeky probe examples/cpp/refs/trim.cpp --at trim.cpp:11 --arg apple --arg " pear" --arg "fig " \
  --capture name --hits all --label name
peeky sql "SELECT n.hit, s.value AS argument, n.value AS name,
  substr(s.value, instr(s.value, ' ') + 1) = n.value AS unchanged
  FROM evidence s JOIN evidence n ON n.hit = s.hit AND n.label = 'name' AND n.selector = 'name'
  WHERE s.label = 'argument' AND s.selector = 's'"
```

```
hit  argument                    name     unchanged
1    0x000000016d78b298 "apple"  "apple"  1
2    0x000000016d78b298 " pear"  "pear"   0
3    0x000000016d78b298 "fig "   "fig "   1
```

`substr(value, instr(value, ' ') + 1)` is the text after the address. An address has no space, but
the text may have several (`" pear"`). So cut at the first space only.

- Use it only on a reference's or pointer's value, one that starts with `0x`. On a plain string,
  it would cut the text at its first space.
- `no_addresses(value)` gives `0x… "apple"`. That is enough to compare two references, or one
  across runs. It still doesn't equal `"apple"`.
- To group and count such values, use `GROUP BY no_addresses(value)`, not `value`. Two references
  to the same text at different addresses are then one group.
- A text that Peeky couldn't check whole carries a note after it, so it never equals a plain
  string either: `"v" (shown up to a NUL at byte 1 of 3)`. This happens with libstdc++ and
  lldb-dap 18 ([7.1](#read-a-c-string-that-holds-a-nul-byte)).

## 5.1 Stop, Look, Change, Finish

Read [COOKBOOK 5.1](../COOKBOOK.md#51-stop-look-change-finish) first.

### Read a caller's local

In C and C++, a caller's local is withheld while its declaration's code hasn't finished:

- **A call in the declaration:** in `long result = twice(seed);`, the caller stores `result` only
  after `twice` returns. The same holds for `long multi =` on one line and `twice(seed + 1);` on
  the next.
- **A declaration line with no code of its own:** the local is stored element by element, on its
  elements' lines. Examples are an array of structs, a nested struct, or a 2-D array initialized
  over several lines.
- **A declaration still running on the caller's line:** Peeky reads the declaration from the source
  up to its `;`. In `struct trio t = { seed,` / `{ twice(3),` / `twice(4) } };`, the first line's
  code runs before the calls, and `t.p` is stored after them.
- **A loop's own variable:** a `for` or `while` loop's variable, as in `for (int i = 0; …)`, is
  withheld too while the body calls, on one line or several. The header's code also runs after the
  body. Peeky can't prove from the source that the loop ran its initialization. A `goto` into the
  body, or a call that a macro hides there, can skip or delay it. To read the variable, probe a
  line in the loop's body, or `step --out` back to that frame.
- **A variable declared in a header:** a variable declared in a `for`, `if`, `while` or `switch`
  header counts until the `;` after the whole statement. So it stays withheld while the body calls.
- **A declaration in another file:** every local declared in another file than the caller's line
  is withheld. An initializer can `#include` its rows, or be included itself.

The local is also withheld wherever the source doesn't show where the declaration ends:

- brackets that don't balance;
- between the declaration and its end, text the compiler may read differently from Peeky's scan:
  - a directive such as `#include`;
  - a line splice, or `<::`;
  - a macro of the program's sources and headers that leaves a bracket open, or holds a `;` outside
    its brackets;
- anywhere in the file, a `#line` directive.

How Peeky reads the source:

- C's trigraphs are read as `-std=c11` reads them: `??/` is a backslash. C++17 has no trigraphs.
- Digraphs (`<% %> <: :>`) are brackets.
- Lines are counted as the compiler's debug information counts them. `\r\n`, a lone `\r` and a
  lone `\n` each end a line; `\n\r` is two line ends.

## 7.1 C and C++

This section shows what Peeky reads and changes in C and C++. Start with the quick start. The
parts after it give the details.

### Quick start: what you can read and change

#### Read

- Read any local, parameter or global with a selector. Fields and elements: `s.end`,
  `bins[3].hi`, `grid[1][0]`. Through a pointer, with `.`: `ps.start`. A C++ member, through
  `this`: `this.head_`.
- A `std::string` reads whole. A `std::vector` reads by index, and a `std::map` by key: `m[1]`,
  `ages["al"]`.
- Structs, arrays and most standard containers show their members, in probes, `inspect` and
  `session get`. `bins` reads `[{lo = 0, hi = 2, count = 0}, …]`, `bins[3]` reads
  `{lo = 6, hi = 7, count = 0}`, and a `std::vector<std::string>` reads
  `size=3 ["users", "42", ""]`. So a capture reads a container's first elements without a session
  ([details](#read-structs-arrays-and-standard-containers)).
- A `std::vector`, `std::array` or `std::map` row shows no elements in an optimized build, or in
  a program running several threads. Nor does it while a library call changes the container.
- Unions, and what pointers point at, are never read into a preview.

#### Values Peeky withholds or can't vouch for

Peeky doesn't guess. Where it can't vouch for a local's value, it withholds it or marks it.

- **`no value yet`** means the local's storage may still hold leftovers. The local is declared
  below the stop, its initializer is still running, or a jump went past its declaration. The row
  says why, and where to read it: `stop after line 35 to read it`. Read it below its declaration
  ([details](#why-a-local-shows-no-value-yet)).
- **A `note`** marks a value Peeky shows but can't vouch for: on a `do` body's first line, or below
  a `setjmp` call. Every field and element read through it carries the note too:
  ``read through `q`: …``. The note is in captures, in `session get` children (marked `[note]`) and
  in the evidence. Don't trust a value marked with a `note`, or anything read through that value.
  `--set` refuses them wherever it refuses the local.
- On a line a loop returns to, such as a `do` body's first line, read the value on the next line.
- Below a `setjmp` call, a local declared at or below the stop shows a `note` instead of
  `no value yet`. It reads plainly below its declaration.
- **Same-named locals:** a plain name means the declaration the code names at the stop. Write
  `name @ file:line` to select another
  ([details](#same-named-locals-which-one-a-name-means)).

#### Change

- `--set` changes integer locals of 16 to 64 bits, within their range. It also changes integer
  fields and elements of a struct, class or array local that the frame holds itself:
  `--set s.end=10`, `--set 'bins[3].hi=8'`. An 8-bit `char` is refused.
- Parameters are set like other locals: an unoptimized build keeps them on the stack.
- A `char[N]` or `std::string` local takes a text in double quotes: `--set 'name="apple"'`. The
  text must fit the local's size; Peeky never reallocates. lldb-dap 21 (macOS) writes every case;
  older versions write fewer ([details](#change-a-text)).
- Pointers and floats can't be set, and a text can be set only as above.
- Peeky never changes a value inside a union, a packed struct, a bitfield, a const, or a `std::`
  type.
- **Never through a pointer or a reference.** That rules out `ps.end`, `this.count`, and a C
  `__block` variable. It also rules out a C++ structured binding of a reference: `n` or `at.x` in
  `auto &[at, n] = *owner`. Such fields are marked `[not settable: reference]`. To change one, set
  it in the frame that owns the struct, before the call
  ([details](#change-a-field-behind-a-pointer)).
- In C, the program's sources may call the function only once. Then a refusal for a pointer
  parameter names that call:
  ``push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable``. If that
  caller only passes its own pointer on, the refusal follows the calls to the frame that owns the
  struct. Or it says that it can't.
- Sometimes the build's debug information rules out a probe's `--set` like this in every function
  on its `--at` line. Then the probe is refused before the program runs. The debugger's launch
  request isn't sent, and no run is saved.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture 'start,page,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   25    main      start     int   3  [settable]
1    observed   25    main      page      int   1  [settable]
1    observed   25    main      items[1]  int   20  [settable]
1    after-set  25    main      start     int   0  [settable]
after-set: 2 rows equal to the hit's observed value left out (`--json` has every row)
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.745
output:
  PAGE:[10, 20, 30]
```

#### Where and when to change

- Change a value only at a statement's first instruction. A breakpoint on the statement's first
  line qualifies (`--at`, `session continue --to LINE`), and so does a `step` that lands there.
  After `step --out`, or on a loop header's later passes, a change is refused
  ([details](#change-at-a-statements-first-instruction)).
- To change a value at a later hit of a probe, add `--set-hit N`. In a session, change values at as
  many stops as you like.
- A caller's frame is never changed: a call it is making may hold a copy of the old value.
  `p.x + f()` loads `p.x` before it calls `f`.
- Rows mark what `set` would change `[settable]`, fields and elements included, a preview's too.
  The fields of a local that the debug information keeps in registers, or as a static, say
  `[not settable: register]` or `[not settable: static]`.
- When a rule leaves nothing settable, `session inspect` says why. A refused change's hint names
  the one way around it.

#### More to try

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture start,page --arg 2     # PAGE:[70, 80]
peeky probe examples/cpp/pagination_bug.cpp --at 28 --capture start
```

### Read structs, arrays and standard containers

A struct, a class or a fixed-size array shows its first members. lldb-dap alone shows such a value
as its type and address: `bins  bin[4] @ 0x16bb9f278`. Peeky writes a preview from the debugger's
own children instead, in the form LLDB's `frame variable` uses. A 2-D array reads
`[[1, 2, 3], [4, 5, 6]]`. For example, take a `histo.c` that counts its arguments in
`struct bin bins[4]`, where each bin is `{lo, hi, count}`. One capture reads every bin:

```sh
peeky probe histo.c --at histo.c:28 --arg 0 --arg 7 --arg 7 --capture 'bins,bins[3]'
```

```
hit  phase     line  function  selector  type    value
1    observed  28    main      bins      bin[4]  [{lo = 0, hi = 2, count = 0}, {lo = 2, hi = 4, count = 0}, {lo = 4, hi = 6, count = 0}, {lo = 6, hi = 7, count = 0}]
1    observed  28    main      bins[3]   bin     {lo = 6, hi = 7, count = 0}
```

`session get bins` shows each element the same way, with its selector:
`bins[3]: bin = {lo = 6, hi = 7, count = 0} (children: peeky session get 'bins[3]')`. The preview
replaces the value in probe rows, `inspect`, `session get` (the root and every child it lists) and
the evidence store. The handle stays, so you still read and set fields by name: `bins[3].hi`,
`--set 'bins[3].hi=8'`. They are marked `[settable]` as before.

#### What a preview shows

A preview shows 8 members or elements, then `…`. It goes two levels deep, with `{…}` below that:
`{s = {a = {…}}}`. It shows at most 40 characters of one member, and 240 in all. A char array or a
string is cut between characters, with LLDB's `...`. A note on a string is kept whole.

- Previews have their own budget: 256 debugger requests within 3 s, per inspection or expansion.
  Past it, values keep the debugger's text. The strings' checks have a budget of their own.
- A probe previews only the values it captures. A selector reaches a field without previewing the
  listings on its way. So a probe that captures scalars costs nothing more.

| Member | In a preview |
| --- | --- |
| A union, under any name the debug information gives it, such as a typedef's | never previewed: `<union>` inside another value, an anonymous one too, because only one of its members holds what was stored |
| A pointer | its address, never what it points at: not a `char *`'s text, nor a function's name |
| A reference member | its address only |
| A member with no value | `<no value>` |
| A C++ base class | a member named by its class: `{Base = {b = 1}, d = 2}` |
| A C++ vtable pointer: `_vptr$Base`, or GCC's `_vptr.Base` | left out: it isn't one of the program's members |
| An anonymous struct | its members in braces, with no name: `{kind = 1, <union>, {u = 5, v = 6}}` |
| A `std::string` | its checked text ([details](#read-a-c-string-that-holds-a-nul-byte)). With libstdc++ and lldb-dap 18: `<no value>`, and the member's own row says why |
| A standard container | as below: `v = size=2 [1, 2]` |
| Any other `std::` value | its debugger's summary, or `{…}` |
| A `char16_t`, `char32_t` or `wchar_t` bitfield | no value: LLDB reads its bits wrongly ([below](#characters)) |
| Another bitfield | as LLDB reads it |
| An unnamed bitfield that LLDB adds to fill a gap | left out |
| A variable-length array, or a flexible array member, inside a struct | `[…]` |

A reference local or parameter to a class shows its address, then the preview:
`0x… {lo = {x = 0, y = 0}, hi = {x = 2, y = 3}}`. A `const std::string &` shows its address and
text the same way. A reference to a union, or to a `std::` type other than a string or a
container, shows no preview.

- A pointer local shows no preview. `p[0]`, which a selector asks for, is previewed when it is a
  struct, a class or an array. It is never previewed when it is a pointer itself: `pp[0]` shows
  its address.
- An array's length is the debug information's constant bound. A typedef'd array, such as
  `typedef int Row[3]`, is an array: `[1, 2, 3]`.
- A value shown with a `note` keeps the note with its preview. So does what is read through it:
  `late.lo = {x = …, y = …}`, with ``read through `late`: …``. This holds in captures,
  `session get` children and the evidence's `note`.

#### When there is no preview

Types come from the build's debug information, not from the debugger's type names. So
`Hold<int[1]> hs[5]` previews its five elements: the `[1]` in its name is a template argument.
The debugger's children give the values, matched to the type's members by name, and an anonymous
member by its order. When Peeky is unsure, there is no preview, only the debugger's `T @ 0x…`.
That is the case for:

- a local withheld at the stop;
- a member that the type lacks, or an array whose listed elements differ from its type;
- a member that the type says is a number, but the debugger lists as a struct;
- a program that uses `setjmp`: Peeky reads no declarations there;
- a local declared twice, with two types;
- a variable-length array: its bound is a variable or an expression, and debuggers misread it.
  clang read another VLA's length, and GCC a wrong one;
- an incomplete type, declared but never defined, such as `op[0]` on a `struct opaque *` or an
  `SSL *`;
- a map's entry, such as `m[#0]`, a `std::pair`;
- what a standard-library type other than a container holds;
- a class with a member named like one of its base classes: `struct B2 : A2 { int A2; }`;
- an optimized build. Optimized builds have no previews, except wide character arrays, which are
  shown element by element. A struct there may hold values that the optimizer hasn't stored yet:
  GCC `-O2` showed `t = {x = 64, y = 0, z = 0}` where the program printed `{5, 6, 7}`.

#### Standard containers

- A `std::vector`, `std::array`, `std::deque` or `std::list` shows its size and first elements,
  within the bounds above: `have: size=3 ["users", "42", ""]`.
- Elements show as their type shows: `size=2 [{x = 1, y = 2}, {x = 3, y = 4}]`,
  `size=2 [size=2 [1, 2], size=1 [3]]`.
- A `std::map` or `std::unordered_map` shows its entries in iteration order, as `key: value`:
  `size=3 {1: 10, 2: 20, 5: 50}`, `{"a": {x = 1, y = 2}}`.
- A set (`std::set`, `std::multiset`, `std::unordered_set`, `std::unordered_multiset`) shows its
  elements: `size=2 {1, 3}`.
- A `std::multimap` keeps its summary, `size=2`: its keys repeat.
- These keep the debugger's own view too: `std::forward_list`, container adaptors such as
  `std::stack` and `std::queue`, `std::pair` and `std::tuple`.
- A container nested four levels below a local, such as `grid.idx[1]`, is past the inspection's
  depth. It shows its summary.
- Selectors work as usual: `m[1]` reads key 1, and `s[#0]` the first element
  ([details](#read-a-c-map-or-set-by-key)).
- A map's listing shows each value's preview: `session get named` lists
  `named["a"]: Pt = {x = 1, y = 2}`. A map's value of your own class, `mp[1]`, is previewed too.
- A reference shows its address, then the container: `0x… size=3 [1, 2, 3]`. A pointer shows its
  address and LLDB's summary, `0x… size=3`, and `p[0]` is the container.
- A container not yet constructed is `no value yet`, like any local. In an optimized build, a
  container shows its summary alone.
- A moved-from container shows what it holds. libc++ and libstdc++ leave it empty, `size=0 []`, as
  the program's `size()` says.

Where the elements come from: the debugger's children, which its formatter reads from the
container's own storage, `size()` of them. Each shows as the class template's argument says in the
debug information (`_Tp`, `_Key`), never by LLDB's type names. A container that the debugger lists
another way keeps its summary: its own members, or fewer elements than its size. LLDB lists a
`std::array` by its one C-array member: libc++'s `__elems_` (`ai.__elems_`), or libstdc++'s
`_M_elems`. Peeky shows that array's elements once the debug information says it holds `_Size`
elements of `_Tp`.

#### A container that something may be changing

Sometimes a standard-library function, or a constructor, destructor or assignment operator, may be
part-way through changing a container. Then the container shows its summary alone (`size=2`). Its
elements get no preview of their own; a struct element shows `Noisy @ 0x…`. This happens:

- **In a program running more than one thread.** A stop pauses the other threads wherever they
  are. Peeky counts the threads lldb-dap lists, those LLDB's `thread list` lists, and those that
  said they started. The program counts as one thread only where they all agree. Peeky withholds
  where any of them doesn't answer, or where a thread has no ID.
- **In a caller frame below a library function,** or below a constructor, destructor or
  `operator=` of any class. For example: `session get o.items --frame N` on `main`'s frame while
  `o.items.clear()` runs an element's destructor, with the vector still counting an element
  already destroyed. Or `slot->~E(); ::new (slot) E(3);` in `main`, stopped in `E::~E()`.
- **For a container reached through a pointer or reference,** wherever the stack holds such a
  frame. Examples are `this.owner[0].items`, a reference parameter, a structured binding of one
  (`auto &[items, n] = *owner`), and a static local. A local that the debug information reaches
  through a pointer counts too: a class passed by value through a hidden pointer, or a named
  return value. Such a frame can also be a `std::thread`'s, a `std::function`'s, or the frame's
  own constructor or destructor. The same holds where Peeky can't read the stack whole: 1,024
  frames or more, or a reply short of its `totalFrames`.

A library function is one whose name, before its parameters, starts with `std::`, `__gnu_cxx::`
or `__gnu_debug::`, or one with no name. Peeky knows a constructor by the debug information or by
its name, and a destructor or assignment operator by its name: `E::~E()`,
`E::operator=(E const&)`.

In all three cases, the container's elements are `not shown`, with the reason. So is everything
read through them: `o.items[1].name`, `shared[0].x`, a map's entry. With several threads, every
container's elements are `not shown`. Peeky can't tell which container the call changes, so it
withholds every container that the frame reads this way (`o.tags[1]` too). Values not read
through a container, such as `o.dropped`, read as usual.

The row says why, and when to read it:

```
not shown: a standard-library call below this frame (std::__1::vector<…>::clear) may be part-way through changing `o.items`, which this is read through (…); read it after that call returns
```

- Through a pointer or reference, it ends:
  `… while a standard-library call runs on this thread (…)`.
- For a lifetime under way, it names `a constructor, destructor or assignment operator`.
- With several threads, it says:
  ``the program runs 2 threads: a stop pauses every other one wherever it is, perhaps part-way through changing `shared` …``.
- Such a row has no handle. So a selector below it is refused with the same reason. A map's key
  lookup says it too: ``cannot read the keys of o.tags's entries: not shown: …``.

#### Characters

- A C char array shows as LLDB writes it, bounded by its size, so it needs no NUL. It is cut at
  40 characters.
- A wide character array shows element by element: `[L'a', L'\0', L'b', L'\0']`. LLDB's own text
  would stop at the first NUL: `L"a"`. This holds for any element wider than a byte. `char16_t` and
  `char32_t` arrays show element by element too: `[u'x', u'\0', u'y']`.
- A char-typed bitfield shows as its number: `sc = -3`, not LLDB's `'\x05'`. LLDB reads it as an
  unsigned character. Peeky shows the number its bits hold, sign-extended where its type is signed.
  Its member row shows the same: `b.sc = -3`.
- A plain `char` is signed or unsigned as the ABI makes it: signed on macOS and x86-64, unsigned on
  Linux arm64.
- A bitfield of a wide character type has no value: `char16_t w : 9`, `wchar_t`, `char32_t`, and
  in C a `wchar_t` by its typedef's name. A preview shows `w = <bitfield>`, and its own row says
  `not shown: a bitfield of a wide character type …`. LLDB reads it at other bits than the
  program's: `u'a'` showed as U+030D.

How Peeky reads these values is in [NATIVE_INTERNALS.md](../NATIVE_INTERNALS.md), for maintainers.

### Build a program from several files

- All `.c` files in the directory that don't define their own `main` are built with the target.
- A directory that holds several programs links only the target's `main`.
- A directory works as the target too: `peeky probe ring/ --at 16` finds its one program. A
  checker script beside it doesn't count.
- When the directory holds several programs, name one in `--at`: `--at ring.cpp:16`.
- Headers read from outside the directory, SDK headers included, are hashed and rechecked
  ([details](#what-peeky-checks-about-a-build)).

### Which C++ builds Peeky supports

Peeky builds `.cpp`, `.cc` and `.cxx` files as C++17, with debug information, and only with
clang. Its C++ rules are checked against clang's debug information alone. Those rules cover
locals declared below the stop or not yet initialized, previews, map lookups, libstdc++ strings,
setting a string's text, and same-named locals. C builds with clang or GCC.

Before a C++ build, Peeky asks the compiler for its `--version`. A compiler that doesn't say
`clang version` is refused before anything is built:

```
UNSUPPORTED: C++ targets need clang++ (Peeky's C++ rules are checked against clang's debug info); set PEEKY_NATIVE_CXX to clang++ (/usr/bin/aarch64-linux-gnu-g++-13 is GCC)
```

To fix it, set `PEEKY_NATIVE_CXX` to a clang++.

- Peeky goes by what the compiler says, never by its name. macOS's `g++` is Apple clang, so it is
  accepted.
- A `ccache` or `distcc` symlink named `clang++` is refused: its own `--version` isn't clang's.
- A wrapper script that runs clang++, such as `exec ccache clang++ "$@"`, passes clang's
  `--version` through. So it works.
- Peeky asks once per compiler file, which it knows by its path, modification time and size.
- A compiler that doesn't answer within 10 s is refused as timed out. Peeky ends it, and every
  process in its process group.

Peeky's C++ support doesn't cover these:

- the debugger's views of other standard-library types;
- overloaded operators;
- expression evaluation;
- reassigning a pointer or a reference;
- changing a whole class object;
- C++ build systems.

### Name the tools in a config

Without a config, Peeky finds the compiler, lldb-dap and llvm-dwarfdump through `PEEKY_NATIVE_*`,
`xcrun` or `PATH` ([COOKBOOK 0](../COOKBOOK.md#what-each-language-also-needs)). To pin them, pass
a trusted `--config` with their absolute paths. On macOS, `xcrun` finds each one, even when it
isn't on `PATH`:

```sh
xcrun --find lldb-dap
xcrun --find clang
xcrun --find llvm-dwarfdump
xcrun --find dsymutil
xcrun --show-sdk-path
```

For example, with the Command Line Tools:

```json
{
  "sources": ["branch_bug.c"],
  "headers": [],
  "arguments": [],
  "compiler": "/Library/Developer/CommandLineTools/usr/bin/clang",
  "adapter": "/Library/Developer/CommandLineTools/usr/bin/lldb-dap",
  "dwarfdump": "/Library/Developer/CommandLineTools/usr/bin/llvm-dwarfdump",
  "dsymutil": "/Library/Developer/CommandLineTools/usr/bin/dsymutil",
  "sysroot": "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
}
```

| Key | What it names |
| --- | --- |
| `sources` | the program's source files |
| `headers` | its headers |
| `arguments` | the program's arguments |
| `language` | `"cpp"` for C++ ([WORKER](../WORKER.md#configure-a-c-target)) |
| `compiler`, `adapter`, `dwarfdump` | the compiler, lldb-dap, and the tool that reads the debug information |
| `dsymutil`, `sysroot` | on macOS: the tool that gathers debug symbols, and the SDK |
| `optimization` | `"Og"`, `"O1"` or `"O2"`; `"O0"` by default ([7.5](../COOKBOOK.md#75-optimized-builds)) |

- Use the paths on your machine. The ones above are one development machine's.
- On macOS, set `sysroot`. Without the SDK's path, a plain clang call couldn't find `stdio.h`.
- On Linux, use the pinned distribution tools. Leave out `dsymutil` and `sysroot` unless your
  toolchain needs them.
- The worker takes the same file, with `--native-config` ([WORKER](../WORKER.md)).

### What Peeky checks about a build

Peeky builds the declared sources in a fresh workspace, with debug information. It records what
went into the build, and checks it again before it trusts the program.

- The build's record covers the declared sources and headers, the compile flags, the tools'
  identities and the output files. The optimization level is part of it: `compiler_options` and
  `target_identity`.
- On macOS, Peeky keeps and hashes the generated dSYM. On Linux, it uses the debug information in
  the executable.
- Each stop is checked against the prepared source location. The loaded executable is checked
  against the one built.
- Missing debug information fails closed. So does a change to the original or copied source, the
  executable or the symbols.
- Files that a compile reads outside the workspace, such as SDK headers or an absolute `#include`,
  are hashed and rechecked like declared ones. Peeky finds them in the compiler's `-MD` dependency
  file. An entry that could be read two ways fails the build's preparation.
- Peeky preprocesses each source before it compiles anything. It refuses inline assembly that can
  read a file, with `UNSUPPORTED`: `.incbin`, `.include`, or an assembler macro that could spell
  one. No dependency file lists such a file ([ERRORS](../ERRORS.md)).
- Peeky builds no `.s` or `.S` file, and passes no `-Wa,` option.
- A source's name may hold any character: `main\u2028x.c`, `größe.c`.

These checks aren't a sandbox. The program, the compiler, the debugger and the symbol tools run
with your permissions. System headers, libraries and the dynamic loader aren't fully hermetic. So
the checks don't prove that a hostile process can't interfere, nor anything about every loaded
library.

### Why a local shows `no value yet`

Peeky withholds a C or C++ local when its storage may still hold leftovers. The row says
`no value yet:` and why. In short, a local is withheld when it is:

- declared below the stop;
- in an initializer that is still running;
- past a `case` or `goto` label;
- `this`, in a constructor;
- hidden by a block's `static` or `extern`;
- declared on the stop line, where control enters that line: `declared on line 3, which has not
  run yet`. Part-way through the line, after `step --out`, the rules below decide;
- a parameter or local of a function stopped at its start, before it stores its parameters. A GCC
  build's one-line function stops there.

The cases in detail follow.

#### Declared below the stop

Until its declaration runs, a local's storage holds whatever an earlier call or loop pass left
there. So the row says `no value yet: declared at line 18, below this stop`.

- The declaration that counts is the one the debugger shows: the one in the stopped function whose
  block holds the stop. Another of the same name, in an earlier loop or a lambda, changes nothing.
- Where Peeky can't tell which one the debugger shows, any declared below the stop withholds it.
- A static or a parameter keeps its value.
- Sometimes Peeky can't place the stop before the declaration. The line may also run code after
  the declaration, as a temporary's cleanup does, or a `goto` label may let control return above
  it. The local is withheld all the same, and the row says it may not be initialized.

#### On the stop line, or part-way through it

- **At the line's first instruction,** a local declared on the stop line is withheld: its
  declaration hasn't run yet. To read it, probe the next line.
- **On a line a loop returns to,** such as a `do` body's first line, it has a `note`: it may be
  the last pass's value.
- **Mid-line,** such as a `for` counter on later passes, or after `step --out`, it shows as its
  storage reads, where the rules below allow it.

While the line still has code to run, a local declared on it is withheld. An example is coming
back from a call with `step --out` in `long r = twice(3) + twice(4);`.

One exception: a variable that a `for`, `if` or `switch` header initializes from literals alone
reads as usual. A one-line loop keeps it live. In C, each declarator before it must be
initialized from literals too: `for (int i = 0, j = 1; ...)`. In C++, only the first declarator
counts, because constructing one before it may call a function. These are still withheld:

- a `while` header's variable;
- one with no initializer: `for (int i; ...)`;
- one whose initializer, or an earlier declarator's, calls or names anything:
  `if (long r = twice(3))`, or `int i = INIT`, where `INIT` may be a macro that calls;
- one that a jump can reach past its declaration:
  - `goto inside;` to a label in the loop's body;
  - a `case` of a `switch` around the loop;
  - any label, once the source has a GNU `asm goto`, whose labels are operands.

#### At a function's start, before it stores its parameters

A function's prologue runs first: it sets up the frame and stores the parameters there. Until it
has run, the frame's storage holds what an earlier call left. So a stop in the prologue withholds
every parameter and local: `no value yet: the stop is at its function's start, before the
prologue stores the parameters`. These keep their values:

- a `static`;
- what an optimized build's location list places;
- a parameter the caller passed on the stack: past the eighth on arm64, or the sixth on x86-64.

Clang marks where the prologue ends in the line table, and LLDB stops there. GCC marks nothing. So
LLDB looks a few rows ahead for a mark, or for a row of another line. If what it finds is past the
function's end, LLDB stops at the function's first instruction. That is common in a function whose
code is all on its first line. A `step --into` stops there too.

For `static int one(int a) { return a * 2; }`, a GCC build stops on `one` before `a` is stored:

- read the argument at the call, on the caller's line;
- or put the body on a line of its own: `static int one(int a) {`, then `return a * 2;`. LLDB then
  moves the breakpoint to that line, past the prologue, and says so;
- or build with clang.

A function whose body has lines of its own usually stops past its prologue. Rarely, it stops at
its start too, as when the next function's prologue is empty. Then stop on a later line of the
function. A change at either stop is refused for the same reason.

Peeky places a stop by its address, which needs the debugger to report where the program is
loaded. Where it doesn't, any stop on a line where a prologue runs is withheld the same way. That
includes clang's, where a fault or a signal can stop. The row says `Peeky cannot tell whether the
stop is before or after its function's prologue stores the parameters`, and why.

#### While its declaration runs

A local is withheld while its own declaration is still running at the stop. For example,
`struct outer o = {` runs over lines 17-22; an array of structs or a 2-D array can do the same.
Stopped at line 20, the row says `its declaration (lines 17-22) is still running at this stop`,
until its `;` has run.

- Peeky finds the end in the source, read as for caller frames ([5.1](#51-stop-look-change-finish)).
- Where it can't find the end, the local is withheld.
- A declaration below the stop whose own line has no code is placed by its elements' lines.

A variable that a `for`, `if`, `while`, `switch` or `catch` header declares is different. It has
run with its part of the header, so the body reads it.

#### Below a label

Below a label that a jump can reach past a declaration, the local is withheld too:
`a jump ... can reach the label on line 21`. Such a label is:

- a `case` or `default` of the `switch` whose body holds the declaration;
- a label that a `goto` above the declaration names;
- a label that a `goto` outside its block jumps back to. Re-entering a block starts its locals
  anew (C11 6.2.4);
- any label, when the function has a computed `goto`.

A `goto` counts only within its function's own lines, so a namespace's other functions don't.

#### Below a `setjmp` call

In a function that calls `setjmp`, or a macro that expands to it, look below the call. There, a
local declared at or below the stop is shown with a `note` instead, and can't be changed. A
`longjmp` can bring control back past its declaration with the local live, or this is the first
pass.

- A stop on the `setjmp` call's own line isn't affected, because a `longjmp` returns past the call.
- Locals declared above the stop, and every other function, are judged as usual.

#### A name a block declares again

Inside a block that declares an outer local's name in another way, the code names the inner one.
The outer local's row says `not what the code reads`, and it can't be changed. Such a declaration
is:

- a `static`, `extern` or `typedef`;
- an enumerator, also one of an enum inside a C struct's body;
- a function declaration;
- a C++ `using` declaration, class or structured binding: `auto [t, u]`. A range-`for`'s or an
  `if`'s binding counts too, though clang 18 on Linux doesn't describe it. A range-`for`'s binding
  counts in its loop. It doesn't count in a lambda in its range, nor at the line's first
  instruction, where the range runs first;
- a `thread_local`, which the debugger lists apart as `q @ file:line`.

A block that opens on the stop line counts.

Peeky reads the source as the build's compiler preprocesses it (`-E`, with the same flags). So
whatever a macro expands to counts, and only what a conditional keeps. A prototype's parameter
names don't count. Where Peeky can't read the source that way, the row says
`may not be what the code reads`: the preprocessor fails, or a system header is included inside
the block.

In optimized builds (`-O1`, `-O2`), a block's declaration hides the outer local too. This holds
though the outer local lives in a register or as a constant, not in the frame:

- On a range-`for`'s line, the stop can't be told from the line's first instruction, where the
  range runs with the outer local. The row says `may not be what the code reads` and names the
  binding.
- A block's `static` that never changes is kept as a constant, and lldb-dap lists it among the
  frame's locals as `t @ file:line`. That row is the static the code names, so it is shown. The
  outer local's row says the static is `listed separately as` it.

#### Declared in another file, or outside the function's own lines

- A local declared in an `#include`d file counts as declared at that `#include`'s line. Below the
  stop, it is withheld. Above the stop, it is withheld when its file may `#include` more after it,
  because a `case` there can skip it.
- Sometimes the frame runs a line outside its function's own lines. Examples are a class's default
  member initializer, a class defined inside the function, or an `#include`d fragment. You hit the
  first when you break on `long b = twice(2);` in `struct Cfg`. The frame may be the stopped one,
  or a caller on its own file's line.
- Then the usual rules run from the function's own line that brings that line in (the `#include`).
  If there is none, they run from the last own line whose code comes before it.
- The frame's class, struct and array locals are withheld too, since one of them is being built
  there. Where Peeky can't place the stop, everything but the parameters is withheld.

#### `this` in C++

In C++, `this` is withheld while its object is being built:

- in a constructor;
- in any frame a constructor called. Peeky reads up to 64 frames to tell; past that, it
  withholds;
- in a lambda defined inside a declaration or a constructor that may still be running. The note
  names that declaration;
- in a lambda that a class's default member initializer defines: `long b = [this] { ... }();`. A
  class with no constructor runs its initializers where the object is built.

An unnamed struct's constructor (`$_0::$_0()`) counts as a constructor too.

### Read C++ members

- In a C++ method, read members through `this`: `this.head_`. A bare `head_` that is a member
  gives an error that names that selector. Java methods work the same way.
- C++ changes no member through `this`, because `this` is a pointer. Java does: `this.total=0`.
- Peeky never reads a member in place of a local. The debugger's list of locals can be
  incomplete, for example for statics or classes built without local-variable tables. So a missing
  local doesn't mean that the code uses the member.

### Read a C++ string that holds a NUL byte

LLDB shows a libc++ (macOS) `std::string` whole, with a NUL as `\0`: `"a\0b"`. A libstdc++ (Linux)
string is different:

- LLDB's summary of it is a C string, which ends at the first NUL.
- On clang's Linux builds, a value LLDB names `std::string` gets
  `error: summary string parsing error`.

So on Linux, the worker checks each string against its `_M_string_length`. It does this in
`inspect`, `get`, listings, probe rows and the evidence store alike.

| Debugger | What you see |
| --- | --- |
| One that reads memory (lldb-dap 20) | Peeky reads the string's bytes and shows them as LLDB shows a libc++ string: `"a\0b"  [memory]`. Past 1024 bytes: `"..."...`. This covers `std::string`, `std::wstring`, `std::u16string` and `std::u32string`, by value, by reference (`0x… "a\0b"`) or by pointer. |
| One that can't (lldb-dap 18) | A text the debugger cut at a NUL keeps a note: `"v" (shown up to a NUL at byte 1 of 3)`. A string whose summary failed has no value: `no readable value: the string's summary failed ..., and this debugger cannot read memory (lldb-dap 20 can); it holds 3 bytes`. A pointer or reference to one shows `0x… <no text: ...>`. |

- A string whose length checks out keeps the debugger's text. So does a `char` string that LLDB
  cut at 1024 bytes, marked `...`, with no NUL before the cut.
- A text read from memory uses LLDB's libc++ escapes: `\0`, `\t`, `\"`, `\U0000001b` for a
  control character, and `\xff` for a byte that starts no UTF-8 character. Wide strings keep their
  prefix: `L"…"`, `u"…"`, `U"…"`.
- A string that the build's debug information gives no length member has no length to check.
  For example, clang leaves `std::wstring` to libstdc++'s own debug information, and libstdc++'s
  old string ABI has no `_M_string_length`. Its text gets `(not checked for a NUL byte: ...)`, or
  no value where LLDB's summary failed.
- A string under a typedef's name is checked the same way: `typedef std::basic_string<char> Bytes`,
  `using Name = std::string`, a template's member typedef, or a reference or pointer to one. Peeky
  resolves the typedef from the build's debug information, or knows the string by its libstdc++
  members.
- LLDB's `error: summary string parsing error`, `error: no target` and
  `error: no ScriptInterpreter` are never shown or saved as a value, for any C or C++ type. A value
  with only that text is unavailable:
  `LLDB's summary of this value failed (...); select its members`. After a pointer's address, it
  reads `0x… <no text: …>`.
- LLDB's text gives no sign of a NUL it dropped. So Peeky attempts to check each libstdc++ string,
  within the inspection's budget of 128 requests and 10 s, per inspection or expansion. Each string
  costs a debugger request, or two or three where its bytes are read. So a large page of strings,
  such as a big `std::vector<std::string>`, may say `(not checked for a NUL byte: ...)` instead. A
  libc++ string costs nothing.

On every platform:

- A literal reads back in C as the same bytes. Where LLDB writes `\0` before a digit (`"n\064"`,
  which C reads as `"n4"`), Peeky writes `"n\00064"`, or `"x\0005"`. Where LLDB writes `\xff`
  before a hex digit, Peeky writes `"\377a"`. Everywhere else the text is LLDB's, unchanged:
  `"a\0b"`, `"\08"`.
- A long text stays within 2000 characters, its address and note included. It is cut between
  characters, with `...`.
- A string is its text. `session get name` shows `name: std::string = "apple"` and no rows under
  it. The library's own members, `(anonymous struct)` or `_M_dataplus`, are never listed, and
  `inspect` offers no `(children: ...)` for a string.
- A struct's, vector's or map's listing marks its strings as not expandable. A string never counts
  toward the depth limit.
- A string that LLDB shows without text keeps its members: `std::u16string @ 0x…` with
  lldb-dap 18.
- Selecting inside a string (`name[0]`, `name.size`) is refused. The error says what to do
  instead: capture the string and compare its text in SQL
  ([4.7](#47-c-references-and-pointers-in-sql)).
- A pointer to a string shows its address and text, and `p[0]` is the string. `p[1]` is refused,
  as for any pointer.
- A `std::string` local takes a text in double quotes, within its capacity: `--set 'name="apple"'`
  ([Change a text](#change-a-text)). Its NUL bytes count as text there too: the new size is
  written with the bytes.

#### Strings Peeky doesn't check

- With lldb-dap 18, a libstdc++ `std::string` has no text, nor does a typedef of that name: its
  summary fails.
- An incomplete `std::wstring` has no text, with either debugger.
- A string inside another value's own summary isn't checked. Vectors, maps and structs show no
  such summary.
- A `const char *` stays a C string, as C has it.
- `std::string_view` isn't covered.
- A libc++ string under a type name Peeky can't resolve, such as a template's member typedef that
  the debug information doesn't give, still lists its `(anonymous struct)`.
- Other nameless children, such as a union inside a struct, are listed as `[not selectable]`.

### Change a text

A value in double quotes is a text, in JSON's string syntax, so `\"` and `\u00e9` work. Peeky
writes the text in the storage the local already has, and only there.

- **A `char[N]` array** takes the text and a NUL, when both fit in its N bytes. The build's debug
  information gives N. The bytes after the NUL stay as they were, and the debugger shows them after
  `\0`: `set name: "apple :4" -> "apple\0:4"`.
- A text that doesn't fit is refused: `name` is a char[16], which holds 15 bytes of text and a
  NUL. A text with a NUL in it is refused too.
- **A `std::string`** takes the text, a NUL and the new size, within its current capacity. Peeky
  never reallocates: `set name: "apple " -> "apple"`. The text goes in its own short buffer
  (libc++: 22 bytes; libstdc++: 15), or in its heap buffer.
- Peeky reads where the text is held from the debugger's members, and checks that against memory.
  A longer text is refused: `with room for 22 bytes, and the text is 30 bytes`. So is a layout that
  doesn't check out, such as a string not yet constructed.
- A heap buffer's room is the string's `capacity()`. In libc++, `__cap_` holds the allocation's
  size (macOS arm64's layout), or half of it (its standard layout). Where its short buffer lies
  tells which.
- **Views taken before the change keep the old size.** This covers a `std::string_view`, a
  `c_str()` or `data()` pointer, and an iterator. Such a view reads a mix of old and new text.
  After `string_view sv = s;` then `s="bye"`, `sv` reads `bye\0o world`. Peeky can't see
  those views, so every `std::string` change says so: a `note` on the change in JSON, and a
  warning.

Which debugger can write what:

| Debugger | What Peeky can write |
| --- | --- |
| lldb-dap 21 (macOS) | every case above, because it writes memory |
| lldb-dap 20 | an array's text, one element at a time, printable ASCII only; a libstdc++ string's text only in its short buffer, because lldb-dap 20 reads memory to tell where the text is |
| lldb-dap 18 | an array's text, one element at a time, printable ASCII only; strings are refused |

A heap buffer needs lldb-dap 21. Rows say `[settable]` where a change can be made: with lldb-dap
20, a string whose text is in its own buffer.

Peeky never writes a text:

- **through a pointer or a reference.** A `char *` may point at a string literal, which is
  read-only, or at a buffer of unknown size. A `const std::string &` may point at a string that
  another frame owns.
- **into a `const` or `volatile` local:** `const std::string cs`, `const char tag[8]`, or a typedef
  of one. The program may never change a const object. Peeky judges this from the build's debug
  information, which also tells a typedef's array or string: `using Name = std::string` is changed
  as a `std::string`.
- **into a field or element,** such as `rec.name`. To change one, copy it into a local with a
  candidate edit.

All the `--set` rules apply too: a statement under way, a local not yet initialized, an optimized
build. Every byte written, and the new size, are read back. Anything that doesn't read back as
asked closes the session.

### Read a C++ map or set by key

The debugger lists a `std::map`'s entries as `[0]`, `[1]`, ... in iteration order. But in C++,
`m[1]` is the value for key 1. Peeky reads `m[K]` as C++ does, and `m[#N]` is the Nth entry in
iteration order: a pair with `.first` and `.second`. So a position is never taken for a key.

In `examples/cpp/maps/scores.cpp`, `m` is `std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}}`:

```sh
peeky probe examples/cpp/maps/scores.cpp --at 9 --capture 'm[1],m[#1].first,m[#1].second,ages["al"],s[#0],s[1],m[3]'
```

```
hit  phase     line  function  selector      type       value
1    observed  9     main      m[1]          int        10
1    observed  9     main      m[#1].first   const int  2
1    observed  9     main      m[#1].second  int        20
1    observed  9     main      ages["al"]    int        42
1    observed  9     main      s[#0]         int        1
1    observed  9     main      s[1]                     <[1] not read: s (std::set<int>) is a C++ set, which has no [] in C++, and the debugger lists its elements in iteration order, not by index; select an element by position with s[#N] (`peeky session get s` lists them)>
1    observed  9     main      m[3]                     <[3] not found: m has no key 3 (keys: 1, 2, 5); in C++, m[3] would insert it with a default value>
```

#### Which keys `m[K]` reads

- `m[K]` reads `std::map` and `std::unordered_map`, with libc++ and libstdc++.
- Peeky knows a map or set by its type as LLDB shows it: `std::map<…>`, `std::__1::map<…>`,
  `std::__debug::map<…>` or `std::pmr::map<…>`, also through `const` and `&`.
- Integer keys, `short` to `long long`, signed or unsigned: `m[1]`, `m[-1]`. A key outside the key
  type's range is refused.
- `std::string` keys: `ages["al"]` or `ages['al']`, in printable ASCII without `"` or `\`. A key
  is matched exactly as Peeky shows it, and by its length. For keys with a NUL byte, see
  [Read a C++ string that holds a NUL byte](#read-a-c-string-that-holds-a-nul-byte). A key whose
  length can't be read is refused.
- Keys of any other type are refused for `m[K]`: `double`, `char`, `bool`, enums, structs, and
  `const char *`, which C++ compares by address. Read those entries with `m[#N].first` and
  `m[#N].second`.
- A key of the other kind is refused, such as a quoted key on a map with integer keys.
- `m[#N]` works only on maps and sets. Elsewhere, `[N]` indexes.
- `m[K]` also works through a typedef (`using Index = std::map<int, int>`, or a class's member
  typedef such as `Holder<T>::S`; the debug information names its class). It works through a
  reference, a pointer (`mp[0][1]`, as C++'s `(*mp)[1]`), a struct (`h.m[1]`), and a vector of
  maps (`vm[0][4]`).
- A missing key is `not found`, with the keys there are. Nothing is inserted.

#### Maps that compare keys their own way

A map with a comparator of its own (`std::map<int, int, ByAbs>`), or a hash or equality of its own,
reads an exact key: `am[-1]`. But it never says that a key is missing. In C++, `am[1]` reads the
entry whose key `ByAbs` treats as equal, `-1`. So Peeky's `am[1]` says that no key equals 1
exactly, and names `am[#N]`. `not found ... would insert it` comes only with `std::less`,
`std::greater`, or `std::hash` with `std::equal_to`.

#### Sets, multisets and multimaps

- They have no `[]` in C++, so `s[1]` and `mm[1]` are refused. `s[#1]` and `mm[#1].second` read by
  position.
- `t[1]` is refused too when two things hold. `t`'s type is a typedef whose class the debug
  information doesn't give. And the debugger lists pairs with a const key, as it does a map's
  entries. `t[1]` could then be a key or a position. `t[#1]` reads by position.
- So is a type that the debug information gives several classes, a map or set among them. An
  example is `std::map<int, std::set<int>>::mapped_type`, where other maps' `mapped_type` is
  `int`. Its type then shows as `?`.
- Vectors, deques and arrays index by position as usual.

#### Listing a map, and big maps

- `session get m` lists `m[1]: int = 10`, `m[2]: int = 20`, ... when the keys compare. Each
  selector reads the value its row shows. Otherwise it lists `m[#0]`, `m[#1]`, ..., with a note
  that says which.
- An entry whose key no selector can write is listed by position. Such a key has more than six
  digits, or is a string with other characters or a NUL. No two entries share a label.
- A `std::map` with integer keys in the standard order (`std::less`, the default, or
  `std::greater`) and more than 20 entries is bisected. It reads about log2(size) entries. So any
  size up to 512 entries reads in a few steps. Its size is the summary's `size=N`, checked against
  the listing.
- Any other map is compared entry by entry. That costs one node per entry and one per key, within
  the inspection's 512 nodes, so one inspection holds a map of about 200 entries. A bigger one is
  refused before any entry is read: use `m[#N]`.
- A listing shorter than the summary's size never says that a key is missing.
- A typedef that the debug information doesn't resolve at all, with no class for its name, is told
  from a vector only by its entries. So a set behind one reads `s[N]` by position, as a vector
  would.
- `std::flat_map` and `std::flat_set` (C++23) are known by name, but not checked with a real build.

In a session, select a map's entry by key the same way, quoted for the shell. Pass the session's
`--name` to every command:

```sh
peeky session start examples/cpp/maps/scores.cpp --at 9 --name maps
peeky session get 'ages["al"]' ages --name maps
peeky session close --name maps
```

```
ages["al"]: int = 42
ages: std::map<std::string, int> = size=2
  ages["al"]: int = 42
  ages["bo"]: int = 31
  note: ages is a C++ map: ages[K] is the value for key K, as in C++ (integer and std::string keys), and ages[#N] its Nth entry in iteration order, a pair with .first and .second
```

### Change a field or an element

`--set` and `session set` change an integer field or array element, as they change an integer
local. The local must be one that the stopped frame holds itself:

```sh
peeky session set s.end=10        # set s.end: 2 -> 10
peeky session set 's.arr[2]=7'    # an element of an array in a struct
peeky session set ps.end=10       # MUTATION_REJECTED: `ps` is a pointer: Peeky changes only values the frame holds itself ...
```

What Peeky checks:

- **The path** is checked against the build's debug information, not the debugger's type names. A
  typedef can hide a pointer, and the debugger shows a bitfield as its declared type. Only fields
  of structs and classes, and elements of fixed-size arrays, count. Never through a pointer
  (`this` included), a reference, a base class or an anonymous member. Never inside a union, a
  packed struct (whose fields may be misaligned) or a `std::` type.
- **The target** is an integer of 16 to 64 bits, not const, volatile or a bitfield. `char`, `bool`,
  enums and floats are refused. The value must fit the target.
- **The root** is the first local in a selector: `s` in `s.arr[2]`. It must be an automatic
  variable or a parameter of the stopped frame, in an unoptimized build. Peeky must not withhold
  it: not declared below the stop, not in an initializer still running, and no jump past it. Nor
  may it show with a `note` that its declaration still runs after the stop, as on a `do` body's
  first line.
- **Arrays:** an element of an array of any length is fetched alone, at any step of the path:
  `big[700].a=1`, `grid[599][2]=3`.
- **Read back:** the field is read back from the debugger after the write. Only the value asked for
  counts as a change.
- **Marks:** probe rows and `session get` listings mark `[settable]` what `set` would change at
  the stop, by the same rules: `bins[3].hi  [settable]`, never `ps.end`. A refusal names the rule
  it broke, and its hint names the one way around it.

### Change a field behind a pointer

Peeky never writes through a pointer or a reference, because what it reaches may be freed or
moved. Change the value in the frame that owns it instead. The refusal says where, so read it
first. In C, where Peeky can follow the calls, it names the owner's line and the `--set-at` to use:

```
`db.count` cannot be changed at debounce.c:35, at any stop of this build: in settle, `db` is a pointer: …; settle has one caller: debounce.c:57 in feed, which passes it on (`db`, its own pointer parameter); feed has one caller: debounce.c:80 in main, which passes `&db`, where `db.count` is settable: a probe's `--set-at 80@N db.count=<value>`, N counting line 80's own stops (find N with `--at 80 --capture db.count --hits all`). Refused before the program runs: nothing ran
```

Do what it says. `--set-at 80@5 db.count=0` sets `main`'s `db.count` at line 80's fifth stop,
before that call. Repeat it for more stops. Other refusals say:

- The owner's call alone, with no `settable`: a change there may not reach the call
  ([below](#when-the-refusal-says-settable-c)).
- `Peeky cannot single out one call of wrap`: `describe`'s `mutable:` line lists the fields each
  function can set.
- `` `p` is a local here, not a parameter, so no call passed it ``, for an automatic local, one
  that shadows a parameter too: if what it points at is also a root local in some frame, change
  it there. Otherwise edit the source.
- `` `kept` isn't proven a parameter here, so Peeky names no call ``: the same advice. This is
  for a static, or where Peeky can't single out the declaration that applies at the stop. It is
  also for a parameter that the function may point elsewhere, or that shares its name with
  another local of the function. Peeky reads the function's body as the compiler preprocesses
  it. `p = …`, `p += …`, `++` or `--` on `p`, `&p`, inline assembly or an address builtin
  anywhere in it count. In C++, so does any mention of `p` but `p->…`, `p[…]` or `*p`, since a
  reference can change it (`aim(p)` with `void aim(Acc *&)`).
- `change the value where it is owned, before the call that passed it`, for a pointer proven to
  be the function's parameter at the stop, still holding what the call passed. C++ says this
  for every such parameter, because overloads and member calls make a call's target unclear
  from the text. Use
  `--set-at <call line>@N`, a probe `--at` the call's line with `--hits N --set-hit N`, or
  `session start --at` that line with `--hit N`.

N counts the call line's own stops, not the called function's. To find N, run
`peeky probe ... --at <call line> --capture <argument> --hits all`. `session continue --to` the
call's line reaches only the next call.

`--set-at 36@all db.count=0`, a change at every stop of `settle`'s line 36, is refused the same way.
Its refusal also says what `--set-at 80@all` would do: change the count before every call at line
80, whether or not that call reaches line 36. That is another experiment. To make a change only
where a callee's line runs, use a candidate edit there ([COOKBOOK 2.8](../COOKBOOK.md#change-it-at-every-stop-all)). Where the owner
has such a line itself, `@all` on it works.

#### What counts as behind a pointer

Every local whose storage the debug information reaches through a pointer:

- a structured binding of a reference: `dropped` in `auto &[at, dropped] = *owner;`. It has no
  storage of its own, and is never marked `[settable]`. Its fields show
  `[not settable: reference]`;
- a named return value, whose object is the caller's;
- in C on macOS, a `__block` variable;
- the elements of GCC's variable-length arrays.

Their refusal says
`` `dropped` is reached through a pointer or reference …. Change the value where it is owned: Peeky changes only values the frame holds itself ``.

These change as usual, because the frame holds them itself: a by-value structured binding
(`auto [a, b] = pt;`), and a by-value parameter that the ABI passes through a hidden pointer. That
is a struct over 16 bytes on arm64, or any class with a destructor: the call's own copy.

#### When the refusal says `settable` (C)

In C, Peeky follows the calls in the program's sources, from the stopped function up to the frame
that owns the struct:

- Each function on the way, the stopped one too, must have one caller. Peeky counts every mention
  of its name in the preprocessed sources, so a macro used twice is two calls. A mention that is
  not a call by name, a prototype or the definition, such as taking its address, leaves the
  callers unknown.
- A caller that passes its own pointer parameter on as is, to the same type, is followed:
  `fits(lay, …)` in `wrap(const struct layout *lay, …)`. It must never point it elsewhere (no
  `lay = …`, `lay++` or `&lay`), and must have no inline assembly (`asm`, `__asm__`) and no
  address builtin (`__builtin_addressof`).

Where that stops before the owner, the refusal names no frame. So does a last caller that passes
a pointer it keeps or computes: `p`, `&a[i]`, `s->inner`.

A struct that the caller doesn't hold in its frame is never changed there: `&g` for a global, or
a static local. To change it, change what sets it with a candidate edit.

The refusal says `settable`, with the `--set-at`, only where a stop on the first line of the
owner's call statement can make the change, and the call sees it. Here the root is `r`, the first
local in the selector `r.tail`. Otherwise it names the owner's call alone. All of these must hold:

- Each function on the way uses the pointer only for that call, and to read integer fields
  through it: `lay->indent`.
- The call is in the first statement on its line. In `r.tail = 1; total += push(&r, 1);`, the
  first statement overwrites the change.
- The statement is one of these forms, read as the compiler preprocesses it, with a macro's code
  where it is used. NAME is not the root: `push(ARGS);`; `NAME = push(ARGS);`, or `+=` and the
  like; a declaration's `int NAME = push(ARGS);`; `return push(ARGS);`; `if (push(ARGS) ...)`.
- ARGS, and the rest of an `if`'s condition, only read:
  - no assignment, `++`, `--` or `&`, except the address of another plain name: `&out`;
  - no call of any kind: `atoi(s)`, `ops[0](r.buf)`, `(*fp)(x)`, `v.fn(x)`, `sizeof(x)`;
  - the root only as the `&r` passed, and in reads of its integer fields: `r.head`, but not
    `r.buf`, which is a pointer into it.
- The caller takes no address of the root or its fields anywhere else in its body, as
  preprocessed, a header included inside it too. A pointer `a = &r` may change it. So:
  - no builtin, except those known to reach no memory, such as `__builtin_expect`.
    `__builtin_addressof`, `__builtin_frame_address` and `__builtin_dwarf_cfa` count;
  - no array in the root named without an index: `keep(r.buf)`. `r.buf[0]` and `sizeof r.buf` are
    fine;
  - passing `&r` whole is fine, to a function of the same file that only uses its fields through
    it: `pop(&r, &v)` reading `r->count` and `r->buf[i]`.
- The owner's function has no inline assembly, which may change any value it reaches.
- The root is declared above the call, and the usual `set` rules allow the change at that stop.
  For example, `int total = 1 +` / `push(&r, 1);` is named by its statement's first line, 10,
  whose stop comes after the call.

Anything else may change the value first: `r = q, push(&r, 1)`, `push(&r, RESET(r))`, or
`push(&r, ops[0](r.buf))` through the array at `r`'s start. So a call such as
`push(&r, atoi(argv[i] + 1))` is named alone, without `settable`.

Calls in other files:

- A call in another source file is changed by a probe `--at` that file's line:
  `--at other.c:6 --set q.tail=<value> --set-hit N`. This works as `--set-at` names a line of the
  probe's own file. A call in a header can't be stopped at.
- Each source file's calls are its own. A header's function is compiled once in each file that
  includes it, and its macros may make those copies differ. So a call in a header that two files
  include is two calls. A function, or a caller, defined in more than one file gets the general
  advice.

### Same-named locals: which one a name means

- When another local of the same name is in scope, LLDB names a local with where it is declared.
  Select it as named: `'p @ s1.c:38.x=3'`, `'n @ s1.c:15=9'`. Two of one name declared on the same
  line are ambiguous, and `get` and `set` refuse them.
- **A plain name** means the declaration the code names at the stop, as C and C++ scope it. Say
  LLDB lists an inner block's `t` and an outer one as `t @ same.c:22` and `t @ same.c:20`. Then `t`
  reads and changes the inner one.
- `t` means the outer one before the inner one's declaration. It also means the outer one after a
  `while`'s or a `switch`'s own `t`. The same holds after a `t` that is a loop's or `switch`'s
  whole body without braces: `while (c) long t = 8;`. That `t`'s scope ends with its statement,
  though the debug information keeps it to the end of the block.
- Scopes are read from the source as the compiler preprocessed it, so a macro's `while (decl)`
  counts too.
- Where Peeky can't tell which one the code names, `t` is refused. That happens when the source
  doesn't say where a scope ends, or when two are equally inner. The error lists the
  `t @ file:line` names, which select.

#### One name with different types

A function can declare one name with different types: a loop's `Task t` and a later range-`for`'s
`const Task &t`, or `s` in sibling blocks.

- The path is checked against the type of the declaration the debugger shows at the stop: the one
  whose block holds the stop.
- `describe`'s `mutable:` line names such locals.
- Two declared on one line can't be told apart, and a change to them is refused.
- A probe that changes one is refused before the program runs, unless the line's breakpoint
  location singles one out. That location is the line's lowest address that runs before the
  function returns, not an exception's landing pad.
- LLDB's later stops on the line, one per other block, are part-way through it, where no change is
  made. The refusal says where one of them names another declaration.

### Change at a statement's first instruction

A change makes the local's or field's storage hold the new value, and the program's later reads of
it see it. What the program copied before keeps the old value:

- a copy on an earlier line: `int a = s.x;`;
- a statement under way at the stop, which may hold what it read in a register.

So C and C++ allow a change only at a statement's first instruction, and refuse it anywhere else.
There, `set` says `cannot be changed here` and keeps the session. `inspect` drops `[settable]` and
says why: `note: nothing here is [settable]: the stop is not at the first instruction of the
statement on line 9 ...`. In JSON, this is `unsettable`.

A breakpoint on a statement's first line (`--at`, `continue --to`) qualifies, and so does a `step`
that lands there. Peeky proves that the stop is at a statement's first instruction from the source
and the line table. All of these must hold:

- **The line starts a statement,** and it is one of its function's own lines:
  - every bracket around it, up to the function's body, is a block of statements. Not a call's
    arguments, a `for` header, an initializer, a `new long[1]{`, a compound literal, or a class
    body. Nothing inside a GNU statement expression `({ … })` counts at all, even a macro's loop
    there;
  - no directive or bracket-hiding macro lies between the line and the previous token;
  - that token ends a statement: `;`, `{`, `}`, a label, `else`, or the `)` of an `if`, `for`,
    `while` or `switch` header.
- A brace after something like parameters (`REPEAT(k, 1) {`) is a function's body only at file
  scope, or in a class's, a namespace's or an `extern "C"` body. Elsewhere, it is a block of the
  function around it.
- **The stopped instruction's address is the lowest** of all the function's code on the lines of
  the statements that start there. Clang may put a later line's code first: `t =` /
  `p.x + g();` loads `p.x` and calls `g` before line 1's store.
- **On a function's first line** (a one-line function or lambda), the statements are those after
  its body's `{`. The stopped instruction's address must be the lowest of their rows past the
  function's entry. For example,
  `long one(struct P p) { long t =` / `p.x + g();` runs the second line first, so neither line
  qualifies.
- **The stop is past the function's prologue,** which stores its parameters. A GCC build's
  one-line function can stop before it. There, `set` says `the stop is at its function's start,
  before the prologue stores the parameters`
  ([above](#at-a-functions-start-before-it-stores-its-parameters)).
- **No call in those statements was inlined** by the compiler (`always_inline`, `flatten`). Peeky
  reads which from the debug information. An inlined body's code carries its own function's lines
  and runs before the statement's lowest row: `t = inl(&p) + p.y;`.

A caller frame (`--frame 1`) is judged where its call is: at the address before the one it returns
to, as LLDB reads it. A frame that a signal interrupted is judged at the instruction it stopped at.
Such a frame is above the system's `_sigtramp` or `__restore_rt`, not a function of the program's
that happens to have that name.

#### Where a change is refused

- **Back from a call:** after `step --out`, or at a return address.
- **On a statement's later line:** `+ f();` under `int t = s.a`.
- **On a loop header's later passes.**
- **In a K&R-style definition:** `long f(a) long a; {` isn't supported.
- **Wherever Peeky can't tell.**
- **On `} else if (p.y > 1) {` after a `step` on the else path.** That line's lowest address is
  the jump that ends the `if` block. So the step stops past it, at the condition. A breakpoint on
  that line, placed at the jump, is never hit on the else path, with or without Peeky. Change the
  value before the `if`, or at the first statement inside the `else if` block.

Two more stops are refused, though nothing is under way there. Change the value in the caller, or
at the next statement:

- in an inlined function's own code: a one-line `always_inline` function's line, where its frame
  stops;
- on a body's `{` placed on the line under its signature, with a statement after it:
  `long f(struct P p)` / `{ long t = p.x + 1;`.

Also:

- A function-try-block (`long f(P p) try { … } catch (…) { … }`) is a function body like any
  other, and so are its handlers.
- The stop's own line may still overwrite the change: `s = (struct tri){9, 9, 9};`.
