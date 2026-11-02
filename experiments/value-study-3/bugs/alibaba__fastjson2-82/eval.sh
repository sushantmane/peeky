#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for fastjson2: the core module only, offline, and only the tests in
# the package of the hidden test file).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=3aed80608b36c310d0fe5f240f49d670b3638698
git status
git -c core.fileMode=false diff $BASE
rm -f core/src/test/java/com/alibaba/fastjson2/issues/Issue81.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./mvnw -B -o -V --no-transfer-progress -pl core clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
  -Dsurefire.failIfNoSpecifiedTests=false '-Dtest=com/alibaba/fastjson2/issues/*' | cat
echo '>>>>> End Test Output'
