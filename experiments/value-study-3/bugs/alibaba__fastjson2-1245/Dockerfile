FROM peeky-study/base:1
# alibaba/fastjson2 at the instance's base commit, built with its Maven wrapper (3.9.0) on JDK 8
# (Multi-SWE-bench's recipe for fastjson2: Zulu 8; here Ubuntu's OpenJDK 8, with its UTF-8 and
# Asia/Shanghai settings as TZ and locale rather than JAVA_TOOL_OPTIONS).
RUN apt-get update -q && apt-get install -y -q --no-install-recommends openjdk-8-jdk-headless && \
    rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-8-openjdk-arm64/bin:$PATH
ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/alibaba/fastjson2.git /testbed && \
    cd /testbed && git checkout -q -B main 6648b96c0162c222467eb44bac30a9d59392c7ff && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 6648b96c0162c222467eb44bac30a9d59392c7ff
WORKDIR /testbed
# Fetch the wrapper's Maven and the core module's dependencies, and build it (the module the
# hidden tests are in), then fetch the test providers by running the grading command once.
RUN ./mvnw -B -V --no-transfer-progress -pl core -am install -DskipTests > /root/build.log 2>&1 || (tail -80 /root/build.log; false)
RUN (./mvnw -B --no-transfer-progress -pl core dependency:go-offline > /root/offline.log 2>&1 || true) && \
    (./mvnw -B --no-transfer-progress -pl core test -Dsurefire.useFile=false -DfailIfNoTests=false \
       -Dsurefire.failIfNoSpecifiedTests=false '-Dtest=com/alibaba/fastjson2/dubbo/*' || true) > /root/fetch.log 2>&1; \
    grep -c 'Tests run:' /root/fetch.log || true
# Maven runs offline from here on (no network once built).
RUN mkdir -p /root/.m2 && \
    printf '<settings>\n  <offline>true</offline>\n</settings>\n' > /root/.m2/settings.xml
# Warm the grading command once at base, offline.
RUN (./mvnw -B -o --no-transfer-progress -pl core test -Dsurefire.useFile=false -DfailIfNoTests=false \
       -Dsurefire.failIfNoSpecifiedTests=false '-Dtest=com/alibaba/fastjson2/dubbo/*' || true) > /root/warm.log 2>&1; \
    grep 'Tests run:' /root/warm.log && grep -q 'BUILD SUCCESS' /root/warm.log && ! grep -q 'offline mode' /root/warm.log && \
    test -z "$(git status --porcelain)"
