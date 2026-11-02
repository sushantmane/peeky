#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for logstash: offline, logstash-core's javaTests task only, limited to the hidden test
# classes; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=2866bf9e3cacf294508154869ac5a17ed73ea027
git status
git -c core.fileMode=false diff $BASE
rm -f logstash-core/src/test/java/org/logstash/common/BufferedTokenizerExtTest.java \
  logstash-core/src/test/java/org/logstash/common/BufferedTokenizerExtWithDelimiterTest.java \
  logstash-core/src/test/java/org/logstash/common/BufferedTokenizerExtWithSizeLimitTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline :logstash-core:javaTests --tests 'org.logstash.common.BufferedTokenizerExt*' | cat
echo '>>>>> End Test Output'
