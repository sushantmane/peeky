# r-java-a: refill Java candidates (jackson-core, jackson-dataformat-xml, jackson-databind from Multi-SWE-bench; lombok from SWE-bench Multilingual)

| refill # | instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | fasterxml__jackson-core-1016 | ok | ok | fail (JsonFactory.java:1302: no stop; NonBlockingJsonParserBase.java:794: no stop; the test JVM ended, exit 0, both runs; assertion JsonFactoryTest.java:335 reached, 3 values; no refusal) | dropped at step 3: not reached | build 2.7 min, grade 0.1 min. JDK 11 + jackson-base stand-in pom (as databind). At base both failing methods (each calls doCanonicalizationTest(false)) fail at the DataInput parser's assertNotSame (test line 324), before the non-blocking section that runs both gold lines. Run: first failing method, testCanonicalizationEnabled |
| 10 | fasterxml__jackson-dataformat-xml-590 | ok | — | — (no gold point: the fix only adds an overriding method, findAndAddVirtualProperties, that returns at once; not attempted) | dropped at step 3: no gold point | build 2.3 min, grade 0.1 min. JDK 11 + jackson-base stand-in (2.15.4 under 2.15.0-rc3-SNAPSHOT). Base FAILED (virtual property written twice), gold-1/gold-2 PASSED. No Peeky image or reach run, per the refill brief |
| 20 | projectlombok__lombok-2792 | ok | — | — (no gold point: the fix only renames HandleVal.visitLocal to endVisitLocal, a signature; not attempted) | dropped at step 3: no gold point | build 2.5 min, grade 0.1 min. JDK 11, lombok-3215's recipe. Base FAILED, gold-1/gold-2 PASSED, 300 p2p pass. No Peeky image or reach run, per the refill brief |
| 22 | fasterxml__jackson-core-1182 | ok | — | — (not attempted: the test patch doesn't compile at base, cannot find symbol NumberTypeFP; and no gold point, the fix only adds an enum and methods) | dropped at step 3: the failing test can't build at base | build 2.3 min, grade 0.1 min. JDK 11 + jackson-base stand-in (2.17.3 under 2.17.0-SNAPSHOT). FAIL_TO_PASS empty in the dataset; f2p = the 2 changed classes (n2p): MISSING at base, PASSED with gold twice. No Peeky image or reach run, per the refill brief |
| 26 | fasterxml__jackson-databind-3851 | ok | ok | ok (FactoryBasedEnumDeserializer.java:135: reached, 3 values; assertion JsonCreatorModeForEnum3566.java:148: no stop, the test throws MismatchedInputException before it) | survives | build 3.1 min, grade 0.15 min. JDK 11 + jackson-base stand-in (2.15.4 under 2.15.0-rc3-SNAPSHOT); full suite at base: 2 unrelated failures (StreamReadStringConstraintsTest, core 2.15.4's limit). One gold point (the field-declaration hunk doesn't count). Run: testEnumACreatorModeDelegating, the first failing method |
| 34 | projectlombok__lombok-3674 | ok | ok | ok (ecj HandleStandardException.java:74: no stop, TestWithDelombok runs javac only; javac HandleStandardException.java:94: reached, 5 values; assertion AbstractRunTests.java:308 reached, 9 values) | survives | build 3.9 min, grade 0.15 min. JDK 11, lombok-3215's recipe; p2p 1 case (dataset). Run: the TestWithDelombok suite, as lombok-3215 (no runtime filter for one file) |
| 38 | fasterxml__jackson-databind-4048 | ok | ok | fail (POJOPropertiesCollector.java:615: refused, INVALID_BREAKPOINT "Line 615 of src/main/java/com/fasterxml/jackson/databind/introspect/POJOPropertiesCollector.java has no code to stop at; executable lines nearby: 604, 607, 610, 613, 614, 625 (see `peeky describe`)", not retryable; assertion Transient3948Test.java:59 reached, 3 values) | dropped at step 3: not reached | build 4.4 min, grade 0.15 min. JDK 11 + jackson-base stand-in (2.16.2, as 4087). The rule's point is the hunk's first changed line, `} else {`, which has no bytecode; not moved (see decisions below). Run: testJsonIgnoreSerialization, the first failing method |

## Notes

- All jackson images (core, dataformat-xml, databind): JDK 11 instead of Multi-SWE-bench's JDK 8
  (not in the base; Peeky launches Java on JDK 9 or later only). Their parent pom
  `jackson-base:<x>-SNAPSHOT` is gone from every repository (Multi-SWE-bench's rewritten later
  snapshot too), so each image installs a stand-in in ~/.m2: the minor line's last release pom
  under the snapshot version (2.15.4, 2.16.2, 2.17.3), pom.xml unchanged, as the first pass's
  jackson-databind images. eval.sh runs only the hidden test files' classes, so pass_to_pass is
  empty for every jackson candidate (PASS_TO_PASS holds none of those classes).
- Lombok images: lombok-3215's recipe (JDK 11, ant-optional, SWE-bench's test.instance target
  added at grading time). pass_to_pass = the dataset's PASS_TO_PASS (2792: 300 cases; 3674: 1).
- Java launches wait for port 5005 (surefire 3 hides the JDWP line), with the first pass's
  zombie-aware loop; launch_timeout 600. No run corrections were needed; no first-run records to
  keep.
- Reach "first failing test" = the first failing method in file order that runs the line at base
  (none ran the line for jackson-core-1016, so its points use the first failing method); for
  lombok, the whole TestWithDelombok suite, as lombok-3215 (DirectoryRunner has no runtime
  filter; its cases run in file-name order, and StandardExceptionWithConstructor.java is the first
  case that runs the javac gold line).
- Step-3 drops without a Peeky image or reach run (refill rule 4), after a complete step 1:
  dataformat-xml-590 and lombok-2792 (no gold point), jackson-core-1182 (the failing test can't
  build at base; also no gold point).
- `docker image rm` done for every dropped candidate's images; survivors keep `:control` and
  `:peeky-r3`.

## Counts

- Examined: 7 (refill #2, 10, 20, 22, 26, 34, 38).
- Step 1 (control image + validation): 7 of 7 ok.
- Step 2 (Peeky image + gold/noop): 4 attempted (1016, 3851, 3674, 4048), 4 ok; 3 skipped by refill
  rule 4 (590, 2792, 1182).
- Step 3 (reach): 2 reached (jackson-databind-3851, lombok-3674); 5 dropped: 1016 (no stop: the
  failing test fails before the gold lines), 590 and 2792 (no gold point), 1182 (the failing test
  can't build at base), 4048 (refused: INVALID_BREAKPOINT, the gold line `} else {` has no code).
- Survivors: fasterxml__jackson-databind-3851 (#26), projectlombok__lombok-3674 (#34).

## Decision for the coordinator

- jackson-databind-4048: the point rule (a hunk's point is the first line it removes or changes)
  picks `} else {` (POJOPropertiesCollector.java:615), which has no bytecode in Java; Peeky refused
  with INVALID_BREAKPOINT and named the nearby executable lines (613 `if (transientAsIgnoral)`,
  which the failing test runs, and 614, 625). The point wasn't moved. Whether a point on a line
  with no code should take the next executable line of the hunk's statement is the rule's
  question, not Peeky's; the images were removed as the brief says (rebuild is ~5 minutes).
