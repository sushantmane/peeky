# java-a: Java candidates (jackson-databind from Multi-SWE-bench, lombok)

| instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
| --- | --- | --- | --- | --- | --- |
| fasterxml__jackson-databind-4087 | ok | ok | ok (BeanUtil.java:313: reached, 2 values; assertion :35 reached, 3 values) | survives | build 3.4 min, grade 0.15 min; one launch correction (below) |
| fasterxml__jackson-databind-3716 | ok | ok | ok (StdTypeResolverBuilder.java:112: reached, 4 values; assertion :285 reached, 1 value) | survives | build 3.4 min, grade 0.15 min |
| fasterxml__jackson-databind-2036 | ok | ok | fail (StdTypeResolverBuilder.java:126: error, :161: error, 0 values; the launch never got ready: the test patch doesn't compile at base, cannot find symbol USE_BASE_TYPE_AS_DEFAULT; no Peeky session, no refusal) | dropped at step 3: not reached | build 1.8 min, grade 0.1 min; base f2p MISSING (compile error); launch_timeout cut to 240 s after a first run sat on a zombie launch (below) |
| fasterxml__jackson-databind-4159 | ok | ok | fail (ObjectMapper.java:361: error, 0 values; the launch never got ready: the test patch doesn't compile at base, cannot find symbol NON_FINAL_AND_ENUMS; no Peeky session, no refusal) | dropped at step 3: not reached | build 2.6 min, grade 0.15 min; base f2p MISSING (compile error) |
| fasterxml__jackson-databind-3666 | ok | ok | ok (BeanDeserializer.java:619: no stop; FactoryBasedEnumDeserializer.java:171: reached, 4 values; assertion :373 no stop, the test errors before it) | survives | build 3.4 min, grade 0.15 min |
| fasterxml__jackson-databind-3509 | ok | ok | ok (NumberDeserializers.java:616: reached, 3 values; StdDeserializer.java:1109: no stop; assertion :49 reached, 1 value) | survives | build 3.1 min, grade 0.15 min |
| fasterxml__jackson-databind-4131 | ok | ok | fail (no gold point: the fix only adds methods, so not attempted; assertion WithPathTest.java:211: error, the launch never got ready: the test patch doesn't compile at base; no Peeky session, no refusal) | dropped at step 3: not reached | build 3.1 min, grade 0.1 min; base f2p MISSING (compile error) |
| projectlombok__lombok-3215 | ok | ok | ok (ecj HandleSuperBuilder.java:1103: no stop; javac HandleSuperBuilder.java:1054: reached, 4 values; assertion AbstractRunTests.java:275 reached, 9 values) | survives | build 2.7 min, grade 0.15 min |

## Notes

- All jackson-databind images: JDK 11 (Multi-SWE-bench uses JDK 8, not in the base). Their parent
  pom `jackson-base:<x>-SNAPSHOT` is gone from every repository (Multi-SWE-bench's later snapshot
  too), so each image installs a stand-in in ~/.m2: the line's last release pom under the snapshot
  version (pom.xml unchanged).
- Reach launch correction (jackson-databind-4087, before any record was written): surefire 3.2.1
  never prints the forked JVM's "Listening for transport dt_socket" line, so the first run waited
  for it and was stopped. The launch now waits until port 5005 listens (/proc/net/tcp) and echoes
  the ready line. Points unchanged.
- jackson-databind-2036 reach: the launch's shell exits at once when javac fails, but in the
  container it stays a zombie (PID 1 is `sleep infinity` and doesn't reap it), so reach.py's
  `kill -0` liveness check sees it alive and waits the full launch_timeout (900 s) per point.
  The first run was stopped (no record) and rerun with launch_timeout 240; later jackson specs use
  300. Points unchanged. (A harness quirk in reach.py, not edited.)
- jackson-databind 2036, 4159 and 4131: the dataset's FAIL_TO_PASS is empty because their test
  patches don't compile at base (they call API the fix adds); fail_to_pass is the changed test
  class (in n2p_tests), which grades MISSING at base and PASSED with gold. The same compile failure
  keeps the failing test from ever starting in the reach check, so all three are not reached.
- pass_to_pass is empty for every jackson-databind candidate: eval.sh runs only the hidden test
  files' classes, and PASS_TO_PASS holds none of them. lombok-3215 keeps all 311 PASS_TO_PASS
  cases (TestWithDelombok).
- Reach "first failing test" = the first failing method in file order (4087 testPreventSerialization,
  3716 testWithPojoAsJsonValue, 3666 EnumCreatorTest.testEnumsFromStringUnwrapped, 3509
  testCoerceConfigToNull); for lombok, the whole TestWithDelombok suite, as in round 2.
- `harness/build_images.sh` has no exec bit, so it was run as `heavy.py bash harness/build_images.sh`.

## Counts

- Step 1 (control image + validation): 8 of 8 ok.
- Step 2 (Peeky image + gold/noop): 8 of 8 ok.
- Step 3 (reach): 5 of 8 reached (4087, 3716, 3666, 3509, lombok-3215); 3 dropped (2036, 4159,
  4131: the test patch doesn't compile at base; 4131 also has no gold point).
