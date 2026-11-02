#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test classes
# they hold, offline (adapted from Multi-SWE-bench's run for this repo: `mvn clean test`, limited
# to the hidden test files' classes).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=45e6fa63c412bb47e32693f7b0348b8bc7b246af
git status
git -c core.fileMode=false diff $BASE
# Files the hidden test patch creates.
rm -f src/test/java/com/fasterxml/jackson/databind/interop/OptionalJava8Fallbacks4082Test.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.databind.interop.OptionalJava8Fallbacks4082Test -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
