#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for mockito: offline and without Gradle's build cache, the junit-jupiter project's
# test task only, limited to the hidden test class; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=edc624371009ce981bbc11b7d125ff4e359cff7e
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- subprojects/junit-jupiter/src/test/java/org/mockitousage/JunitJupiterTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline --no-build-cache :junit-jupiter:test --tests org.mockitousage.JunitJupiterTest | cat
echo '>>>>> End Test Output'
