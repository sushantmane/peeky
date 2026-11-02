Assertion failure when inserting into arrays with JSON_DIAGNOSTICS set
#### What is the issue you have?
The assertion checking parent correctness in `assert_invariants()` fails for arrays when using `JSON_DIAGNOSTICS`. It seems parents of objects being inserted into arrays are not being updated.

#### Please describe the steps to reproduce the issue.
To reproduce compile and run the following:
```c++
#define JSON_DIAGNOSTICS 1

#include <nlohmann/json.hpp>

using json = nlohmann::json;

int main()
{
    json j_arr = json::array();
    j_arr.push_back(json::object());
    j_arr.push_back(json::object());
    json j_obj = json::object();
    j_obj["key"] = j_arr;
}
```

This produces the following assertion failure:
```
assert_failure: single_include/nlohmann/json.hpp:18032: void nlohmann::basic_json<ObjectType, ArrayType, StringType, BooleanType, NumberIntegerType, NumberUnsignedType, NumberFloatType, AllocatorType, JSONSerializer, BinaryType>::assert_invariant(bool) const [with ObjectType = std::map; ArrayType = std::vector; StringType = std::__cxx11::basic_string<char>; BooleanType = bool; NumberIntegerType = long int; NumberUnsignedType = long unsigned int; NumberFloatType = double; AllocatorType = std::allocator; JSONSerializer = nlohmann::adl_serializer; BinaryType = std::vector<unsigned char>]: Assertion `!check_parents || !is_structured() || std::all_of(begin(), end(), [this](const basic_json & j) { return j.m_parent == this; })' failed.
Aborted (core dumped)
```

Note that this does not happen when there is only one `push_back`, i.e. only a single element in the array.

This seems to have been the case since `JSON_DIAGNOSTICS` was introduced in 176d8e261.

#### Can you provide a small but working code example?
Disabling `JSON_DIAGNOSTICS` or commenting out the `push_back` lines will make this code run.

#### What is the expected behavior?
Assertion failure due to incorrect parents.

#### And what is the actual behavior instead?
No assert should fail and parents should be correct.

#### Which compiler and operating system are you using?

- Compiler: GCC 11.1.0 and Clang 12.0.0
- Operating system: Linux

#### Which version of the library did you use?

<!-- Please add an `x` to the respective line. -->

- [ ] latest release version 3.9.1
- [ ] other release - please state the version: ___
- [x] the `develop` branch

#### If you experience a compilation error: can you [compile and run the unit tests](https://github.com/nlohmann/json#execute-unit-tests)?

- [x] yes
- [ ] no - please copy/paste the error message below