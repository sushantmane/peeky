# Peeky Cookbook: C and C++

**[AGENT_GUIDE.md](../AGENT_GUIDE.md) and [c-cpp.md](c-cpp.md) cover most tasks.** This cookbook is
for details, which they cite by section number.

This cookbook covers what is different for C and C++. The [COOKBOOK](../COOKBOOK.md) has the recipes
every language shares. Each section here has the number and title of the COOKBOOK section it adds
to. For a one-page summary of the C and C++ rules, see [c-cpp.md](c-cpp.md).

Start with [7.1](#71-c-and-c): what Peeky reads and changes in C and C++.

## Contents

- [1.1 What Can I Ask?](#11-what-can-i-ask)
  - [See each name's type](#see-each-names-type)
  - [How a C++ function's name is shown](#how-a-c-functions-name-is-shown)
- [2.1 Fields and Indexes](#21-fields-and-indexes)
  - [Read through a pointer](#read-through-a-pointer)
  - [When what a pointer points at is unproven](#when-what-a-pointer-points-at-is-unproven)
  - [Which element's address a pointer holds](#which-elements-address-a-pointer-holds)
  - [A pointer one past an array's end](#a-pointer-one-past-an-arrays-end)
  - [How far a pointer is from a pointer parameter](#how-far-a-pointer-is-from-a-pointer-parameter)
  - [A value read at another pointer's address](#a-value-read-at-another-pointers-address)
  - [Read a C++ map or set](#read-a-c-map-or-set)
- [2.2 Every Iteration of a Loop](#22-every-iteration-of-a-loop)
  - [Loop headers in a GCC build](#loop-headers-in-a-gcc-build)
- [4.7 C++ References and Pointers in SQL](#47-c-references-and-pointers-in-sql)
  - [Compare a reference's text in SQL](#compare-a-references-text-in-sql)
- [4.8 Compare Addresses Across Runs](#48-compare-addresses-across-runs)
  - [Which element in each run](#which-element-in-each-run)
  - [Offsets from a pointer in each run](#offsets-from-a-pointer-in-each-run)
- [5.1 Stop, Look, Change, Finish](#51-stop-look-change-finish)
  - [Read a caller's local](#read-a-callers-local)
  - [When the program crashes](#when-the-program-crashes)
- [7.1 C and C++](#71-c-and-c)
  - [Quick start: what you can read and change](#quick-start-what-you-can-read-and-change)
  - [Read structs, arrays and standard containers](#read-structs-arrays-and-standard-containers)
  - [Build a program from several files](#build-a-program-from-several-files)
  - [Which C++ builds Peeky supports](#which-c-builds-peeky-supports)
  - [Name the tools in a config](#name-the-tools-in-a-config)
  - [What Peeky checks about a build](#what-peeky-checks-about-a-build)
  - [Why a local shows `no value yet`](#why-a-local-shows-no-value-yet)
  - [Read a `static` local](#read-a-static-local)
  - [Read C++ members](#read-c-members)
  - [Read a lambda's captures](#read-a-lambdas-captures)
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
- A pointer one past an array's end is refused, since nothing there belongs to the array
  ([below](#a-pointer-one-past-an-arrays-end)).
- What any other pointer points at is read. Where Peeky can't show it is a live object, it is
  marked `[unproven]`, with why ([below](#when-what-a-pointer-points-at-is-unproven)).

### When what a pointer points at is unproven

A C or C++ pointer can outlive what it points at, and nothing in the program marks it. So Peeky
checks where each pointer you read through points. It still reads what is there, since you may
need it. But where it can't show that a live object is there, the value is marked `[unproven]`,
with a note that says why.

This `main` keeps items in an array, gets one from a call that returns its own local, and
allocates another:

```c
static struct item *newest(void) {
  struct item made = {7, 3};
  struct item *p = &made;
  return p;
}

int main(void) {
  struct item stock[2] = {{1, 5}, {2, 8}};
  struct item *first = &stock[0];
  struct item *last = newest();
  struct item *kept = malloc(sizeof *kept);
  kept->id = 9;
  kept->qty = 4;
  printf("%ld %ld %ld\n", first->qty, last->qty, kept->qty); /* line 19 */
```

```sh
peeky probe pick.c --at pick.c:19 --capture first.qty,last.qty,kept.qty,kept
```

```
hit  phase     line  function  selector   type    value
1    observed  19    main      first.qty  long    5
1    observed  19    main      last.qty   long    4334437632  [unproven]
1    observed  19    main      kept.qty   long    4  [unproven]
1    observed  19    main      kept       item *  0x00000001025d5360
status: completed  hits: 1  seconds: 1.006  max pause: 56.1 ms
warning: last.qty: Peeky can't show that `last` points at a live object, so this value may be left over: it points below the stack pointer, where no live call keeps its locals, as a pointer into a call that has returned does; read the value before that call returns.
warning: kept.qty: Peeky can't show that `kept` points at a live object, so this value may be left over: its address is in no local, member or element that Peeky could place in the frames it lists, nor in the program's static memory; if it is a heap block, Peeky can't tell whether that block is still allocated, so check where the program frees it.
```

`last.qty` reads what `newest`'s returned call left. `kept.qty` reads a live block, but Peeky
can't ask the allocator, so a freed block would read the same way.

What a pointer points at is proven live where its address is in one of these:

- A local or parameter of a frame Peeky lists, at most ten, of the pointer's type.
- An element of an array such a frame holds, or a member or base class of a struct it holds.
  These are the arrays and objects listed [below](#a-pointer-one-past-an-arrays-end), and
  number, `char` array and pointer locals too. A `Shape *` at a `Circle` local's `Shape` counts.
- A global or `static` local of the pointer's type, inside its own extent, as the build's debug
  information places it. Past its end, or elsewhere in the program's image, such as its code,
  isn't proven.

A `char *` may point at any byte of such an object, as C allows. It may also point into a string
literal, or one of `main`'s argument strings, from the pointer to the string's NUL. Environment
strings count where `main` declares `envp`. A literal counts only in a section that holds nothing
but strings, as macOS's `__cstring` does. Linux linkers merge literals into `.rodata`, among other
constants, so there a literal's text is marked.

A union's member is never proven, only the union itself: no declaration says which member holds a
value. An element of an array of unions is a union too. Nor does a declaration in scope say which object a storage holds after placement `new`.
Where the types differ, what is read is marked.

Otherwise, the note says what Peeky knows of the address:

- ``it holds the address of `cells[4]`, past the end of `cells`, which has 3 elements``, or an
  address some elements before an array's start.
- `it points below the stack pointer`: no live call keeps its locals there.
- `it points into the stack of the frames Peeky lists`, but at no local, member or element of its
  type.
- ``it points into the union `un` ``, and no declaration says which of its members holds a value.
- `it points into the program's loaded image, but no compatible global, static local or proven
  string literal is there`, as a pointer into the program's code does. It says to check where the
  pointer was assigned. A string literal is proven where it is stored in a named `char` array,
  such as `static const char name[] = "Maya";`, and the pointer points at that.
- `its address is in no local, member or element that Peeky could place`, such as a heap
  block's. A live block and a freed one read alike.

The mark is on what is read through the pointer: `last.qty`, `last[0]`, a struct read whole, and
`session get last`'s children. `session get` gives the note once, below them. The pointer's own
row is its address, with no mark. A `char *`'s text is read through it, so that row is marked
too: `dup  char *  0x… "heap"  [unproven]`. So is a C string a function returns, in
`--capture return` and `session step --out`.

`--json` rows and the store keep `view: unproven` and the note. `queries diff` ranks such a value
with the qualified ones, after the real differences
([COOKBOOK 3.1](../COOKBOOK.md#31-two-inputs-side-by-side)). Two runs' leftovers can differ for
any reason.

Peeky checks addresses, not where a pointer came from:

- A pointer into a call that returned reads as live where a later call's local of its type is at
  that address now.
- A pointer to a member Peeky doesn't list is marked. It lists a struct's members three levels
  deep, and the members of an array's first 16 elements.
- In a method of a C++ object made with `new`, what `this` points at is marked: it is a heap
  block.
- A pointer into a library's own memory, such as `stdout`, is marked.
- A C++ reference, and a smart pointer's object, aren't checked. A GCC lambda's capture by
  reference is ([details](#read-a-lambdas-captures)).
- A pointer with a `note` already, such as a local on a `do` body's first line, keeps that note
  on what is read through it. It may hold leftovers itself
  ([7.1](#values-peeky-withholds-or-cant-vouch-for)).

lldb-dap 18 gives no address for a number, `char` array or pointer local, or a `std::string`.
Peeky then asks for `&name`, as a hover that reads the variable and runs nothing. It asks only for
locals that may hold what the pointer points at. It reads `argv[N]` the same way.

Peeky bounds what it reads per stop: 32 locals asked for, 256 locals of a frame, 64 that need
asking, and 64 argument strings. Where a bound left a local unplaced, the note says only that:
`Peeky didn't place every local at this stop: the per-stop limit was reached`. It never says the
pointer points at no local then, or below the stack pointer. Where only the argument strings were
cut short, it says so: ``Peeky didn't read every one of `main`'s argument and environment
strings``.

### Which element's address a pointer holds

Capture the array with the pointer, at the same stop. Where Peeky can prove which element's
address the pointer holds, its row says so. This `main` keeps its readings in `int level[6]`.
`top` points at the highest, and `end` one past the last:

```c
const int *end = level + 6;
const int *top = level + peak(level, n);
printf("TOP:%d AT:%d\n", *top, (int)(top - level)); /* line 21 */
```

```sh
peeky probe levels.c --at levels.c:21 --capture top,end,level -- 1 5 5 2
```

```
hit  phase     line  function  selector  type         value
1    observed  21    main      top       const int *  0x000000016dc6f2c8  [at level[2]'s address]
1    observed  21    main      end       const int *  0x000000016dc6f2d8  [at the address one past level's end]
1    observed  21    main      level     int[6]       [1, 5, 5, 2, 0, 0]
```

Peeky says it only where all of this holds:

- Both values are read at one stop, in one frame: one probe hit, one `session get`, or one
  `session inspect`, which says it of the locals it lists.
- The array's own type gives its length, such as `int level[96]`. A struct or another single
  object counts as an array of one.
- The pointer points at the array's element type. A typedef or `const` makes no difference. A
  `char *` into an `int` array says nothing.
- Its address is a whole number of elements from the array's first.
- It is at most one past the end, as `end` is. C lets a pointer hold that address, but reading it
  is undefined, so Peeky never reads it.
- Only one captured array or object matches. Where several do, the row says none.

It is a fact about the address, not about which object the pointer designates. Another object can
have the same address:

- A union's two arrays: `u.a[2]` and `u.b[2]` are one address. With both captured, neither is
  named. With only `u.b`, a pointer set to `&u.a[2]` says `[at u.b[2]'s address]`.
- The array that follows: the address one past `a`'s end can be `b[0]`'s.
- Storage the program reuses, or an object you didn't capture.

Otherwise the row shows the address alone. A C++ reference never says, nor does a pointer read
alone, unless it is one past an array's end ([below](#a-pointer-one-past-an-arrays-end)). `--json`
rows have `at_element_of`, such as `{"selector": "level", "index": 2, "length": 6}`, and
`element_size`, the bytes of one element. An object has no `length`.

An array passed to a function is a pointer there, with no length. So no element is named in it.
Rows there say less ([below](#how-far-a-pointer-is-from-a-pointer-parameter)).

### A pointer one past an array's end

C lets a pointer hold the address one past an array's end, as `end` does in
`long *end = queue + 4;`. But nothing there belongs to the array, so Peeky never reads through
such a pointer. Its row shows its address, and says where it is, even with the array not
captured:

```
$ peeky session get end
end: long * = 0x000000016b8c3280  [at the address one past queue's end]
  <children: none read: `end` holds the address one past the end of `queue`, and nothing there belongs to `queue`; read `queue` by name>
```

A selector that reads through it, such as `end[0]`, or `pend.x` for a pointer to a struct, is
refused with the same reason. A `char *` one past a `char` array's end shows its address alone,
never the text LLDB read there. So does such a `char *` that a function returned, in
`session step --out` and `--capture return`.

Peeky looks for the array in the locals of each frame it lists, at most ten:

- An array local, or an array a local holds: a struct's member (`r.buf`), or one in the first 16
  elements of an array of structs (`rs[1].buf`).
- In C++, an array member of the object `this` points at (`this.buf_`). A `std::array` counts as
  an array.
- A 2-D array counts as one array of its innermost elements, row after row: `char names[4][16]`
  holds 64 `char`s.
- The pointer points at the array's element type, of its size, as for an element
  ([above](#which-elements-address-a-pointer-holds)).
- In another source file, a struct, class or union is that type only with the same layout: its
  name, size, and each member's name, offset and type. Two files can give one name two layouts.

Peeky reads a caller's locals once per stop, however often you inspect it.

In a function the array was passed to, the row names the caller's frame:
`[at the address one past queue's end in frame 1]`. Read the array there with
`session get queue --frame 1`.

Another object of the pointer's type can start at that address, such as the next array. Peeky
names it, `[at the address one past t.a's end, also t.b[0]'s]`, but reads nothing:
`` `t.b[0]` starts there too, but nothing shows `tend` points at it``. Read it by name if the
pointer is meant to point at it. So a pointer to the start of an array that sits right after
another of its type is refused too.

`--json` rows, `session get`'s children and `session inspect`'s locals give it as `past_end`,
such as `{"selector": "queue", "length": 4}`. Where another frame holds the array, it also has
that frame's index and function: `"frame": 1, "function": "main"`. The store keeps the pointer's
address, and the reason in place of what it points at.

Peeky can't tell in these cases, so it reads what the pointer points at, as for any pointer. It
marks it `[unproven]` unless the address is in an object it places
([above](#when-what-a-pointer-points-at-is-unproven)):

- One past a single object, such as `&x + 1`. Locals of one type often sit side by side, so a
  pointer to the second would be refused too.
- An array Peeky doesn't list: a global or `static` one, a heap block, or one held deeper than
  above.
- An array whose address lldb-dap doesn't give. lldb-dap 18 gives none for a `char` array or a
  `std::array`, and lldb-dap 20 and later give every value's.
- An address two or more elements past the end, or before the start.
- An array of an enum, or of a struct whose layout Peeky didn't read whole, in another source
  file than the pointer's function. A pointer to an enum local in another file is marked
  `[unproven]` for the same reason.

### How far a pointer is from a pointer parameter

In `peak(const int *level, int n)`, `level` is a pointer, and `p` and `top` walk the array it points
at. Peeky can't tell how long that array is, or that `p` is in it. But how far apart two addresses
are is a fact. Capture the pointers together:

```sh
peeky probe levels.c --at levels.c:8 --capture top,p,level --hits all -- 1 5 5 2
```

```
hit  phase     line  function  selector  type         value
3    observed  8     peak      top       const int *  0x000000016b42b2c4  [level + 1 by address; same array not proven]
3    observed  8     peak      p         const int *  0x000000016b42b2c8  [level + 2 by address; same array not proven]
3    observed  8     peak      level     const int *  0x000000016b42b2c0
```

`[level + 2 by address]` says that `p`'s address is `level`'s plus `2 * sizeof(*level)` bytes.
Nothing proves that both point into one array, so it is no C expression Peeky vouches for. It
names no element, and Peeky reads nothing there. A row says it only where all of this holds:

- Both pointers are read at one stop, in one frame.
- Both point at the same type, as for an element above: `const int *` and `int *` count.
- Their addresses are a whole number of elements apart, at most 2^20 of them. Pointers further
  apart, such as one into the stack and one into the heap, say nothing.
- A pointer that is before the base says so: `[level - 2 by address; same array not proven]`.
- A pointer at the base's own address says ``[same address as `level`]``. An equal address claims
  nothing about any array, so nothing is doubted. With both one past an array's end, the row adds
  `at the address one past queue's end`.

Every pointer of one type is counted from one base. It is the first of them that is a parameter
of the function, such as `level`, else the first you capture. The base itself says nothing. A
pointer at an element's address of a captured array says that instead
([above](#which-elements-address-a-pointer-holds)).

`--json` rows have `offset_from`, such as `{"selector": "level", "offset": 2}`. The store keeps
it in `offset_from` and `element_offset`, apart from `at_element_of`, so SQL never takes one for
the other ([4.8](#offsets-from-a-pointer-in-each-run)).

### A value read at another pointer's address

A value read straight through a pointer names the other captured pointers of its type that hold
the same address. At `peak`'s first pass, `p`, `top` and `level` are equal:

```sh
peeky probe levels.c --at levels.c:8 --capture 'top,p,level,p[0]' --hits 1 -- 1 5 5 2
```

```
hit  phase     line  function  selector  type         value
1    observed  8     peak      top       const int *  0x000000016b30f220  [same address as `level`]
1    observed  8     peak      p         const int *  0x000000016b30f220  [same address as `level`]
1    observed  8     peak      level     const int *  0x000000016b30f220
1    observed  8     peak      p[0]      const int    1  [read through `p`, which holds the same address as `top` and `level`]
```

It is an address fact, as above. Peeky can't know how many elements the program still counts, so
it never calls a value left over. Where a program keeps a pointer one past its last element in
use, a value read at that pointer's address is not one of them. Capture both pointers to see it.

- It is said of `p[0]`, `node.qty` and `node[0].qty`, read at the pointer's own address.
- It names the nearest captured pointer the value is read through. `node.next.qty` says it of
  `node.next` where you capture `node.next` too, never of `node`.
- It isn't said through a pointer you didn't capture: with `Item **pp`, `pp[0].qty` says it only
  where `pp[0]` is captured. Nor is it said at another element, as in `p[1]`.
- Both pointers are read at one stop, in one frame: one probe hit, or one `session get`. They
  point at the same type, and neither is null.
- `session get p top` says it of the members it lists under each pointer too. Only the pointers
  you name count.

`--json` rows, and `session get`'s children, have `read_through`, such as `{"selector": "p", "same_address_as": ["top",
"level"]}`, with at most three names. The store doesn't keep it.

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

## 4.8 Compare Addresses Across Runs

Read [COOKBOOK 4.8](../COOKBOOK.md#48-compare-addresses-across-runs) first.

### Which element in each run

Where both runs captured the array, `queries diff` compares which element's address each pointer
holds ([2.1](#which-elements-address-a-pointer-holds)). `peak` takes the last of equal readings.
A candidate edit makes it take the first, `--old '*p >= *top' --new '*p > *top'`:

```
run_a  run_b  hit  phase     selector  value_a             value_b             differs  note
1      2      1    observed  top       0x000000016aee72d8  0x000000016f29b2c4  1        the address of `level[2]` in run a, of `level[1]` in run b; by address alone: the pointer or the array may have moved
1      2      1    observed  end       0x000000016aee72e8  0x000000016f29b2d8  1        addresses only: the address of the same element of `level` in both runs
```

- Another element's address ranks first among the addresses that differ, after the real
  differences. It is an address fact. The program may have moved the pointer, or only the array
  moved under it: a stale pointer, storage reused, another layout.
- Its row may be at an earlier hit than the first real difference. Then a note names that hit,
  so you can read the hits in order: ``note: `now` holds a different element's address in each
  run at hit 4, before the first real difference (hit 5); …``.
- So read it with the program. Here `top` is computed from `level` (`level + peak(level, n)`), so
  `level[2]` against `level[1]` is the change.
- Where the pointer's address reads the same in both runs and only the array moved, `diff` leaves
  it out, as it does any value that reads the same. `queries compare` still says it.
- The same element's address says
  ``addresses only: the address of the same element of `level` in both runs``. It ranks with the
  address-only values.
- Where either run didn't prove it, the row says `addresses differ: it may point at another
  element too`. Then compare offsets yourself.

The store keeps each in `at_element_of` and `element_index`:

```sh
peeky sql "SELECT run_id, hit, selector, at_element_of, element_index FROM evidence
  WHERE at_element_of IS NOT NULL ORDER BY selector, run_id, hit"
```

A pointer or reference whose text after the address reads the same, such as a
`const std::string &` that reads `"Maya"` in both runs, ranks with the address-only values too.
It may still be another element with the same text.

### Offsets from a pointer in each run

Where the array is a pointer, such as a parameter, `diff` shows each pointer's offset from the
same base in both runs ([2.1](#how-far-a-pointer-is-from-a-pointer-parameter)). With the same
edit, at the `return` in `peak`:

```
run_a  run_b  hit  phase     selector  value_a             value_b             differs  note
1      2      1    observed  top       0x000000016f65f2d8  0x000000016b0232c4  1        addresses differ: by address, `level + 2` in run a and `level + 1` in run b; nothing proves they point into one array, so it may or may not be another element
1      2      1    observed  level     0x000000016f65f2d0  0x000000016b0232c0  1        addresses differ: it may point at another element too; compare offsets with address() (COOKBOOK 4.8)
```

- These rows rank with the addresses that differ, after the real differences. Two pointers of one
  type can point into separate blocks, such as `src` and `dst` on the heap. Their distance is then
  where the allocator put them, which can change between runs.
- So read the offsets with the program: here `top` is `level`'s own walker, and `level + 2` against
  `level + 1` is the change.
- The base itself says `addresses differ: it may point at another element too`. Nothing tells
  where it points, so the same offset in both runs ranks there too.

To count from another pointer than the base Peeky chose, use SQL. `element_size` is the size of
what a pointer points at:

```sh
peeky sql "SELECT t.run_id, t.hit,
    (address(t.value) - address(l.value)) / t.element_size AS from_level
  FROM evidence t JOIN evidence l ON l.run_id = t.run_id AND l.hit = t.hit
    AND l.phase = t.phase AND l.selector = 'level'
  WHERE t.selector = 'top' AND t.phase = 'observed' ORDER BY t.run_id, t.hit"
```

## 5.1 Stop, Look, Change, Finish

Read [COOKBOOK 5.1](../COOKBOOK.md#51-stop-look-change-finish) first.

### Read a caller's local

`session get --frame 1 result` reads a caller's local, and so does a probe's `--capture 1:result`
([2.10](../COOKBOOK.md#210-values-in-a-callers-frame)). In C and C++, a caller's local is withheld
while its declaration's code hasn't finished:

- **A call in the declaration:** in `long result = twice(seed);`, the caller stores `result` only
  after `twice` returns. The same holds for `long multi =` on one line and `twice(seed + 1);` on
  the next.
- **A declaration line with no code of its own:** the local is stored element by element, on its
  elements' lines. Examples are an array of structs, a nested struct, or a 2-D array initialized
  over several lines. Peeky goes by the code of the declaration's lines instead. It shows the
  local once the caller's line is below the declaration, and all of that code comes before the
  call. A loop's next pass, above the declaration, still withholds it.
  - So does an optimized build, or one with an `optimize` attribute or `#pragma GCC optimize`
    in any of its files. Either may lay a function's code out of source order.
  - So does the caller of an inlined call, such as an `always_inline` function's. Its address
    is where that call's code starts, not where a call returns. A frame the debugger rebuilds
    for a tail call's caller is withheld too.
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

### When the program crashes

A crash is the program's failure, not Peeky's. It can be a fault, such as `strlen(NULL)`, or a
signal. It can be `abort`, after an uncaught C++ exception, `std::terminate` or a failed `assert`.
Most such crashes stop in libc, the C++ runtime or `abort`, outside your sources.

LLDB stops on other signals too, and on a C++ throw or catch filter. So Peeky calls a stop a crash
only with proof:

- The signal is SIGABRT, SIGSEGV, SIGBUS, SIGILL or SIGFPE. On macOS, that includes a Mach
  exception that becomes one, such as `EXC_BAD_ACCESS`. It also includes LLDB's `hit program
  assert`.
- The program neither handles nor ignores that signal. Peeky reads the program's signal masks, and
  runs nothing in it: Linux's `/proc/PID/status`, or macOS's process information.

Any other such stop isn't a crash. Examples are a SIGINT or SIGPIPE the program handles, and a
filter's stop. Where Peeky can't read the masks, it doesn't call the stop a crash either.

A probe ends at the crash with `status: failed` and `PROGRAM_CRASHED`. The hits before it stand,
and the output so far is kept:

```
status: failed  hits: 1 before it failed  seconds: 1.224  max pause: 117.3 ms
error [PROGRAM_CRASHED]: The program crashed after hit 1: signal SIGABRT, outside the program's sources, in a call from main at abort.cpp:12, after a C++ exception that nothing caught. Peeky ended it there, so it has no exit status
hint: The program failed, not Peeky: its output may say why. Probe a line before the crash to see the values that led to it.
cleanup: terminated pids 20244, 20245
output so far (the program was stopped at its crash):
  before 1
  before 2
  libc++abi: terminating due to uncaught exception of type std::length_error: basic_string
```

- The message gives what the debugger says, such as `signal SIGABRT` or `EXC_BAD_ACCESS`. It
  names the nearest frame of your code, among the first 64.
- An uncaught exception is named where `__cxa_throw` called `std::terminate`. LLDB doesn't say
  its type, but the C++ runtime writes it to the output.
- JSON has `crash`: `description`, `outside_program`, `frame`, `function`, `location`, and
  `uncaught_exception` where it applies.
- Peeky ends the program at the crash, so there is no exit status. On macOS, a fault would stop
  again at each `continue`.
- An exception that the program catches never stops it.

At a stop that isn't a crash, a probe reads nothing and lets the program go on, with a warning.
For a signal, the warning adds that LLDB may not deliver it to the program. So the run after it can
differ from one without Peeky.

At the 100th such stop in a row, the probe ends with `BUDGET_EXHAUSTED`. The same stop means the
same description, place, and nearest frame of your code. A loop may raise a signal that often, or
LLDB may stop there again without letting the program past. On
macOS, a fault that the program handles does that. Peeky can't tell which, and the message says
so. Probe a line before those stops, or use an input that doesn't raise them.

In a session, the state says the same, and `crash` is in its JSON. At any other signal stop, or a
C++ throw or catch filter's, the JSON has `signal_stop` instead. It has the same fields, and
`kind`: `signal`, `exception` (a macOS Mach exception), or the filter's name, such as
`cpp_throw`. Nothing in a frame outside your sources is read, at a crash or any other such stop.
`session inspect` there is refused, and the session goes on:

```
peeky: [INVALID_REQUEST] Nothing in frame 0 (__pthread_kill) is read: it is outside the program's sources, where the program crashed (signal SIGABRT). Frame 12, main at abort.cpp:12, is the nearest of your code: `session inspect --frame 12`
```

`--frame N` reads that frame as any caller's, even past the 10 frames listed. Its values are
never settable. A frame of your code whose line or module doesn't match the build still fails,
with `DEBUGGER_ERROR`.

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
- A `char[N]` shows its text up to its first NUL, then a note of what follows it
  ([details](#characters)).
- A `static` local reads where the code at the stop names it, and is never changed
  ([details](#read-a-static-local)).
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
- **`[unproven]`** marks a value read through a pointer that Peeky can't show points at a live
  object: into a call that returned, a heap block, or past an array. Its note says why. Don't
  trust it as the object's value ([details](#when-what-a-pointer-points-at-is-unproven)).
- On a line a loop returns to, such as a `do` body's first line, read the value on the next line.
- Below a `setjmp` call, a local declared at or below the stop shows a `note` instead of
  `no value yet`. It reads plainly below its declaration.
- **Same-named locals:** a plain name means the declaration the code names at the stop. Write
  `name @ file:line` to select another
  ([details](#same-named-locals-which-one-a-name-means)).
- **An object ended by an explicit destructor call** isn't told from a live one. Peeky can't tell
  that the program ended an object's lifetime with `std::destroy_at(p)` or `p->~T()`, and hasn't
  yet reused its storage. It shows the bytes the object last held, with no note.
  - That holds for every object: a local, a member, an element, an entry of a container.
  - Read such an object after the program builds a new one in its place.
  - Where Peeky reads an unordered container's nodes itself, it withholds what an entry points
    at in heap memory, which may be freed: a vector's elements, a long string's text
    ([details](#standard-containers)). A short string's text in the node shows as it was.

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
- A refused change to a structured binding by reference says what it binds. For `v` in
  `for (auto &[k, v] : m)`, change what fills `m`. For `auto &[a, b] = p;`, change `p.b` where it
  is settable. For `auto &[at, n] = *owner;`, change it where `*owner` is owned.
- In C, the program's sources may call the function only once. Then a refusal for a pointer
  parameter names that call:
  ``push has one caller: m.c:15 in main, which passes `&r`, where `r.tail` is settable``. If that
  caller only passes its own pointer on, the refusal follows the calls to the frame that owns the
  struct. Or it says that it can't.
- Sometimes the build's debug information rules out a probe's `--set` like this in every function
  on its `--at` line. Then the probe is refused before the program runs. The debugger's launch
  request isn't sent, and no run is saved. A root local whose type isn't one of the integers
  `set` lists is refused so too, by the name the debugger gives that type:
  ``in main, only writable integer locals of these types support native mutation: …; `tracks`
  has type `const long` ``. That covers a `const` or `volatile` one, a `char`, `bool`, float,
  enum, pointer or reference, a typedef `set` doesn't list (`typedef long Count;`), and
  `_BitInt(32)`. A struct, an array or a `std::string` is judged at the stop.
- The stop judges a root by the same names, its debug information's too. LLDB 18 and 20 show a
  `_BitInt(32)` as `int`, but it is never `[settable]`.

```sh
peeky probe examples/c/pagination_bug.c --at 25 --capture 'start,page,items[1]' --set start=0
```

```
hit  phase      line  function  selector  type  value
1    observed   25    main      start     int   3  [settable]
1    observed   25    main      page      int   1  [settable]
1    observed   25    main      items[1]  int   20  [settable]
1    after-set  25    main      start     int   0  [settable]
after-set: 2 number, flag, null or text rows equal to the observed value left out; `--json` has all
set start: 3 -> 0
status: completed  hits: 1  seconds: 0.745
output:
  PAGE:[10, 20, 30]
```

On Linux with LLVM 18, the output is labelled `output so far (the program ended, but its output
may still have been arriving):`. A last line says that the debugger sent more text. Both are
true: lldb-dap 18 crashes as Peeky closes the session (`free(): invalid pointer`). So Peeky can't
prove that it sent all of the program's output. LLVM 20's lldb-dap ends cleanly, and the label is
`output:`.

The label stays on every such probe. The last line is whole on a build's first probe in a state
directory, then short: `the debugger again sent more text as Peeky closed the session`. So is
LLDB 18's warning about a GCC build's `_M_local_buf`. A build is its `build_id`, a best-effort
identity used only for this: a candidate, a changed source or another compiler is another one
([2.7](../COOKBOOK.md#27-json-for-scripts)). `--json` has both whole.

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
string is cut between characters, with LLDB's `...`. A note on a string or a char array is kept
whole.

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
- So a partly filled array previews what its memory holds past what the program wrote. In
  `struct water found[96]` with `count` 2, `found[2]` on are leftovers, such as
  `{kind = '\x90', at = 0x…}`. Peeky can't tell which elements the program filled. Capture the
  count with the array, and read only the elements below it.
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
- A container that holds more than its preview shows ends with `…`. Under a probe's table, a line
  says how to read the rest:
  ``preview: `by_hour` holds 9 entries, more than its preview shows; read one with `by_hour[K]`
  (key K) or `by_hour[#N]` (the Nth), or list them in a session: `peeky session get by_hour` ``.
  A sequence's line names `v[N]` instead.
- A set (`std::set`, `std::multiset`, `std::unordered_set`, `std::unordered_multiset`) shows its
  elements: `size=2 {1, 3}`.
- A `std::multimap` keeps its summary, `size=2`: its keys repeat.
- An unordered container (`std::unordered_map`, `std::unordered_set` and their multi forms) can
  be misread by LLDB's libstdc++ formatter. It counts one as empty when the debug information
  doesn't name its element type. That happens in a GCC build, and with LLDB 18 for one named by
  a typedef (`using Ids = std::unordered_set<long>`). It can also decode the entries as another
  type, or from the wrong place (see below).

  So Peeky checks LLDB's count against the container's own `_M_element_count`, and the type and
  place LLDB decodes. Where either is wrong, Peeky reads the entries from the container's own
  nodes instead. The row shows them as any container's, with `view: memory`:
  `ids  Ids  size=1 {9}`. Keys, positions, previews and listings read them too.

  Peeky reads the nodes only where all of these hold:

  - the debug information gives the nodes' type, and where each node holds its entry;
  - the entries hold numbers, `bool`s, characters, enums, pointers and `std::string`s, in pairs
    and structs. A `long double` doesn't count: the debug information doesn't say which of its
    formats its bytes hold. Nor does a struct with a base class or a bitfield;
  - its chain of nodes ends after exactly `_M_element_count` of them. Peeky walks at most 16,384
    nodes with a debugger that reads memory, as lldb-dap 20 does, and 512 with lldb-dap 18
    (see below);
  - nothing may be changing the container
    ([A container that something may be changing](#a-container-that-something-may-be-changing));
  - the build is unoptimized, and its source asks to optimize no function: no `optimize`
    attribute (`[[using gnu: optimize("O2")]]` too) or pragma. An optimized function may
    keep a value it stores in a register, with the nodes unchanged;
  - the row has no note, such as `no value yet` or leftovers;
  - every pointer and reference on the way refers to a live object, as Peeky can show it
    ([When what a pointer points at is unproven](#when-what-a-pointer-points-at-is-unproven)).
    A heap block or a returned call's frame doesn't count. With lldb-dap 18, which gives no
    address for a container local, neither does a pointer or reference to one.

  Each entry shows as the debugger shows such a value in a local:

  - a `float` or `double` with LLDB's digits: `0.10000000000000001`, `1.0E+10`;
  - a character with LLDB's escapes: `'a'`, `'\n'`, `'\xc8'`. A wide one shows as its literal,
    `L'a'`. A code that is no valid character shows in hex, `U'\x110000'`, where LLDB would
    show `U'\U0000fffd'`;
  - an enum as its enumerator: `Teal`. Where no enumerator holds the value, or several do, it
    shows the number, with the enum's type;
  - a pointer as its address. Peeky never reads what it points at, so `ptrs[#0][0]` is refused;
  - a `std::string` as its text, where that text is in its own small buffer in the node: its
    `_M_p` points at its `_M_local_buf`, which holds up to 15 bytes. Elsewhere, its text is in
    heap memory, which Peeky can't show is still allocated. That is so for a longer string, and
    for a short one after `reserve()`. A string destroyed in place, before a new one is built
    there, keeps a pointer to its freed text. So that string alone shows `<not read>`, and
    reading it says why:
    ``lost[1]: not read: Peeky reads a string in an unordered container's node only where its
    text is in the node: this one's is outside it, in heap memory Peeky can't show is still
    allocated; print it from the program``.

  A container held in an entry is read only where it is inside the node, as a `std::array` is:
  `size=1 {1: size=2 [4, 5]}`. Any other alone shows `<not read>`, and reading it says why:

  - a `std::vector`'s elements, and a nested unordered container's nodes, are in heap blocks.
    Peeky can't show such a block is still allocated. A vector destroyed in place, before a new
    one is built there, keeps pointers to its freed buffer:
    ``lists[1]: not read: Peeky doesn't read a `std::vector` held in an unordered container's
    node: its elements are in a heap block, which Peeky can't show is still allocated; print it
    from the program``;
  - Peeky doesn't read a `std::map`, a `std::list` or another container there:
    ``trees[1]: not read: Peeky doesn't read a `std::map` held in an unordered container's node;
    print it from the program``.

  A container of more than 256 entries is read too. Peeky walks its whole chain to check its
  count. The row reads its first 256 entries, and a later one when a position or key asks for it:
  `big[#350]`, `big[399]`. `big[K]` compares every key. A listing says how many entries it
  doesn't show: `... 336 more (400 entries in all)`.

  How long a chain Peeky walks depends on the debugger:

  - with lldb-dap 20, which reads memory, up to 16,384 nodes. Each read takes the 4,096 bytes
    around a node, so nodes close together cost one read;
  - with lldb-dap 18, up to 512. It reads each node through a longer variable path than the last,
    so a walk's cost grows with the square of its length.

  A walk also stops at the reads' budget: 4,096 reads and 4 MiB in one inspection. Nodes spread
  over memory can reach it before 16,384. Past either limit, the row keeps its note.

  Otherwise the row has no value, and no key, position or listing is read in it. For example, in
  a GCC build:

  ```
  weights  std::unordered_set<long double>  <not listed: the debugger cannot list this build's unordered containers: it counts 0 entries, but the container's own count (`_M_element_count`) is 1; print its entries from the program>
  weights[#0]   <[#0] not read: weights is not listed: the debugger cannot list this build's unordered containers: …>
  ```

  Inside a preview it shows `<not listed>`: `{us = <not listed>, n = 2}`. A pointer to one is
  withheld the same way. Where Peeky can't read the container's own count, the row is withheld
  too, and says so. That happens for one reached through a base class or an anonymous member.

  Where Peeky would read the nodes but can't at this stop, the row says why, and what to try:

  - Through a pointer or reference that Peeky can't show refers to a live object. With lldb-dap
    18, which gives a container local no address, a reference parameter to one is such a case.
    Where the listed frames hold locals of its type that Peeky couldn't place, and that read
    there, the row offers them to inspect. Peeky doesn't prove which one, if any, it refers to:

    ```
    stock  Stock &  <not listed: the debugger counts 0 of its 3 entries, and Peeky reads its nodes only through a reference it proves refers to a live object, not `stock`; inspect this candidate local, `on_hand` in main (`--frame 1`): it may be what `stock` refers to>
    ```

  - While a call may be changing the container
    ([A container that something may be changing](#a-container-that-something-may-be-changing)).
    The row names that call, and says to retry after it returns. The entries may still be
    withheld then, for example if the container has grown past what Peeky walks:

    ```
    on_hand  Stock  <not listed: the debugger counts 0 of its 2 entries, and Peeky doesn't read them from its nodes while a constructor, destructor or assignment operator below this frame (Movement::~Movement) may be part-way through changing it; retry after that call returns>
    ```

  Elsewhere it says to print the entries from the program.

  A right count isn't enough. LLDB decodes each entry as the allocator's first template
  argument, at a fixed place in each node. So Peeky uses LLDB's listing only where the debug
  information proves that type and place right. These are read from the nodes instead, or
  withheld, each with why:

  - an allocator whose first template argument is another type, such as
    `WeirdAlloc<double, int>` for `int` entries;
  - a type aligned to more than 8 bytes, such as `long double`;
  - with LLDB 18 or 19, a map whose type's name holds `set`, such as
    `std::unordered_map<int, Asset>`;
  - one Peeky can't place in the build's debug information.
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
- An unordered container that Peeky reads from its own nodes says so of itself:
  ``… and Peeky doesn't read them from its nodes while a standard-library call below this frame (…) may be part-way through changing it; retry after that call returns``.
- With several threads, it says:
  ``the program runs 2 threads: a stop pauses every other one wherever it is, perhaps part-way through changing `shared` …``.
- Such a row has no handle. So a selector below it is refused with the same reason. A map's key
  lookup says it too: ``cannot read the keys of o.tags's entries: not shown: …``.

#### Characters

A narrow character array, such as `char name[32]`, shows its text up to its first NUL. A note after
it says what follows the text. Those bytes are never shown as part of the text:

| What the array holds | Its value |
| --- | --- |
| The text, a NUL, then bytes that aren't all NULs | `"Adeyemi" (then a NUL and 24 bytes not shown)` |
| The text, then only NULs | `"hello" (then 59 NUL bytes)` |
| A NUL first | `"" (then a NUL and 31 bytes not shown)` |
| No NUL | `"abcdefgh" (no NUL in its 8 bytes)` |
| No NUL in the bytes Peeky could read | `"abcdef" (then 10 bytes not read)` |

- LLDB's text shows the bytes up to the last one it read that isn't a NUL. It shows the same
  for an array that runs into memory it can't read. So Peeky reads the bytes after that text
  again, and calls them NULs only where it read them.
- It reads them from memory where the debugger can. Otherwise it reads them as the debugger
  lists the array's elements, at most 64 of them: lldb-dap 18 reads no memory.
- It reads them only where nothing on the way to the array is in doubt. A pointer on the way
  must be proven to point at a live object. The value must have no `note`, and the build must
  be unoptimized. Otherwise the bytes after LLDB's text are `not read`.
- So a NUL past those 64 elements is not shown, and the note says the bytes were not read.
  `text()` is NULL for such a row.
- This holds for an array of `char`, `signed char`, `unsigned char` or a `char8_t` typedef, by
  LLDB's name for its type. An array under a typedef's own name, such as `Tag` for `char[8]`,
  keeps LLDB's text.
- A printable UTF-8 character shows as itself: `"Malmö"`. Every other byte is `\xNN`, or C's own
  escape, such as `\n`. So byte 0x01 is `\x01`, never LLDB's `\U00000001`.
- A byte before a hex digit is written in octal, `"\001a"`, so that C reads it back the same.
- `--json` rows and `session inspect --json` keep every byte read, NULs included, as `buffer`:
  `"Adeyemi\0\x80\xf8\xe6\xef\x01…"`. It ends `...` where the array holds bytes that weren't
  read. The store keeps only the value.
- LLDB reads at most an array's first 1024 bytes. In a longer array, the bytes after the NUL are
  counted as not shown. With no NUL in those 1024 bytes, the text ends `...`, with no note.
- In a preview, the note is kept whole, and a long text before it is cut with `...`:
  `{label = "desk" (then a NUL and 7 bytes not shown), id = 7}`.
- In SQL, `text(value, language, type)` gives the text up to the NUL
  ([4.9](../COOKBOOK.md#49-a-lists-elements-and-a-strings-text)).
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

Peeky builds `.cpp`, `.cc` and `.cxx` files as C++17, with debug information, with clang or GCC.
Its C++ rules are checked against both compilers' debug information. Those rules cover locals
declared below the stop or not yet initialized, previews, map lookups, libstdc++ strings, setting
a string's text, and same-named locals. C builds with clang or GCC too.

Before a C++ build, Peeky asks the compiler for its `--version`. Clang gets `--driver-mode=g++`,
which keeps it in C++ mode; g++ needs nothing. A compiler that is neither clang nor GCC is refused
before anything is built:

```
UNSUPPORTED: C++ targets need clang++ or g++ (Peeky's C++ rules are checked only against their debug info); set PEEKY_NATIVE_CXX to one of them (/opt/intel/bin/icpx is neither clang nor GCC, by its --version)
```

To fix it, set `PEEKY_NATIVE_CXX` to a clang++ or a g++.

- Peeky goes by what the compiler says, never by its name. macOS's `g++` is Apple clang, so it
  builds as clang.
- A `ccache` or `distcc` symlink named `clang++` is refused: its own `--version` is neither
  clang's nor GCC's.
- A wrapper script that runs clang++, such as `exec ccache clang++ "$@"`, passes clang's
  `--version` through. So it works.
- Peeky asks once per compiler file, which it knows by its path, modification time and size.
- A compiler that doesn't answer within 10 s is refused as timed out. Peeky ends it, and every
  process in its process group.

#### What is different in a GCC build

GCC's debug information differs from clang's, and LLDB misreads some of it. Where Peeky can't
prove a value, it withholds it, and the row says why. These are the known gaps:

- **Unordered containers** are read from their own nodes. LLDB finds no element type in GCC's
  `std::allocator`, so it counts each one as empty. Peeky reads the entries itself where it can
  prove how. A container whose entries hold a `long double` stays withheld. So does one longer
  than Peeky walks: 16,384 entries with lldb-dap 20, 512 with lldb-dap 18. A container held in
  an entry, other than a `std::array`, is withheld on its own, with why. So is a string whose
  text isn't in its own small buffer in the node, where its `_M_p` doesn't point at its
  `_M_local_buf` ([details](#standard-containers)).
- **A one-line function or lambda** stops before it stores its parameters. They show `no value
  yet` there, and can't be changed
  ([details](#at-a-functions-start-before-it-stores-its-parameters)).
- **GDB's DAP** isn't supported. Peeky debugs GCC's builds with lldb-dap.

Some things only look different:

- **A lambda's captures** are read through `__closure`, which lldb-dap doesn't list
  ([details](#read-a-lambdas-captures)). In an optimized build, and at a one-line lambda's first
  stop, a captured name is `not found`, and the row says why. To see a by-value capture there,
  stop where the lambda is made, or print it inside the lambda.
- **Where a lambda is made:** GCC builds the lambda on its last line, `};`, in the function around
  it. It gives the lambda's first line only the lambda's prologue. So `--at` that first line
  stops inside the lambda, on its next line, and the probe warns where the lambda is made. A stop
  on the `};` line in the outer function comes before the lambda copies what it captures by
  value.
- **A caller's aggregate** declared over several lines, such as `struct trio t = {seed, {…}}`, gets
  no code on its first line. Peeky reads its elements' lines instead, and a caller frame shows it
  once all of their code comes before the call ([details](#read-a-callers-local)). In an optimized
  build, or above an inlined call, it stays withheld in a caller frame.
- **A `const` local aggregate** that GCC builds at compile time, such as `const Span cs{8, 9};`,
  is in read-only data, not in the frame. It reads as a `static` local does, and is never changed
  ([details](#read-a-static-local)).
- **Function names:** LLDB names a `static` function from its declaration, from the global scope:
  `::trim_left(const std::string &)`, where clang's build gives `trim_left(std::string const&)`.
  Tables and messages drop that leading `::`, so the column reads `trim_left` in both. The store
  keeps the debugger's name, and `short_function(function) = 'trim_left'` matches either. A
  lambda is plain `operator()`, and an unnamed class's constructor is `<constructor>`. The
  `short_function()` hint names what the store holds.
- **Lines:** GCC runs a default member initializer on the line that builds the object. So `--at`
  the member's line never stops, and `describe` leaves that line out. A range `for` header stops
  once, before its loop, and says so.
- **Loops:** GCC tests a `for` loop at its bottom, which branches back to the body's first line. A
  local declared on that line shows a `note` there: its value may be the last pass's, or leftovers
  on the first pass. A `do` body's first line does the same
  ([details](#why-a-local-shows-no-value-yet)). A pair's or tuple's structured binding shows `no
  value yet` there instead. Until its declaration runs, the reference it is read through may point
  anywhere.
- **A structured binding of a pair or tuple:** GCC holds `t` in `auto [t, u] = make(i)[0];` as a
  reference to its element. LLDB shows that reference's address as `t`'s value. Peeky shows the
  element instead: `t = 10`, `{x = 3, y = 4}` for a struct, `"apple"` for a string.
  - The row's `type` is the binding's: `std::tuple_element<0, std::pair<long, long> >::type`. Its
    `debugger_type` keeps LLDB's, with `&&`.
  - Apple clang 21 does the same, with `long &&`. Clang 18 marks the reference as its own, so
    LLDB doesn't list `t`, and it is `not found`.
  - An element that is a pointer shows its value, but Peeky doesn't follow it: `p[0]` can't be
    read. Read what it points at where the pair is made.
  - It can't be changed, nor can its fields: a change would write through the reference.
  - A binding by reference (`auto &`, `const auto &`, `auto &&`) is read only where Peeky proves
    that what it refers to is still alive. That is one of these:
    - an object of the stopped frame, or a member of one: `auto &[a, b] = p;`, `= o.inner`. A
      union's member doesn't count, since another member may become active.
    - an element of a local `std::vector`, `std::map`, `std::set` or `std::multiset`: `for (const
      auto &[k, v] : m)`, or `auto &[r, s] = v[0];` for a vector. The container must use the
      default allocator, and a map or set `std::less`. Its elements must be numbers or
      characters: a map's key and value, or a `std::pair` or `std::array` of them. So no
      constructor, allocator or order of the program's sees where an element is. While the
      binding is in scope, the code may only call the container's members that give a value,
      such as `m.size()`. Anywhere in the container's scope, the code must keep no
      pointer, iterator or reference into it: no `v.data()`, `m.begin()` or `&v[0]`. It must not
      pass the container on or name it in a lambda. At the stop, the binding must point into the
      vector's elements, or into one of the map's entries as the debugger lists them (at most 256).
  - The program must not replace `operator new` or `operator delete`, which `std::allocator`
    calls, in any form. Peeky reads that from the build's debug information, and from
    `LD_PRELOAD`, `LD_AUDIT` and `DYLD_INSERT_LIBRARIES` in the launch's environment. On Linux it
    also reads `/etc/ld.so.preload` on the host that runs the program, at each launch. If that file
    names a library (blank lines and `#` comments don't count), or exists but can't be read, the
    binding is withheld.
  - A call's result is never read: `auto &&[p, q] = one(i);` or `const auto &[x, y] = one(i);`.
    C++ keeps it alive until the binding's scope ends, but the function may keep a pointer into
    it. With the named return value optimization, `P r; saved = &r; return r;` builds `r` where
    the caller keeps the result. The row says `… a call's result, which the function called may
    keep a pointer into`.
  - Each of the binding's names must be a number or a character. A binding with a pointer in it
    stays `not shown`, since the pointer may point at another element.
  - Each use of a name must be in an expression of numbers and characters alone: the binding's
    names, locals of such types, number literals, casts such as `(long)`, and their built-in
    operators. `use(k * v)`, `if (k > 1)`, `long w = k;`, `w += v;` and `b += 1;` are fine.
  - A literal counts only with the language's own suffixes, such as `1u`, `2.5f` or `0x1Fll`. A
    user-defined literal, such as `1_sink`, `10ms` or `2i`, calls an operator of its own.
  - An operand of another type may bring an overloaded operator that keeps the name's address,
    so `std::cout << k`, `sink << v` and `use(m.count(k))` withhold the binding. So do `&k`,
    `auto &r = k`, `arr[k]` and a branch of `?:`.
  - The name alone may be an argument of a call whose every function takes it by value, as
    `use(long)` or `printf`'s `...` do.
  - Peeky reads that code as the compiler preprocessed it, so a macro counts as what it expands
    to. In an optimized build, only an object of the frame is read.
  - Anything else may be gone by the stop. `auto &[t, u] = make(0)[0];` refers into a temporary
    that its line destroys. Such a row shows `not shown: the binding refers to an object Peeky
    can't prove is still alive`, and why. Read that object where the code names it. For a
    container's element, the row says to read the container itself, such as the map a loop
    walks. Or print the value from the program. Binding by value instead would break a loop
    that writes through the binding.
  - In an optimized build, a stop on the binding's own line shows `no value yet`. Peeky can't
    tell whether that pass has set its reference yet.
  - Peeky reads the binding's line to tell it from a reference the code declares with the
    element's type: `std::tuple_element<0, P>::type &r = p.first;` keeps its address first. If
    it can't read that line, the row is `not shown`.
- **Strings with lldb-dap 18:** GCC describes `std::string` whole, so LLDB's summary works. A text
  cut at a NUL keeps its note: `"a" (shown up to a NUL at byte 1 of 3)`.
- **A const pointer:** GCC's `this` is `Widget *const`. A change through it is refused as a
  pointer's, as clang's is.
- **A map ordered by a lambda:** LLDB names the lambda `key_compare`. A root local's row shows
  GCC's name for it, `std::map<int, long, main()::<lambda(int, int)>>`
  ([details](#maps-that-compare-keys-their-own-way)).

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
- Peeky preprocesses the sources at the same time, then compiles them at the same time: up to 8
  at once, or `PEEKY_BUILD_JOBS`. Each source's commands write only that source's files, and they
  may start in any order. When several sources fail, Peeky reports the first one's error, as a
  build of one source at a time would.
- What a compile writes can still depend on when it runs, as between any two builds. `__TIME__`
  and `__DATE__` do, and so may a compiler wrapper that shares a cache between compiles. Set
  `PEEKY_BUILD_JOBS=1` to compile one source at a time.
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
  the last pass's value. A structured binding of a pair or tuple is withheld there instead. It is
  read through a reference that its declaration sets, which may point anywhere until then.
- **Where another line's code lies just before the line,** Peeky reads the stop's disassembly.
  A header over two lines, `for (` then `i = 10; i < 13; i++)`, runs into the line with the local
  set. The local reads only where every way into the line comes that way.
- In C++, clang puts the code that cleans up after an exception there, at `-O0`. That code ends
  in a jump and never runs into the line, so the local is withheld.
- Sometimes Peeky can't tell which way control came. Both kinds of way in may reach the line, or
  a call that may not return, or a computed jump. An exception may enter the line, as the
  binary's exception tables say. Or Peeky can't read the disassembly. Then the row has a `note`:
  it may be leftovers. A change there is refused.
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
  instruction, where the range runs first, unless the loop's body has code on that line too;
- a `thread_local`, which the debugger lists apart as `q @ file:line`.

A block that opens on the stop line counts. So does the block of an `if constexpr (…)`, an
`if consteval` or an `if !consteval`.

A lambda's body that opens and closes on the stop line is that lambda's code, with every block
in it. That holds for a generic lambda too, and whatever follows its parameters: `constexpr`,
`mutable`, `noexcept(…)`, an attribute or a trailing return type. What they declare counts in the
lambda's own frame, but not in the function that holds the lambda. It counts there too where
Peeky can't tell which function the frame runs, or where the lambda goes on past the line. In a
frame on a line that holds several lambdas' bodies, the row says `may not be what the code reads`.
Peeky can't tell which of them the frame runs.

A statement's header can also name the outer local, and run before the line's own declaration of
that name exists. For example, `if (t) { static long t = 5; use(t); }` or
`for (auto [t, u] : make(t)) { use(t); }`. A mention isn't always a read, as in `sizeof(t)`. So
the row says `may not be what the code reads` wherever Peeky can't tell that the header has run:

- at every stop in an optimized build, and in a caller frame;
- on any line of a header that spans lines;
- in an unoptimized build, at any stop the line table doesn't place after the header's `)`. That
  includes the line's first instruction. It also includes a stop back from a call in the header,
  as in `if (check(t) && t > 2)` after `step --out` from `check`. A range-`for`'s declaration
  and `:` carry its implicit code, all of it run after its range. That is the `begin` and `end`
  setup, each pass's test and binding, and the range's cleanup. So a stop there counts as past;
- at every stop on the line when part of the header runs on each pass. That is a `while`'s
  condition, or a `for`'s condition or increment;
- at every stop on the line when the declaration is in a lambda in the header, where it counts
  (see above).

Peeky trusts the line table's column only on a line of plain ASCII text, without tabs, that no
macro changes. Any `if`, `switch`, `while` or `for` header counts, also one with an
init-statement: `if (long k = t; k)`. A member's name, such as `p.t` or `p.template t<0>()`,
isn't the local. Otherwise Peeky counts every `t` the header spells. That includes one the header
declares itself, as in `for (long t = 0; …)`, or a lambda's parameter `t`. There the row may say
`may not be what the code reads` though the plain note would be true.

The line can also name the outer local outside the scope of the declaration that hides it. For
example, `use(t); { static long t = 5; use(t); }` reads the outer `t` before the block. And
`do { static long t = 5; use(t); } while (t--);` reads it in the condition, after the block ends.
There the row says `may not be what the code reads` at every stop on the line. The note says
whether the line names `t` before that declaration, after its scope ends, or both. Peeky can't
tell whether that code reads this local, or another variable of that name. As with headers, it
counts every `t` the line spells, except a member's name and a name a declaration there surely
declares, as in `long t = 3;`. A name an expression may hold counts, as in `return use(0), t;`,
`ok and t` or `a < b > t`. In C++, `A<B> t` counts as a declaration only beside a keyword or a
`::`, as in `std::vector<long> t`. What an `if`'s header declares, as in
`if (auto [t, u] = make(); ok)`, is in scope in its `else` too. It is in scope up to the end of
the statement the header controls, past any label: a `do` loop's `while (…);`, or a labelled
block's `}`. Where Peeky can't tell where that statement ends, the note says the declaration may be
out of scope there. In a lambda's own frame, only the lambda's body counts.

Peeky reads the source as the build's compiler preprocesses it (`-E`, with the same flags). So
whatever a macro expands to counts, and only what a conditional keeps. A prototype's parameter
names don't count. Where Peeky can't read the source that way, the row says
`may not be what the code reads`: the preprocessor fails, or a system header is included inside
the block.

In optimized builds (`-O1`, `-O2`), a block's declaration hides the outer local too. This holds
though the outer local lives in a register or as a constant, not in the frame:

- On a range-`for`'s line, the stop can't be told from the line's first instruction, before the
  binding exists. The row says `may not be what the code reads` and names the binding. When the
  loop's body has code on that line too, this holds only where the header names the outer local.
  Otherwise the row says `not what the code reads`.
- A block's `static` that never changes is kept as a constant, and lldb-dap lists it among the
  frame's locals as `t @ file:line`. That row is the static the code names, so it is shown. The
  outer local's row says the static is `listed separately as` it. That holds for a parameter too,
  which lldb-dap then lists as `t @ file:line` as well. Where the parameter and the static are
  declared on one line, Peeky can't tell which one the row is. The row says
  `may not be what the code reads`.

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
- In a C++ target, a frame can run a line of the program's sources or headers while its
  function's declaration is outside them, as a `#line` from bison or flex puts it. Peeky then
  finds no function it can place for the frame, inlined or not.
- Such a frame shows every local and parameter as `not shown`. Read what the function returns
  after `step --out`, or stop in a function that `describe` lists. C targets aren't judged this
  way.

#### `this` in C++

In C++, `this` is withheld while its object is being built:

- in a constructor;
- in any frame a constructor called. Peeky reads up to 64 frames to tell; past that, it
  withholds;
- in a lambda defined inside a declaration or a constructor that may still be running. The note
  names that declaration. Peeky follows every function around a nested lambda, up to 256 of
  them, and withholds `this` past that;
- in a lambda that a class's default member initializer defines: `long b = [this] { ... }();`. A
  class with no constructor runs its initializers where the object is built.

An unnamed struct's constructor (`$_0::$_0()`) counts as a constructor too. In a GCC build, a
lambda's captures are withheld in the same places ([captures](#read-a-lambdas-captures)).

### Read a `static` local

lldb-dap lists a function's static storage apart from its locals. That holds a `static` local,
and a `const` aggregate GCC builds at compile time. Peeky reads the one the code at the stop
names. `inspect` lists it under `[Statics]`. Here `tick` declares `static long calls` at line 7,
and again in a block, at line 13:

```sh
peeky probe tick.c --at 15 --capture calls,cs --hits 2
```

```
hit  phase     line  function  selector  type        value
1    observed  15    tick      calls     long        101  [not settable: static]
1    observed  15    tick      cs        const span  {start = 8, end = 9}  [not settable: static]
2    observed  15    tick      calls     long        102  [not settable: static]
2    observed  15    tick      cs        const span  {start = 8, end = 9}  [not settable: static]
```

- The code names a `static` local from below its declaration to the end of its block. An inner
  block's declaration hides an outer one. Peeky reads that from the source, as the compiler
  preprocesses it, and from the debug information.
- Above its declaration, on its line, or past its block, the selector is `not found`, and says
  why: ``tick declares 'count' in static storage (…), at line 18, in a block this stop is outside
  of, so the code here doesn't name it; read it in that block, below line 18``.
- Where Peeky can't tell which declaration the code names, the note says why, and it isn't read.
- Where the frame has a local of that name, the name selects the local, as before
  ([A name a block declares again](#a-name-a-block-declares-again)).
- Where lldb-dap lists several of one name, the row is `calls @ tick.c:13`. One a C++ symbol
  names may read `Acc::run()::calls`. Select either as `calls`.
- A template's instances share the declaration's file and line. Peeky reads only the stopped
  instance's, as `long tick<int>(int)::calls` names it, or as its address proves it.
- An inlined or copied function's `static` is one declaration in every copy. lldb-dap lists none
  for an inlined call's frame, so there the note says the debugger doesn't list it.
- In an optimized build, the row carries a note. Optimized code may reorder stores and keep
  values in registers, so the value it uses may differ from what the declared address holds.
- C++ runs a `static` local's initializer the first time control passes its declaration, unless
  it is a constant. Until then the row is `no value yet`. Peeky reads the guard variable the
  compiler gives it (`_ZGVZ4tickE1w`), and shows the value only once that says it has run.
- A `static` local is never changed: Peeky changes only values a frame holds itself. Its rows say
  `[not settable: static]`. `--set calls=5` is refused before the program runs. A `const` one's
  fields are refused as const.
- `describe` marks it among the function's names: `calls (long, static)`. So it marks a `const`
  aggregate GCC builds in read-only data.
- A lambda is a function of its own: a `static` of the function around it isn't read there.

### Read C++ members

- In a C++ method, read members through `this`: `this.head_`. A bare `head_` that is a member
  gives an error that names that selector. Java methods work the same way.
- C++ changes no member through `this`, because `this` is a pointer. Java does: `this.total=0`.
- Peeky never reads a member in place of a local. The debugger's list of locals can be
  incomplete, for example for statics or classes built without local-variable tables. So a missing
  local doesn't mean that the code uses the member.

### Read a lambda's captures

Inside a lambda, select a capture by its name in a GCC build. In a clang build, select it as a
member of `this`:

| Build | Selector | Row |
| --- | --- | --- |
| GCC | `count` | `count  long  25  [not settable: capture]` |
| clang | `this.count` | `this.count  long &  0x…` |

- clang lists the lambda's object as `this`. A bare `count` there says to select `this.count`.
- GCC passes the lambda's object as `__closure`, which lldb-dap doesn't list. Peeky reads it as a
  hover, which runs nothing. `session inspect` lists the captures under `[Captures]`.
- In a GCC build, `describe` lists the captures among the lambda's names, with how each is
  captured and the type the lambda's object holds: `tariff (Tariff, capture by value)`,
  `over_peak (long int &, capture by reference)`. `this` is the object's capture: `this (Acc *,
  capture of the object by reference)` for `[this]`, `this (Acc, capture of the object by
  value)` for `[*this]`.
- A capture by value shows the lambda's copy. The program may have changed the outer variable
  since the lambda was made.
- A capture by reference shows what it refers to: `count  long  25`. The `type` is that value's,
  and `debugger_type` keeps LLDB's `long &`. A struct, a string or a container shows its text
  with no address, and its members stay listed.
- A pointer captured by reference shows its value, and its text is checked as a pointer local's
  is. Peeky doesn't follow it: `word[0]` can't be read there.
- Peeky checks the lambda's object as it checks what a pointer points at
  ([unproven](#when-what-a-pointer-points-at-is-unproven)). `std::sort` calls a copy of the lambda
  that no listed frame holds. So its captures are marked `[unproven]`, with a note that names
  `__closure`.
- It checks what a capture by reference refers to the same way. A capture of a heap value is
  marked: ``read through `held`: Peeky can't show that `held` refers to a live object …``.
- A capture is withheld where clang's `this` would be, such as in a lambda that a running
  declaration defines ([`this` in C++](#this-in-c)).
- In `[*this]`, `this` is the lambda's copy of the object: `this  Acc  {total = 5}`, and
  `this.total` reads the copy's member. `[this]` shows the pointer, as before.
- The lambda's own local of a capture's name hides the capture from its declaration to the end of
  its block, as C++ reads it. Above it in the block, and after the block, the name reads the
  capture. So does a stop whose code isn't the block's, as after a macro's `if (int x = 9) …`.
  Where Peeky can't tell, as in the block on the declaration's own line, the row says why.
- No capture can be changed inside the lambda, by value or by reference. The lambda's object
  holds it, which the frame reaches through a pointer. Its rows say `[not settable: capture]`, and
  a `--set` is refused before the program runs, saying the same.
- A capture by value is the lambda's own copy, a member of the lambda's object: GCC 13 names it
  `__tariff`, clang `tariff`. Where a local of the function around the lambda is that object,
  `describe` lists its integer fields as settable there: `main: price.__tariff.cents_per_kwh`.
- Change that copy at a stop in that function once the lambda is made:
  `--set-at 61@4 price.__tariff.cents_per_kwh=45`. It reaches the lambda where the code calls
  `price` itself, not a copy of it. A GCC build's refusal names this route.
- For a capture by reference, change what it refers to where that is owned. Or change the outer
  variable before the lambda is made, or edit the source.
- Peeky reads no capture in an optimized build, where a `mutable` lambda may keep one in a
  register. Nor at a one-line lambda's first stop, before it stores `__closure`. There a capture
  is `not found`, and the row says why: ``not found: 'bonus' is a capture of this lambda, which
  GCC passes through `__closure`, and Peeky can't read it here: the stop is at the lambda's start
  …``.

### Read a C++ string that holds a NUL byte

LLDB shows a libc++ (macOS) `std::string` whole, with a NUL as `\0`: `"a\0b"`. A libstdc++ (Linux)
string is different:

- LLDB's summary of it is a C string, which ends at the first NUL.
- On clang's Linux builds, a value LLDB names `std::string` gets
  `error: summary string parsing error`. GCC's builds describe `std::string` whole, so there the
  summary works.

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
  information gives N. The bytes after the NUL stay as they were. The change shows them as the
  debugger does, after `\0`: `set name: "apple :4" -> "apple\0:4"`. A row of `name` then reads
  `"apple" (then a NUL and 10 bytes not shown)` ([details](#characters)).
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

In a GCC build, LLDB reads a string's own buffer as `char[0]` and lists none of its characters.
So with lldb-dap 20, Peeky writes the text there with LLDB's `memory write` command. Any text
that fits works, not only printable ASCII. Peeky then reads the bytes and the size back. With
lldb-dap 18, which reads no memory, the change is still refused: use lldb-dap 20 or later.

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

A comparator that is a lambda shows as the compiler names it. LLDB names it by a typedef no code
writes: `type` with libc++, `key_compare` with libstdc++. So a root local's row reads its type
from the build's debug information instead:

| Build | `type` | `debugger_type` |
| --- | --- | --- |
| clang | `std::map<int, long, (lambda at hours.cpp:6:18)>` | `std::map<int, long, type>` |
| GCC | `std::map<int, long, main()::<lambda(int, int)>>` | `std::map<int, long, key_compare>` |

A field's type, and the lambda's own local, keep LLDB's name. A candidate's lambda is named by
the candidate's lines.

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

A pointer can't be set, but the statement that moves it can be skipped: flip its guard, then
restore it with `@before` ([COOKBOOK 2.8](../COOKBOOK.md#skip-a-statement-restore-with-before)).
`--set-at 15@all trend=0 --set-at 18@all trend=@before` keeps `turn = p;` from running on a flat.
In C and C++, Peeky writes back only an integer, whose type the debug information gives, and
only to the declaration its change wrote: a shadowing `x` or another block's `y` is refused. A
`continue;` has code of its own in a clang build, so it can be the restore line.

#### What counts as behind a pointer

Every local whose storage the debug information reaches through a pointer:

- a structured binding of a reference: `dropped` in `auto &[at, dropped] = *owner;`. It has no
  storage of its own, and is never marked `[settable]`. Its fields show
  `[not settable: reference]`;
- in a GCC build, a structured binding of a pair or tuple: `t` in `auto [t, u] = p;`. GCC holds it
  as a reference to its element;
- a named return value, whose object is the caller's;
- in C on macOS, a `__block` variable;
- the elements of GCC's variable-length arrays.

Their refusal says
`` `dropped` is reached through a pointer or reference …. Change the value where it is owned: Peeky changes only values the frame holds itself ``.

These change as usual, because the frame holds them itself: a by-value structured binding of a
struct (`auto [a, b] = pt;`), and a by-value parameter that the ABI passes through a hidden
pointer. That is a struct over 16 bytes on arm64, or any class with a destructor: the call's own
copy.

A by-value binding of a bitfield, `hi` in `auto [lo, hi] = bits;`, is one exception. GCC gives it
no location and a 1-byte type named `__unknown__`, so no stop can read or change it. Its row and a
refused change say so, and say to print it from the program: `not readable: the build gives it no
location and no type of its own …`.

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
