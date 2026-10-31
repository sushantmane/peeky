#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from the
# SWE-bench Multilingual eval_script for this instance: Maven instead of mvnd, offline, and each
# test command runs even if an earlier one fails).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=1df53d6eb30a019fff69a5ff7533ebbd998a438d
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- processing/src/test/java/org/apache/druid/query/groupby/GroupByQueryQueryToolChestTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(
mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testCacheStrategy
mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testResultLevelCacheKeyWithSubTotalsSpec
mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testMultiColumnCacheStrategy
) | cat
echo '>>>>> End Test Output'
