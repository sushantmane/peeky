std::unordered_map in struct, serialization
- What is the issue you have?
		
Cannot build deserialization code for a std::unordered_map. Following the example in the README at https://github.com/nlohmann/json#arbitrary-types-conversions


- Please describe the steps to reproduce the issue. Can you provide a small but working code example?


      #include <unordered_map>
      #include <vector>
      #include <string>
      #include <nlohmann/json.hpp>

      struct myStruct{
        std::string structName;
      }
				
      struct myStructCollection {
        std::unordered_map<int, myStruct> myStructs;
        myStructCollection(std::unordered_map<int, myStruct> myStructs) : myStructs (myStructs){}
      };

      void from_json(const json& j, myStructCollection& p) {
        p.myStructs = j.at("myStructs").get<std::unordered_map<int, myStruct>>();
      }

- What is the expected behavior?

Builds. According to https://github.com/nlohmann/json#conversion-from-stl-containers, std::unordered_map is supported.

- And what is the actual behavior instead?

Error at line 269 of "utility": 

      pair& operator=(pair&& _Right)
        _NOEXCEPT_COND(is_nothrow_move_assignable_v<_Ty1>
        && is_nothrow_move_assignable_v<_Ty2>)
      {	// assign from moved pair
        first = _STD forward<_Ty1>(_Right.first); // ERROR ON THIS LINE!!!!
        second = _STD forward<_Ty2>(_Right.second);
        return (*this);
      }

Error	 C2166	"l-value specifies const object	"


- Which compiler and operating system are you using? Is it a [supported compiler](https://github.com/nlohmann/json#supported-compilers)?

MSVC compiler 19.14.26430, Windows 10, Visual Studio 2017 (version 15.7.2), v141 toolset.

- Did you use a released version of the library or the version from the `develop` branch?

Used vcpkg to install.

- If you experience a compilation error: can you [compile and run the unit tests](https://github.com/nlohmann/json#execute-unit-tests)?