#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, then rebuild and run the test class
# they move and change, offline (adapted from Multi-SWE-bench's run for this repo:
# `mvn clean test`, limited to the hidden test file's class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=a18b8cd98e94660dcac19bd2cd11f376705d7745
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/com/fasterxml/jackson/dataformat/xml/failing/JsonAppend578Test.java
# The file the hidden test patch creates (it moves the test out of failing/).
rm -f src/test/java/com/fasterxml/jackson/dataformat/xml/ser/JsonAppend578Test.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o clean test -Dtest=com.fasterxml.jackson.dataformat.xml.ser.JsonAppend578Test -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false | cat
echo '>>>>> End Test Output'
