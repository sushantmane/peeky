#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for dubbo: the dubbo-common module only, offline, and only the
# hidden test class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=e84cdc217a93f4628415ea0a7d8a9d0090e2c940
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- dubbo-common/src/test/java/org/apache/dubbo/common/utils/ReflectUtilsTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o -V --no-transfer-progress -pl dubbo-common clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
  -Dtest=ReflectUtilsTest | cat
echo '>>>>> End Test Output'
