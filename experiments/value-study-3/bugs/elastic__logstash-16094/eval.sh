#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for logstash: offline, logstash-core's javaTests task only, limited to the hidden test
# class; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=18583787b3cc1095a002f4a8e1f4d9436e712c54
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- logstash-core/src/test/java/org/logstash/log/CustomLogEventTests.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline :logstash-core:javaTests --tests org.logstash.log.CustomLogEventTests | cat
echo '>>>>> End Test Output'
