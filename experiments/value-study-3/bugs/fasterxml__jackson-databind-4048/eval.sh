#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test class
# they change, offline (adapted from Multi-SWE-bench's run for this repo: `mvn clean test`,
# limited to the hidden test file's class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=5e94cb1b29a5948737d86f5fe7eaeda318b74910
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/databind/introspect/Transient3948Test.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.databind.introspect.Transient3948Test -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
