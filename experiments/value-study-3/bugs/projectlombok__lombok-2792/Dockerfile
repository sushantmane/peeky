FROM peeky-study/base:1
# Lombok at the instance's base commit, built with ant on JDK 11 (SWE-bench spec for version 2792).
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=/usr/lib/jvm/java-11-openjdk-arm64/bin:$PATH
# Ant's optional tasks (junit) are needed by lombok's test targets.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ant-optional && \
    rm -rf /var/lib/apt/lists/*
RUN git clone -q https://github.com/projectlombok/lombok.git /testbed && \
    cd /testbed && git checkout -q -B main 9b3e84717d301d7a6829420f5849cf1465e73131 && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test "$(git rev-parse HEAD)" = 9b3e84717d301d7a6829420f5849cf1465e73131
WORKDIR /testbed
# Fetch the build's dependencies and compile lombok and its tests.
RUN ant -noinput test.compile test.formatter.compile
# Warm the grading command once at base: add SWE-bench's test.instance target, run it, then restore
# the build file so the working tree is clean.
RUN <<'EOS'
set -e
f=buildScripts/tests.ant.xml
{ head -n -1 $f; cat <<'XML'; tail -n 1 $f; } > /root/tests.ant.xml && cp /root/tests.ant.xml $f
<target name="test.instance" depends="test.compile, test.formatter.compile" description="Runs test cases for the swe-bench instance">
      <junit printsummary="yes" fork="true" forkmode="once" haltonfailure="no">
        <formatter classname="lombok.ant.SimpleTestFormatter" usefile="false" unless="tests.quiet" />
        <classpath location="build/ant" />
        <classpath refid="cp.test" />
        <classpath refid="cp.stripe" />
        <classpath refid="packing.basedirs.path" />
        <classpath location="build/tests" />
        <classpath location="build/teststubs" />
        <test name="lombok.transform.TestWithDelombok" />
      </junit>
    </target>
XML
ant -noinput test.instance > /root/warm.log 2>&1 || true
grep -c '\[junit\] \[PASS\]' /root/warm.log
git checkout -- $f
test -z "$(git status --porcelain)"
EOS
