FROM peeky-study/base:1
# google/gson at the instance's base commit, built with Maven on JDK 11 (Multi-SWE-bench's recipe
# for gson: Ubuntu 22.04's OpenJDK 11 and Maven; here the base's OpenJDK 11 and Maven).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/google/gson.git /testbed && \
    cd /testbed && git checkout -q -B main 6d2557d5d1a8ac498f2bcee20e5053c93b33ecce && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 6d2557d5d1a8ac498f2bcee20e5053c93b33ecce
WORKDIR /testbed
# Fetch the dependencies and build the gson module (the one the hidden tests are in) and its
# parent, then fetch the clean plugin and the test providers by running the grading command once.
RUN mvn -B -V --no-transfer-progress -pl gson -am install -DskipTests > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (mvn -B --no-transfer-progress -pl gson dependency:go-offline > /root/offline.log 2>&1 || true) && \
    (mvn -B --no-transfer-progress -pl gson clean test -Dmaven.test.skip=false -DfailIfNoTests=false \
       -Dtest=ReadersWritersTest || true) > /root/fetch.log 2>&1; \
    grep -c 'Tests run:' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading command once at base, with no network.
RUN --network=none (mvn -B -o --no-transfer-progress -pl gson clean test -Dmaven.test.skip=false -DfailIfNoTests=false \
       -Dtest=ReadersWritersTest || true) > /root/warm.log 2>&1; \
    grep 'Tests run:' /root/warm.log && grep -q 'BUILD SUCCESS' /root/warm.log && \
    test -z "$(git status --porcelain)"
