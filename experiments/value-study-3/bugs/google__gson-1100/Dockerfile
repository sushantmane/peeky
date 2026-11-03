FROM peeky-study/base:1
# google/gson at the instance's base commit, built with Maven on JDK 11 (SWE-bench spec for
# version 1100).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
RUN git clone -q https://github.com/google/gson.git /testbed && \
    cd /testbed && git checkout -q -B main f0aa1118e9ef66ed324f9a63cdfb551cb4e9eca5 && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = f0aa1118e9ef66ed324f9a63cdfb551cb4e9eca5
WORKDIR /testbed
# Fetch dependencies and build (as SWE-bench's install step), then fetch the remaining plugins
# and test providers by running the test commands of the existing tests once with network.
RUN mvn -B clean install -pl gson -DskipTests -am > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (mvn -B dependency:go-offline -pl gson > /root/offline.log 2>&1 || true) && \
    (mvn test -B -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testDatePattern ; mvn test -B -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testInvalidDatePattern ; true) > /root/fetch.log 2>&1; \
    grep -c 'BUILD SUCCESS' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading commands once at base, with no network (the hidden test method doesn't exist
# yet, so only the existing ones run).
RUN --network=none (mvn test -B -o -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testDatePattern ; mvn test -B -o -pl gson -Dtest=com.google.gson.DefaultDateTypeAdapterTest#testInvalidDatePattern ; true) > /root/warm.log 2>&1; \
    grep 'Tests run:' /root/warm.log && test "$(grep -c 'BUILD SUCCESS' /root/warm.log)" = 2 && ! grep -q 'offline mode' /root/warm.log && \
    test -z "$(git status --porcelain)"
