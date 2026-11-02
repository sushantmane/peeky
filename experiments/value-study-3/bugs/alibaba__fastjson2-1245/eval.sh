#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from
# Multi-SWE-bench's run script for fastjson2: the core module only, offline, and only the tests in
# the package of the hidden test file).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=6648b96c0162c222467eb44bac30a9d59392c7ff
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- core/src/test/java/com/alibaba/fastjson2/dubbo/DubboTest6.java core/src/test/java/org/apache/dubbo/springboot/demo/ParamsDTO.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./mvnw -B -o -V --no-transfer-progress -pl core clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
  -Dsurefire.failIfNoSpecifiedTests=false '-Dtest=com/alibaba/fastjson2/dubbo/*' | cat
echo '>>>>> End Test Output'
