Compiler segfault on a match with generics
The following code causes the compiler to segfault during the "Functions" step:

```pony
actor Main
  new create(env: Env) =>
    Dealer.from[U8](0)
    // Dealer(0) // -- does not cause any segfault

class Dealer
  new from[U: (U8 | Dealer val)](u: U val) =>
    // Note: commenting out this match fixes the issue
    match u
    | let u': U8 => None
    | let u': Dealer val => None
    end

  new create(u: (U8 | Dealer val)) =>
    // this function is harmless
    match u
    | let u': U8 => None
    | let u': Dealer val => None
    end
```

This happens when a function with generics containing in a union the class itself tries to match a variable of that generic type. This function has to be covered in order for this segfault to occur.

Occurs on Arch Linux x86_64, `ponyc` 0.33.0 [release] (installed through the `community/ponyc` package), compiled with llvm 7.1.0 with GCC.