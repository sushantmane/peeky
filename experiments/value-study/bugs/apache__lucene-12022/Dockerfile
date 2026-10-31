FROM peeky-study/base:1
# Lucene at the instance's base commit, built with its Gradle wrapper on JDK 17 (SWE-bench spec
# for version 12022).
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-17-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/apache/lucene.git /testbed && \
    cd /testbed && git checkout -q -B main 11f2bc205652f1f2b2f61ba286cbfd2d77fd2d1b && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 11f2bc205652f1f2b2f61ba286cbfd2d77fd2d1b
WORKDIR /testbed
# First run: the wrapper downloads Gradle and the build writes gradle.properties (ignored by git);
# size it for the study's containers (4 CPUs, 6 GB).
RUN (./gradlew --no-daemon help > /root/first.log 2>&1 || true) && test -f gradle.properties && \
    sed -i -e 's/^org.gradle.workers.max=.*/org.gradle.workers.max=4/' -e 's/^tests.jvms=.*/tests.jvms=4/' gradle.properties && \
    grep -E '^(org.gradle.workers.max|tests.jvms|org.gradle.jvmargs)' gradle.properties
# Fetch dependencies and build: run the relevant test class once with network (failures fine).
# Gradle provisions the JDK toolchains the build asks for (e.g. 19 for core's main19) here.
RUN (./gradlew --no-daemon test --tests org.apache.lucene.document.TestLatLonShape > /root/fetch.log 2>&1 || true) && tail -5 /root/fetch.log
# Gradle runs offline from here on (no network once built).
RUN mkdir -p /root/.gradle/init.d && \
    printf 'gradle.startParameter.offline = true\n' > /root/.gradle/init.d/offline.gradle
# Warm the grading command once at base (with SWE-bench's test logging), then restore the file.
RUN <<'EOS'
set -e
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

./gradlew --no-daemon --offline test --tests org.apache.lucene.document.TestLatLonShape > /root/warm.log 2>&1 || true
grep -c ' PASSED$' /root/warm.log
git checkout -- gradle/testing/defaults-tests.gradle
test -z "$(git status --porcelain)"
EOS
