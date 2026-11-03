#!/bin/bash
# Grades an attempt: restore and apply the hidden tests, rebuild, run them (adapted from the
# SWE-bench Multilingual eval_script for this instance; offline).
set -uxo pipefail
exec 2>&1
cd /testbed
git config --global --add safe.directory /testbed
BASE=c61a404b410bd482fcd6a48d303fb483932214d0
git status
git -c core.fileMode=false diff $BASE
git checkout $BASE -- test/transform/resource/after-delombok/StandardExceptions.java test/transform/resource/after-ecj/StandardExceptions.java
# Files the hidden test patch creates.
rm -f test/transform/resource/after-delombok/StandardExceptionWithConstructor.java test/transform/resource/after-ecj/StandardExceptionWithConstructor.java test/transform/resource/before/StandardExceptionWithConstructor.java test/transform/resource/messages-ecj/StandardExceptionWithConstructor.java.messages
git apply -v /grade/test.patch
# SWE-bench's pre-install step: a test.instance target that runs the instance's test class.
git checkout $BASE -- buildScripts/tests.ant.xml
f=buildScripts/tests.ant.xml
{ head -n -1 $f; cat <<'XML'; tail -n 1 $f; } > /grade/tests.ant.xml && cp /grade/tests.ant.xml $f
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
ant -noinput test.compile
echo '>>>>> Start Test Output'
(ant -noinput test.instance) | cat
echo '>>>>> End Test Output'
