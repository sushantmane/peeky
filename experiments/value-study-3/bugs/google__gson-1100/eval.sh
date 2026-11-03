#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from the
# SWE-bench Multilingual eval_script for this instance: Maven instead of mvnd, offline, and each
# test command runs even if an earlier one fails).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=f0aa1118e9ef66ed324f9a63cdfb551cb4e9eca5
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- gson/src/test/java/com/google/gson/DefaultDateTypeAdapterTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(
mvn test -B -o -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testNullValue
mvn test -B -o -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testDatePattern
mvn test -B -o -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testInvalidDatePattern
) | cat
echo '>>>>> End Test Output'
