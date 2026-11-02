#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test classes
# they hold, offline (adapted from Multi-SWE-bench's run for this repo: `mvn clean test`, limited
# to the hidden test files' classes).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=8146fa8191176b5d463fb0d445bc313d777a1483
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/databind/jsontype/deftyping/TestDefaultForEnums.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.databind.jsontype.deftyping.TestDefaultForEnums -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
