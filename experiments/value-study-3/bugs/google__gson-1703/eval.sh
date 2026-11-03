#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for gson: the gson module only, offline, and only the hidden test
# class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=6d2557d5d1a8ac498f2bcee20e5053c93b33ecce
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- gson/src/test/java/com/google/gson/functional/ReadersWritersTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o -V --no-transfer-progress -pl gson clean test -Dmaven.test.skip=false -DfailIfNoTests=false \
  -Dtest=ReadersWritersTest | cat
echo '>>>>> End Test Output'
