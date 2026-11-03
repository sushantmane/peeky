#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test class
# they move and change, offline (adapted from Multi-SWE-bench's run for this repo:
# `mvn clean test`, limited to the hidden test file's class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=cf7c15a3ddf8fa6df5c8961cb57e97e12ee9728a
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/failing/JsonCreatorModeForEnum3566.java
# The file the hidden test patch creates (it moves the test out of failing/).
rm -f src/test/java/com/fasterxml/jackson/databind/deser/creators/JsonCreatorModeForEnum3566.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.databind.deser.creators.JsonCreatorModeForEnum3566 -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
