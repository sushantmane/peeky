#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for fastjson2: the core module only, offline, and only the tests in
# the package of the hidden test file).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=27ca2b45c33cd362fa35613416f5d62ff9567921
git status
git -c core.fileMode=false diff $BASE
rm -f core/src/test/java/com/alibaba/fastjson2/issues_2200/Issue2269.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./mvnw -B -o -V --no-transfer-progress -pl core clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
  -Dsurefire.failIfNoSpecifiedTests=false '-Dtest=com/alibaba/fastjson2/issues_2200/*' | cat
echo '>>>>> End Test Output'
