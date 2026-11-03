FROM peeky-study/base:1
# apache/druid at the instance's base commit, built with Maven on JDK 11 (SWE-bench spec for
# version 14092).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/apache/druid.git /testbed && \
    cd /testbed && git checkout -q -B main 4fffee17761286996a8c43154b72db4bcaafb16d && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 4fffee17761286996a8c43154b72db4bcaafb16d
WORKDIR /testbed
# Fetch dependencies and build (as SWE-bench's install step), then fetch the remaining plugins
# and test providers by running the test command of the existing test once with network.
RUN mvn -B clean install -pl processing,cloud/aws-common,cloud/gcp-common -DskipTests -am > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (mvn -B dependency:go-offline -pl server > /root/offline.log 2>&1 || true) && \
    (mvn test -B -pl server -Dtest=org.apache.druid.discovery.DruidLeaderClientTest#testServerFailureAndRedirect ; true) > /root/fetch.log 2>&1; \
    grep -c 'BUILD SUCCESS' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading command once at base, with no network (the hidden test method doesn't exist
# yet, so only the existing one runs).
RUN --network=none (mvn test -B -o -pl server -Dtest=org.apache.druid.discovery.DruidLeaderClientTest#testServerFailureAndRedirect ; true) > /root/warm.log 2>&1; \
    grep 'Tests run:' /root/warm.log && grep -q 'BUILD SUCCESS' /root/warm.log && ! grep -q 'offline mode' /root/warm.log && \
    test -z "$(git status --porcelain)"
