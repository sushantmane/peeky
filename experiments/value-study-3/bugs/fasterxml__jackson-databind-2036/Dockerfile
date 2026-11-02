FROM peeky-study/base:1
# FasterXML/jackson-databind at the instance's base commit, built and tested with Maven on JDK 11
# (Multi-SWE-bench's image uses JDK 8, which the base doesn't have; JDK 11 is the oldest there).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/FasterXML/jackson-databind.git /testbed && \
    cd /testbed && git checkout -q -B main bfeb1fa9dc4c889f8027b80abb2f77996efd9b70 && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = bfeb1fa9dc4c889f8027b80abb2f77996efd9b70
WORKDIR /testbed
# Fetch the plugins and dependencies, then build and run the whole test suite once with network,
# as Multi-SWE-bench's prepare step does (`mvn clean test`; failures fine).
RUN (mvn -B dependency:go-offline > /root/offline.log 2>&1 || true) && \
    (mvn -B clean test > /root/fulltest.log 2>&1 || true) && \
    grep -E '^\[(INFO|ERROR|WARNING)\] Tests run:|BUILD' /root/fulltest.log | tail -3
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading command once at base, offline (failures fine; it must resolve everything).
RUN (mvn -B -o clean test -Dtest=com.fasterxml.jackson.databind.jsontype.TestBaseTypeAsDefault -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false; true) > /root/warm.log 2>&1; \
    grep -E 'Tests run:|BUILD' /root/warm.log | tail -3; \
    grep -q 'BUILD' /root/warm.log && ! grep -qi 'offline mode\|Could not resolve' /root/warm.log && \
    test -z "$(git status --porcelain)"
