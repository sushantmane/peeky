FROM peeky-study/base:1
# elastic/logstash at the instance's base commit, built with its Gradle wrapper (7.5) on JDK 11
# (Multi-SWE-bench's recipe for logstash: Zulu 11, with UTF-8 and Asia/Shanghai in
# JAVA_TOOL_OPTIONS; here the base's OpenJDK 11, with TZ and locale). Its Java tests, logstash-core's
# javaTests task, depend on the build's bootstrap: JRuby and the gems, fetched here. This bug's tests
# are the JVM options parser's (tools/jvm-options-parser, Gradle project :jvm-options-parser).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
ENV TZ=Asia/Shanghai LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone -q https://github.com/elastic/logstash.git /testbed && \
    cd /testbed && git checkout -q -B main 1cca6bcb2c769db169260a30531c4f2bd2f184c3 && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 1cca6bcb2c769db169260a30531c4f2bd2f184c3
WORKDIR /testbed
# With network: fetch the wrapper's Gradle and the build's dependencies, bootstrap (JRuby and the
# gems), compile, and run one existing Java test of logstash-core and the parser's tests.
RUN ./gradlew --no-daemon :logstash-core:javaTests --tests org.logstash.common.SourceWithMetadataTest \
      :jvm-options-parser:test > /root/build.log 2>&1 || (tail -100 /root/build.log; false)
# JUnit 4.13.1, in Gradle's cache: the upstream fix moves the parser's tests from JUnit 4.12 to
# 4.13.1 (the hidden tests run on either), and grading is offline. Fetched by a throwaway build.
RUN mkdir -p /root/junit-fetch && cd /root/junit-fetch && touch settings.gradle && \
    printf '%s\n' "apply plugin: 'java'" "repositories { mavenCentral() }" \
      "dependencies { testImplementation 'junit:junit:4.13.1' }" \
      "task fetch { doLast { println configurations.testRuntimeClasspath.files } }" > build.gradle && \
    /testbed/gradlew --no-daemon -q fetch && cd / && rm -rf /root/junit-fetch && \
    find /root/.gradle/caches/modules-2 -name 'junit-4.13.1.jar' | grep -q .
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
RUN --network=none ./gradlew --no-daemon --offline :jvm-options-parser:test \
      --tests org.logstash.launchers.JvmOptionsParserTest > /root/warm.log 2>&1 || (tail -100 /root/warm.log; false)
RUN --network=none ./gradlew --no-daemon --offline :logstash-core:javaTests \
      --tests org.logstash.common.SourceWithMetadataTest > /root/offline.log 2>&1 || (tail -100 /root/offline.log; false)
RUN grep -c ' PASSED$' /root/warm.log && test -z "$(git status --porcelain)"
