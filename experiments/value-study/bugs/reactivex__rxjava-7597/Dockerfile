FROM peeky-study/base:1
# RxJava 3 at the instance's base commit, built with its Gradle wrapper (7.6.2) on JDK 11
# (SWE-bench spec for version 7597).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/ReactiveX/RxJava.git /testbed && \
    cd /testbed && git checkout -q -B main 13a8d83656940dce0c18e8e0a2a17ac157dd1ddb && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 13a8d83656940dce0c18e8e0a2a17ac157dd1ddb
WORKDIR /testbed
# Gradle runs offline in the image (no network once built).
RUN mkdir -p /root/.gradle/init.d && \
    printf 'gradle.startParameter.offline = true\n' > /root/.gradle/init.d/offline.gradle
# Fetch the wrapper's Gradle and the build's dependencies (including the test task's, e.g. the
# jacoco agent), compile main and test sources.
RUN mv /root/.gradle/init.d/offline.gradle /root/offline.gradle && \
    ./gradlew --no-daemon -q compileJava compileTestJava && \
    (./gradlew --no-daemon -q test --tests io.reactivex.rxjava3.internal.operators.observable.ObservableSwitchTest || true) && \
    mv /root/offline.gradle /root/.gradle/init.d/offline.gradle
# Warm the grading command once at base (with SWE-bench's test logging), then restore build.gradle.
RUN <<'EOS'
set -e
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

./gradlew --no-daemon --offline test --tests io.reactivex.rxjava3.internal.operators.observable.ObservableSwitchTest > /root/warm.log 2>&1 || true
grep -c ' PASSED$' /root/warm.log
git checkout -- build.gradle
test -z "$(git status --porcelain)"
EOS
