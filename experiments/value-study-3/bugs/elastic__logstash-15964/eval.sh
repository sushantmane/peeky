#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for logstash: offline, logstash-core's javaTests task only, limited to the hidden test
# class; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=ff37e1e0d3d19b605951c94263b72c5e5a053112
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- logstash-core/src/test/java/org/logstash/common/io/DeadLetterQueueWriterAgeRetentionTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline :logstash-core:javaTests --tests org.logstash.common.io.DeadLetterQueueWriterAgeRetentionTest | cat
echo '>>>>> End Test Output'
