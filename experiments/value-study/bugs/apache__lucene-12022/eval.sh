#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from the SWE-bench
# Multilingual eval_script for this instance; offline).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=11f2bc205652f1f2b2f61ba286cbfd2d77fd2d1b
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- lucene/core/src/test/org/apache/lucene/document/TestLatLonShape.java
git apply -v /grade/test.patch
git checkout $BASE -- gradle/testing/defaults-tests.gradle
# SWE-bench's pre-install step for lucene: print every test's result (testLogging).
sed -i '
/testLogging {/,/}/{
  /testLogging {/r /dev/stdin
  d
}
' gradle/testing/defaults-tests.gradle << 'EOF'
testLogging {
  showStandardStreams = true
  // set options for log level LIFECYCLE
  events TestLogEvent.FAILED,
         TestLogEvent.PASSED,
         TestLogEvent.SKIPPED,
         TestLogEvent.STANDARD_OUT
  exceptionFormat TestExceptionFormat.FULL
  showExceptions true
  showCauses true
  showStackTraces true

  // set options for log level DEBUG and INFO
  debug {
      events TestLogEvent.STARTED,
             TestLogEvent.FAILED,
             TestLogEvent.PASSED,
             TestLogEvent.SKIPPED,
             TestLogEvent.STANDARD_ERROR,
             TestLogEvent.STANDARD_OUT
      exceptionFormat TestExceptionFormat.FULL
  }
  info.events = debug.events
  info.exceptionFormat = debug.exceptionFormat

  afterSuite { desc, result ->
      if (!desc.parent) { // will match the outermost suite
          def output = "Results: ${result.resultType} (${result.testCount} tests, ${result.successfulTestCount} passed, ${result.failedTestCount} failed, ${result.skippedTestCount} skipped)"
          def startItem = '|  ', endItem = '  |'
          def repeatLength = startItem.length() + output.length() + endItem.length()
          println('\n' + ('-' * repeatLength) + '\n' + startItem + output + endItem + '\n' + ('-' * repeatLength))
      }
  }
}
EOF

echo '>>>>> Start Test Output'
(./gradlew --offline test --tests org.apache.lucene.document.TestLatLonShape) | cat
echo '>>>>> End Test Output'
