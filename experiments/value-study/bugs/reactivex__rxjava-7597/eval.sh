#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, run them (adapted from the SWE-bench
# Multilingual eval_script for this instance; offline).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=13a8d83656940dce0c18e8e0a2a17ac157dd1ddb
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- src/test/java/io/reactivex/rxjava3/internal/operators/flowable/FlowableSwitchTest.java src/test/java/io/reactivex/rxjava3/internal/operators/observable/ObservableSwitchTest.java
git apply -v /grade/test.patch
git checkout $BASE -- build.gradle
# SWE-bench's pre-install step for rxjava: print every test's result (testLogging in build.gradle).
sed -i '
/testLogging {/,/}/{
  /testLogging {/r /dev/stdin
  d
}
' build.gradle << 'EOF'
testLogging {
    outputs.upToDateWhen { false }
    showStandardStreams = true
    showStackTraces = true

    // Show output for all logging levels
    events = ['passed', 'skipped', 'failed', 'standardOut', 'standardError']

    // set options for log level LIFECYCLE
    events org.gradle.api.tasks.testing.logging.TestLogEvent.FAILED,
           org.gradle.api.tasks.testing.logging.TestLogEvent.PASSED,
           org.gradle.api.tasks.testing.logging.TestLogEvent.SKIPPED,
           org.gradle.api.tasks.testing.logging.TestLogEvent.STANDARD_OUT,
           org.gradle.api.tasks.testing.logging.TestLogEvent.STANDARD_ERROR
    exceptionFormat org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    showExceptions true
    showCauses true
    showStackTraces true

    // set options for log level DEBUG and INFO
    debug {
        events org.gradle.api.tasks.testing.logging.TestLogEvent.STARTED,
               org.gradle.api.tasks.testing.logging.TestLogEvent.FAILED,
               org.gradle.api.tasks.testing.logging.TestLogEvent.PASSED,
               org.gradle.api.tasks.testing.logging.TestLogEvent.SKIPPED,
               org.gradle.api.tasks.testing.logging.TestLogEvent.STANDARD_ERROR,
               org.gradle.api.tasks.testing.logging.TestLogEvent.STANDARD_OUT
        exceptionFormat org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
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
(./gradlew --no-daemon --offline test --tests io.reactivex.rxjava3.internal.operators.observable.ObservableSwitchTest) | cat
echo '>>>>> End Test Output'
