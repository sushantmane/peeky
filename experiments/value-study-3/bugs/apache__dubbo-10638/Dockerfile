FROM peeky-study/base:1
# apache/dubbo at the instance's base commit, built with Maven on JDK 17 (Multi-SWE-bench's recipe
# for dubbo: Ubuntu 22.04's OpenJDK 17 and Maven; here the base's OpenJDK 17 and Maven).
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-17-openjdk-arm64/bin:$PATH
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
# The project's surefire (3.0.0-M5) forks its test JVM over TCP and resolves the container's own
# host name (InetAddress.getLocalHost), which a container with no network can't resolve from
# /etc/hosts: glibc's myhostname module resolves it.
RUN apt-get update -q && apt-get install -y -q --no-install-recommends libnss-myhostname > /dev/null && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i -E 's/^hosts:.*/hosts:          files myhostname dns/' /etc/nsswitch.conf && \
    grep -n '^hosts:' /etc/nsswitch.conf
RUN git clone -q https://github.com/apache/dubbo.git /testbed && \
    cd /testbed && git checkout -q -B main ddd1786578438c68f1f6214bcab600a299245d7d && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = ddd1786578438c68f1f6214bcab600a299245d7d
WORKDIR /testbed
# Fetch the dependencies and build dubbo-common (the module the hidden tests are in) and what it
# needs, then fetch the clean plugin and the test providers by running the grading command once.
RUN mvn -B -V --no-transfer-progress -pl dubbo-common -am install -DskipTests > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (mvn -B --no-transfer-progress -pl dubbo-common dependency:go-offline > /root/offline.log 2>&1 || true) && \
    (mvn -B --no-transfer-progress -pl dubbo-common clean test -Dsurefire.useFile=false -DfailIfNoTests=false \
       -Dtest=PojoUtilsTest || true) > /root/fetch.log 2>&1; \
    grep -c 'Tests run:' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading command once at base, with no network.
RUN --network=none (mvn -B -o --no-transfer-progress -pl dubbo-common clean test -Dsurefire.useFile=false \
       -DfailIfNoTests=false -Dtest=PojoUtilsTest || true) > /root/warm.log 2>&1; \
    grep 'Tests run:' /root/warm.log && grep -q 'BUILD SUCCESS' /root/warm.log && \
    test -z "$(git status --porcelain)"
