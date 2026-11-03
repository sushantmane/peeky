FROM peeky-study/base:1
# mockito/mockito at the instance's base commit, built with its Gradle wrapper (8.4) on JDK 21
# (Multi-SWE-bench's recipe for mockito: Zulu 21, with UTF-8 and Asia/Shanghai in
# JAVA_TOOL_OPTIONS; here the base's OpenJDK 21, with TZ and locale).
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-21-openjdk-arm64/bin:$PATH
ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/mockito/mockito.git /testbed && \
    cd /testbed && git checkout -q -B main bfee15dda7acc41ef497d8f8a44c74dacce2933a && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = bfee15dda7acc41ef497d8f8a44c74dacce2933a
WORKDIR /testbed
# With network: fetch the wrapper's Gradle, the plugins and every project's dependencies, and
# compile all main and test sources. The java21 project has no tests at base, so a throwaway
# JUnit 4 + AssertJ test (removed again) makes its test task resolve and run once.
RUN ./gradlew --no-daemon testClasses > /root/build.log 2>&1 || (tail -100 /root/build.log; false)
RUN mkdir -p subprojects/java21/src/test/java/org/mockito/java21 && \
    printf '%s\n' 'package org.mockito.java21;' \
      'import static org.assertj.core.api.Assertions.assertThat;' \
      'import org.junit.Test;' \
      'public class WarmupTest { @Test public void runs() { assertThat(1).isEqualTo(1); } }' \
      > subprojects/java21/src/test/java/org/mockito/java21/WarmupTest.java && \
    (./gradlew --no-daemon --no-build-cache :java21:test > /root/java21.log 2>&1 || (tail -100 /root/java21.log; false)) && \
    rm subprojects/java21/src/test/java/org/mockito/java21/WarmupTest.java && \
    rm -rf subprojects/java21/build && \
    grep -q '> Task :java21:test$' /root/java21.log
# Gradle runs offline from here on (no network once built).
RUN mkdir -p /root/.gradle/init.d && \
    printf 'gradle.startParameter.offline = true\n' > /root/.gradle/init.d/offline.gradle
# Check that the tests run with no network (an existing test class of the core project), then warm
# the grading command once at base (no java21 test sources: NO-SOURCE).
RUN --network=none ./gradlew --no-daemon --offline --no-build-cache :test \
      --tests org.mockito.MockitoTest > /root/offline.log 2>&1 || (tail -100 /root/offline.log; false)
RUN --network=none ./gradlew --no-daemon --offline --no-build-cache :java21:test > /root/warm.log 2>&1 || (tail -100 /root/warm.log; false)
RUN grep -n '> Task :java21:test' /root/warm.log && test -z "$(git status --porcelain)"
