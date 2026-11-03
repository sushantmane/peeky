#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test class
# they change, offline (adapted from Multi-SWE-bench's run for this repo: `mvn clean test`,
# limited to the hidden test file's class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=315ac5ad2ad5228129a3df50623570d9bc2d7045
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/core/json/JsonFactoryTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.core.json.JsonFactoryTest -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
