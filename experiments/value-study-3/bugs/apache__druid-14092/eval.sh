#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from the
# SWE-bench Multilingual eval_script for this instance: Maven instead of mvnd, offline, and each
# test command runs even if an earlier one fails).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=4fffee17761286996a8c43154b72db4bcaafb16d
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- server/src/test/java/org/apache/druid/discovery/DruidLeaderClientTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
(
mvn test -B -o -pl server -Dtest=org.apache.druid.discovery.DruidLeaderClientTest#test503ResponseFromServerAndCacheRefresh
mvn test -B -o -pl server -Dtest=org.apache.druid.discovery.DruidLeaderClientTest#testServerFailureAndRedirect
) | cat
echo '>>>>> End Test Output'
