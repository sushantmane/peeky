#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from Multi-SWE-bench's
# run script for logstash: offline, the JVM options parser's test task only, limited to the hidden
# test class; Gradle rebuilds what changed).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=1cca6bcb2c769db169260a30531c4f2bd2f184c3
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- tools/jvm-options-parser/src/test/java/org/logstash/launchers/JvmOptionsParserTest.java
git apply -v /grade/test.patch
echo '>>>>> Start Test Output'
./gradlew --no-daemon --offline :jvm-options-parser:test --tests org.logstash.launchers.JvmOptionsParserTest | cat
echo '>>>>> End Test Output'
