# Round 3 candidates: java-b

| instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
|---|---|---|---|---|---|
| alibaba__fastjson2-1245 | ok | ok (image's Java check fails: JDK 8; gold solved, noop not) | ObjectReaderImplList.java:387: no stop (terminated; base throws earlier, at the auto-type check, line 304); assertion DubboTest6.java:76 reached, 9 values | dropped at step 3: not reached | build 2 min, grade 0.5 min. JDK 8 per Multi-SWE-bench; Peeky refuses to launch on JDK 8 ("[UNSUPPORTED] ... only from that JDK's own bin/java: a native executable in a JDK 9 or later"), attach works. Run correction: launch's ready line (surefire 3 hides the JDWP line; wait for port 5005 instead) |
| elastic__logstash-16579 | ok | ok | BufferedTokenizerExt.java:81: reached, 4 values; :107 (flush): no stop (test never calls flush); assertion :95 reached, 1 value | survives | build 2.5 min, grade 0.3 min. JDK 11; offline needs an init script that skips bootstrap's bundler action once the gems are installed (bootstrap, downloadJRuby removed from p2p: SKIPPED). Launch waits for port 5005 (as java-a) |
| elastic__logstash-16094 | ok | ok | CustomLogEventSerializer.java:72: reached, 4 values; environment.rb:127 (Ruby, first code hunk): not attempted (Java test never runs it); assertion :193 reached, 6 values | survives | build 1.5 min, grade 0.2 min. Same image recipe as 16579. Hidden test needs a new system property name and `message_1`, neither in the problem statement |
| elastic__logstash-15928 | ok | ok | JvmOptionsParser.java:183: reached, 5 values; assertion :138 reached, 3 values | survives | build 1.5 min, grade 0.1 min. Gold bumps the parser's JUnit to 4.13.1: the image's Gradle cache holds it (throwaway build) so gold builds offline; hidden tests pass on 4.12 too |
| elastic__logstash-15964 | ok | ok | DeadLetterQueueWriter.java:316: error, launch never ready (hidden test doesn't compile at base: stub overrides shutdown()); assertion :271: same | dropped at step 3: not reached | build 2 min, grade 0.2 min. f2p = compileTestJava + the changed class's 6 tests (n2p). Run corrections: launch loop exits when Gradle dies (zombie-aware), launch_timeout 900→300 (reach.py's kill -0 sees the dead launch shell as a zombie, so it waits the full timeout) |
| alibaba__fastjson2-82 | ok | ok, but the image fails build_images.sh's Java check (JDK 8: Peeky refuses to launch; attach works); gold solved, noop not | JSON.java:602: reached, 1 value; :621: reached, 1 value; assertion Issue81.java:15 reached, 4 values | survives (decision needed: JDK 8 image fails Peeky's Java check) | build 2 min, grade 0.1 min. JDK 8 per Multi-SWE-bench. Fix to step 1: the image runs `clean` once with network (the clean plugin's plexus-utils was missing offline) |
| mockito__mockito-3133 | ok | ok | GenericMaster.java:20, MockitoExtension.java:191: error, launch never ready (hidden test doesn't compile at base: @Captor on parameters); assertion JunitJupiterTest.java:61: same | dropped at step 3: not reached | build 3 min, grade 0.2 min. JDK 21. f2p = compileTestJava + n2p tasks junit-jupiter:testClasses, :test (no per-test lines); eval uses --no-build-cache |
| apache__dubbo-7041 | ok | ok | ReflectUtils.java:1206: reached, 4 values; assertion ReflectUtilsTest.java:412 reached, 3 values | survives | build 2 min, grade 0.1 min. JDK 17 + Maven; eval runs only ReflectUtilsTest, so pass_to_pass is empty |

Counts: step 1 (control image + validation) 8/8 ok; step 2 (Peeky image + gold/noop) 8/8 graded
right, but the two JDK 8 images (fastjson2) fail build_images.sh's Java check; step 3 (reach) 5
reached, 3 not. Survivors: elastic__logstash-16579, elastic__logstash-16094,
elastic__logstash-15928, apache__dubbo-7041, and alibaba__fastjson2-82 (pending the JDK 8
decision).

Decisions for the coordinator:
- JDK 8 (fastjson2, Multi-SWE-bench's Zulu 8): Peeky refuses to launch a Java program on JDK 8,
  so the Peeky image fails its check; attaching to the test JVM works (reach used it). The same
  fastjson2-82 control image graded on the base's JDK 11 (offline, JAVA_HOME and PATH only) gives
  the same results (base: Issue81 FAILED; gold: PASSED; the 5 pass_to_pass PASSED), so a JDK 11
  rebuild is an option.
- reach.py: a launch that exits (e.g. the test doesn't compile) still waits the whole
  launch_timeout, because `kill -0` sees its shell as a zombie (the container's PID 1,
  `sleep infinity`, doesn't reap). My specs use launch_timeout 300 for the later candidates.
- Multi-SWE-bench instances whose fail_to_pass is a compile task (compileTestJava) can't be
  reached by the rules: the hidden test doesn't compile at base (15964, 3133 here).
