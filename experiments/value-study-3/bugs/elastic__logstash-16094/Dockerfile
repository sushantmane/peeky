FROM peeky-study/base:1
# elastic/logstash at the instance's base commit, built with its Gradle wrapper (8.5) on JDK 11
# (Multi-SWE-bench's recipe for logstash: Zulu 11, with UTF-8 and Asia/Shanghai in
# JAVA_TOOL_OPTIONS; here the base's OpenJDK 11, with TZ and locale). Its Java tests, logstash-core's
# javaTests task, depend on the build's bootstrap: JRuby and the gems, fetched here.
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/elastic/logstash.git /testbed && \
    cd /testbed && git checkout -q -B main 18583787b3cc1095a002f4a8e1f4d9436e712c54 && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 18583787b3cc1095a002f4a8e1f4d9436e712c54
WORKDIR /testbed
# With network: fetch the wrapper's Gradle and the build's dependencies, bootstrap (JRuby and the
# gems), compile, and run one existing Java test of logstash-core.
RUN ./gradlew --no-daemon :logstash-core:javaTests --tests org.logstash.common.SourceWithMetadataTest \
      > /root/build.log 2>&1 || (tail -100 /root/build.log; false)
# Gradle runs offline from here on (no network once built). The root project's bootstrap task
# ends with a bundler install and `bundle lock --add-platform java`, which reach rubygems.org even
# with every gem installed; once bootstrapped, its own action is skipped while vendor/bundle holds
# the gems (the tasks it depends on, JRuby's install and the assemble tasks, still run).
RUN mkdir -p /root/.gradle/init.d && \
    printf 'gradle.startParameter.offline = true\n' > /root/.gradle/init.d/offline.gradle && \
    cat > /root/.gradle/init.d/bootstrapped.gradle <<'EOF'
// The image is bootstrapped (JRuby in vendor/jruby, the gems in vendor/bundle), and bootstrap's
// bundler steps need the network: skip its own action while the gems are there.
gradle.projectsEvaluated {
    def bootstrap = gradle.rootProject.tasks.findByName('bootstrap')
    if (bootstrap != null) {
        bootstrap.onlyIf { !new File(gradle.rootProject.projectDir, 'vendor/bundle/jruby').isDirectory() }
    }
}
EOF
# Check that the Java tests run with no network: warm the grading command once at base, offline.
RUN --network=none ./gradlew --no-daemon --offline :logstash-core:javaTests \
      --tests org.logstash.log.CustomLogEventTests > /root/warm.log 2>&1 || (tail -100 /root/warm.log; false)
RUN grep -c ' PASSED$' /root/warm.log && test -z "$(git status --porcelain)"
