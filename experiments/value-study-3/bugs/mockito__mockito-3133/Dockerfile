FROM peeky-study/base:1
# mockito/mockito at the instance's base commit, built with its Gradle wrapper (8.2) on JDK 21
# (Multi-SWE-bench's recipe for mockito: Zulu 21, with UTF-8 and Asia/Shanghai in
# JAVA_TOOL_OPTIONS; here the base's OpenJDK 21, with TZ and locale).
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-21-openjdk-arm64/bin:$PATH
ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/mockito/mockito.git /testbed && \
    cd /testbed && git checkout -q -B main edc624371009ce981bbc11b7d125ff4e359cff7e && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = edc624371009ce981bbc11b7d125ff4e359cff7e
WORKDIR /testbed
# With network: fetch the wrapper's Gradle, the plugins and every project's dependencies, compile
# all main and test sources, and run the JUnit 5 extension's tests.
RUN ./gradlew --no-daemon testClasses :junit-jupiter:test > /root/build.log 2>&1 || (tail -100 /root/build.log; false)
# Gradle runs offline from here on (no network once built).
RUN mkdir -p /root/.gradle/init.d && \
    printf 'gradle.startParameter.offline = true\n' > /root/.gradle/init.d/offline.gradle
# Check that the tests run with no network: warm the grading command once at base, offline.
RUN --network=none ./gradlew --no-daemon --offline --no-build-cache :junit-jupiter:test \
      --tests org.mockitousage.JunitJupiterTest > /root/warm.log 2>&1 || (tail -100 /root/warm.log; false)
RUN grep -n '> Task :junit-jupiter:test' /root/warm.log && test -z "$(git status --porcelain)"
