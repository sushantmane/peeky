Deserialization to std::array with non-default constructable types fails
Compilations fails when trying to convert an `nlohmann::json` object to an `std::array<T, N>`, where `T` does not have a default constructor.

#### What is the issue you have?

Compile error when trying to deserialize an `std::array` of non-default constructable types. However, deserialization from such types works fine if such a type is deserialized directly (i.e. not an `std::array` of it), or if e.g. an `std::vector` of such types is deserialized.

Hence it seems to me that this limitation is purely due to the way deserialization is implemented for `std::array`. Of course for e.g. `std::vector` this is not an issue anyway, since there you can constructor the `std::vector` first and then `emplace_back()` objects into it, which isn't possible for `std::array` in this case.

I haven't really looked into it, but I would think that using pack expansion (with most likely some extra helper structs thrown in) would allow creating the requested `std::array` in "one go" (i.e. no creating it beforehand and then assigning values to each location).

<!-- Provide a detailed introduction to the issue itself, and why you consider it to be a bug. -->
<!-- If possible, be specific and add stack traces, error messages, etc. Avoid vague terms like "crash" or "doesn't work". -->

#### Please describe the steps to reproduce the issue.

<!-- Provide a link to a live example, or an unambiguous set of steps to -->
<!-- reproduce this bug. Include code to reproduce, if relevant -->

Live example here: https://godbolt.org/z/qxP5aE

#### Can you provide a small but working code example?

```cpp
#include <nlohmann/json.hpp>

#include <array>
#include <vector>

struct NoDefaultConstructor {
    explicit NoDefaultConstructor (int x) : x(x) { }
    int x;
};

namespace nlohmann {
    template <>
    struct adl_serializer<NoDefaultConstructor> {
        static NoDefaultConstructor from_json (json const & j) {
            return NoDefaultConstructor(j.get<int>());
        }
    };
}

int main  () {
    { // OK
        auto a = nlohmann::json(3);
        auto b = a.get<NoDefaultConstructor>();
    }

    { // OK
        auto a = nlohmann::json({ 1, 2 });
        auto b = a.get<std::vector<NoDefaultConstructor>>();
    }

    { // Does not compile.
        auto a = nlohmann::json({ 1, 2 });
        auto b = a.get<std::array<NoDefaultConstructor, 2>>();
    }
}
```

#### What is the expected behavior?

Code compiles and returns the requested `std::array`.

#### And what is the actual behavior instead?

nlohmann::json 3.7.1: Compile error that the function `get<std::array<NoDefaultConstructor, 2>>` does not exist:

```
JsonTest.C:33:57: error: no matching function for call to ‘nlohmann::basic_json<>::get<std::array<NoDefaultConstructor, 2> >()’
   33 |     auto b = a.get<std::array<NoDefaultConstructor, 2>>();
```

nlohmann::json 3.6.0: Static assert goes off complaining that `get<T>()` doesn't allow `T` to be non-default constructable.

#### Which compiler and operating system are you using?

- Compiler: gcc 9.1.0 & clang 11.0.0
- Operating system: Some flavor of linux

#### Which version of the library did you use?

<!-- Please add an `x` to the respective line. -->

- [ ] latest release version 3.9.1
- [x] other release - please state the version: 3.6.0 (Godbolt, clang) and 3.7.1 (local, gcc)
- [ ] the `develop` branch

#### If you experience a compilation error: can you [compile and run the unit tests](https://github.com/nlohmann/json#execute-unit-tests)?

- [x] yes
- [ ] no - please copy/paste the error message below