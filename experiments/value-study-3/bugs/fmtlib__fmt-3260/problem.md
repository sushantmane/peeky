formatting chrono with padding
The grammar in the documentation 

```
   replacement_field: "{" [`arg_id`] [":" (`format_spec` | `chrono_format_spec`)] "}"

   chrono_format_spec: [[`fill`]`align`][`width`]["." `precision`][`chrono_specs`]
   chrono_specs: [`chrono_specs`] `conversion_spec` | `chrono_specs` `literal_char`
   conversion_spec: "%" [`modifier`] `chrono_type`
```

suggests to me that `{:>30%H}` is valid, but this is not what fmt-9.1.0 and/or master @ b90895412f46e18e5b17efdea2c8f79e7d7504b3 outputs.

Input:

```c
#include <chrono>
#include <fmt/core.h>
#include <fmt/chrono.h>
int main()
{
        fmt::print("{:>30d}\n", 30);
        fmt::print("{:>30%H:%M}\n", std::chrono::system_clock::now());
}
```

Output:

```
                            30
>3023:48
```

Expected:

```
                            30
                         23:48
```