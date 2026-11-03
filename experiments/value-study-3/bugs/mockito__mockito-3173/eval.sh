#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for mockito: offline and without Gradle's build cache, the java21 project's test task
# only, limited to the hidden test class; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=bfee15dda7acc41ef497d8f8a44c74dacce2933a
git status
git -c core.fileMode=false diff $BASE
rm -f subprojects/java21/src/test/java/org/mockito/java21/RecordTest.java
git checkout $BASE -- subprojects/java21/src/test/java/org/mockito/java21/.gitkeep
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline --no-build-cache :java21:test --tests org.mockito.java21.RecordTest | cat
echo '>>>>> End Test Output'
