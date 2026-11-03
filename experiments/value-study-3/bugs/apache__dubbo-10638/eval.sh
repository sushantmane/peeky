#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for dubbo: the dubbo-common module only, offline, and only the
# hidden test class).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=ddd1786578438c68f1f6214bcab600a299245d7d
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- dubbo-common/src/test/java/org/apache/dubbo/common/utils/PojoUtilsTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
mvn -B -o -V --no-transfer-progress -pl dubbo-common clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
  -Dtest=PojoUtilsTest | cat
echo '>>>>> End Test Output'
