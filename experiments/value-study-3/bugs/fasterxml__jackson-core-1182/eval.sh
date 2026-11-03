#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test classes
# they change, offline (adapted from Multi-SWE-bench's run for this repo: `mvn clean test`,
# limited to the hidden test files' classes).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=d14acac9536939886e432d7145670a9210f54699
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/core/read/NumberParsingTest.java src/test/java/com/fasterxml/jackson/core/util/TestDelegates.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.core.read.NumberParsingTest,com.fasterxml.jackson.core.util.TestDelegates -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
