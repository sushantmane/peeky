Exception is treated as passing when used CHECKED_ELSE before in xml reporter
**Describe the bug**
After CHECKED_ELSE(true) {} is being run, any exception being thrown in the tests are suppressed and treated as passing when using xml reporter. Using console reporter reports failure.

After CHECKED_ELSE(false) {} is being run, any exception being thrown are suppressed in both console and xml reporter.

**Expected behavior**
Both the console reporter and xml reporter should behave exactly the same, that the exception is reported and test counted as failure in both cases.

**Reproduction steps**
Steps to reproduce the bug.
<!-- Usually this means a small and self-contained piece of code that uses Catch and specifying compiler flags if relevant. -->
```
#include <catch2/catch_test_macros.hpp>
#include <exception>

TEST_CASE("Testing") {
    CHECKED_ELSE(true) {}
    throw std::runtime_error("it is an error");
}
```
Running it with 
./test -r xml
```
<?xml version="1.0" encoding="UTF-8"?>
<Catch2TestRun name="test" rng-seed="3025144594" xml-format-version="2" catch
  <TestCase name="Testing" filename="/home/ross/workspace/catch2-xml/test.cpp
    <OverallResult success="true" skips="0"/>
  </TestCase>
  <OverallResults successes="1" failures="0" expectedFailures="0" skips="0"/>
  <OverallResultsCases successes="1" failures="0" expectedFailures="0" skips=
</Catch2TestRun>
```
And running it directly with
./test
```
...
/home/ross/workspace/catch2-xml/test.cpp:4: FAILED:
  {Unknown expression after the reported line}
due to unexpected exception with message:
  it is an error

===============================================================================
test cases: 1 | 1 failed
assertions: 2 | 1 passed | 1 failed
```
And if the argument inside CHECKED_ELSE is false, exception is suppressed for both console and xml reporter. It also looks like a bug.

```
#include <catch2/catch_test_macros.hpp>
#include <exception>

TEST_CASE("Testing") {
    CHECKED_ELSE(false) {}
    throw std::runtime_error("it is an error");
}
```
Running
./test -r xml
```
<?xml version="1.0" encoding="UTF-8"?>
<Catch2TestRun name="test" rng-seed="3270933764" xml-format-version="2" catch2-version="3.3.2">
  <TestCase name="Testing" filename="/home/ross/workspace/catch2-xml/test.cpp" line="4">
    <OverallResult success="true" skips="0"/>
  </TestCase>
  <OverallResults successes="0" failures="0" expectedFailures="0" skips="0"/>
  <OverallResultsCases successes="1" failures="0" expectedFailures="0" skips="0"/>
</Catch2TestRun>
```
Running 
./test 
```
Randomness seeded to: 1751265161
===============================================================================
test cases: 1 | 1 passed
assertions: - none -
```
**Platform information:**
<!-- Fill in any extra information that might be important for your issue. -->
 - OS: **Ubuntu jammy**
 - Compiler+version: **clang++-15**
 - Catch version: **v3.3.2**


**Additional context**
Add any other context about the problem here.