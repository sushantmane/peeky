FROM peeky-study/base:1
# apache/druid at the instance's base commit, built with Maven on JDK 11 (SWE-bench spec for
# version 15402).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/apache/druid.git /testbed && \
    cd /testbed && git checkout -q -B main 1df53d6eb30a019fff69a5ff7533ebbd998a438d && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 1df53d6eb30a019fff69a5ff7533ebbd998a438d
WORKDIR /testbed
# Fetch dependencies and build (as SWE-bench's install step), then fetch the remaining plugins
# and test providers by running the test commands once with network (failures fine).
RUN mvn -B clean install -pl processing -DskipTests -am > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (mvn -B dependency:go-offline -pl processing > /root/offline.log 2>&1 || true) && \
    (mvn test -B -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testCacheStrategy ; mvn test -B -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testResultLevelCacheKeyWithSubTotalsSpec ; mvn test -B -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testMultiColumnCacheStrategy ; true) > /root/fetch.log 2>&1; \
    grep -c 'BUILD SUCCESS' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading commands once at base, offline.
RUN (mvn -B clean install -pl processing -DskipTests -am -o && mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testCacheStrategy ; mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testResultLevelCacheKeyWithSubTotalsSpec ; mvn test -B -o -pl processing -Dtest=org.apache.druid.query.groupby.GroupByQueryQueryToolChestTest#testMultiColumnCacheStrategy ; true) > /root/warm.log 2>&1; \
    grep -c 'BUILD SUCCESS' /root/warm.log && ! grep -q 'offline mode' /root/warm.log && \
    test -z "$(git status --porcelain)"
