# Round 3 refill: r-java-b (Java)

| refill # | instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
|---|---|---|---|---|---|---|
| 4 | mockito__mockito-3173 | ok | ok | InlineDelegateByteBuddyMockMaker.java:275: reached, 5 values; :650: reached, 2 values; assertion RecordTest.java:24 reached, 3 values | survives | build 7.6 min, grade 0.1 min. JDK 21 + Gradle 8.4 (as Multi-SWE-bench). Image build ran a throwaway java21 test once (with network) to fetch its test runtime, then removed it. First hunk changes a field declaration, so the first gold point is the second hunk. No run corrections |
| 8 | elastic__logstash-16681 | ok | ok | PipelineBusV2.java:146: reached, 4 values; assertion PipelineBusTest.java:343 reached, 6 values | survives | build 2.1 min, grade 0.4 min. JDK 11, same image recipe as logstash-16579 (bootstrap and downloadJRuby removed from p2p: SKIPPED). One code hunk, so one gold point (its first removed line is a comment; point = first changed statement). Concurrency test (deadlock, 1 s deadline): base failed, gold passed twice. No run corrections |
| 14 | apache__druid-14092 | ok | ok | DruidLeaderClient.java:157: no stop (catch block: at base the test gets a 503 response, no exception); :217: reached, 5 values; assertion DruidLeaderClientTest.java:253 reached, 7 values | survives | build 7.5 min, grade 0.5 min. JDK 11, Maven; SWE-bench install step (processing, cloud/aws-common, cloud/gcp-common with -am), server module built by its test run. First two hunks are Javadoc/signature, last adds a method, so gold points are go's catch block and the statement after the 503/504 insertion. No run corrections |
| 16 | apache__dubbo-10638 | ok | ok | CompatibleTypeUtils.java:131: no stop (at base realize throws before any LocalTime string is parsed); PojoUtils.java:145: reached, 2 values; assertion PojoUtilsTest.java:771 no stop (realize throws at :770 first) | survives | build 3.9 min, grade 0.1 min. JDK 17 + Maven, as dubbo-7041, plus libnss-myhostname: this surefire (3.0.0-M5, TCP fork node) resolves the container host name, which fails with no network (first validation: all MISSING, "UnknownHostException"); image rebuilt, validation rerun. No run corrections |
| 28 | alibaba__fastjson2-2285 | ok | ok | ObjectWriterBaseModule.java:222: refused (INVALID_REQUEST "Missing executable line debug information; compile with -g"); assertion Issue2269.java:21: refused (same) | dropped at step 3: not reached (Peeky refused) | build 3.3 min, grade 0.5 min. JDK 11 instead of Multi-SWE-bench's JDK 8 (refill rule; same grading results). Refusal cause: Peeky's attach parses every class in the declared sources' package dirs, and the hidden test's package issues_2200 holds Kotlin-compiled test classes (Issue2231.kt, Issue2264.kt, Issue2276.kt); not worked around. Images removed |
| 32 | google__gson-1100 | ok | ok | DefaultDateTypeAdapter.java:87 (write): no stop (at base fromJson("null") throws in read before toJson reaches write); :95 (read): reached, 2 values; assertion DefaultDateTypeAdapterTest.java:166 reached, 2 values | survives | build 1.6 min, grade 0.1 min. JDK 11, Maven, SWE-bench install step. No run corrections |
| 40 | google__gson-1703 | ok | ok | Streams.java:92: refused (INVALID_REQUEST "Missing executable line debug information; compile with -g"); assertion ReadersWritersTest.java:177: refused (same) | dropped at step 3: not reached (Peeky refused) | build 1.3 min, grade 0.1 min. JDK 11 + Maven. Dataset F2P/P2P empty (its parser misses surefire 3 lines): fail_to_pass = the changed test class; parser adapted (Maven prefixes, "s" unit). Refusal cause: Peeky's attach parses every class in the declared sources' package dirs, and the test package com.google.gson.functional holds ProGuard-processed EnumWithObfuscatedTest classes without line tables; not worked around. Images removed |

## Notes

- Every launch waits for port 5005 (surefire 3 and Gradle hide or delay the JDWP line) and exits
  if the build dies (zombie-aware), as the first pass's later specs; launch_timeout 300 (druid 600).
  No run corrections were needed: every first run is the record.
- Gold points that the failing test doesn't run at base, so "no stop" is expected: druid-14092's
  catch block (the test gets a 503 response, no exception), dubbo-10638's LocalTime conversion and
  gson-1100's write (the test throws earlier, in realize / read). Each of these bugs also has a
  reached gold point.
- dubbo-10638's image adds libnss-myhostname (a change to the dubbo-7041 recipe): this version's
  surefire forks over TCP and calls InetAddress.getLocalHost, and a `--network none` container's
  /etc/hosts lacks its host name. The first validation gave MISSING (UnknownHostException); after
  the rebuild it was rerun from scratch.
- fastjson2-2285 is on JDK 11 instead of Multi-SWE-bench's JDK 8 (refill rule 5); grading matched
  the dataset (base fails, gold passes twice).
- gson-1703: the dataset's FAIL_TO_PASS and PASS_TO_PASS are empty because Multi-SWE-bench's
  gson parser expects surefire 2's lines; fail_to_pass is the class the test patch changes, and
  parse.py strips Maven's `[INFO] ` prefixes and accepts "s" as the time unit.

## Decision needed: a Peeky Java-attach refusal (2 candidates)

fastjson2-2285 and gson-1703 were refused at `peeky session start` (attach config) with
INVALID_REQUEST "Missing executable line debug information; compile with -g", for the gold and the
assertion point alike. Peeky's attach (java_attach.py) parses every `.class` file in the package
directories of the config's declared sources, and `_class_metadata` raises on any class with a
method lacking a line table (or with a non-Java SourceFile) before checking whether the class
belongs to a declared source. The offending classes are in the hidden test's package, not in the
fix's: fastjson2's Kotlin-compiled tests (Issue2231.kt, Issue2264.kt, Issue2276.kt in
issues_2200) and gson's ProGuard-processed EnumWithObfuscatedTest classes (functional). The fix's
packages parse cleanly (fastjson2 writer: 162 classes; gson internal: 57), so a gold point whose
config lists only the fix's sources would likely attach. Not worked around: the specs list the
gold and test sources, as round 2's Java specs do. Worth filing as a Peeky reach gap (like
#495-#500).

## Counts

- Examined: 7 (refill 4, 8, 14, 16, 28, 32, 40).
- Step 1 (control image + validation): 7 of 7 ok.
- Step 2 (Peeky image + gold/noop): 7 of 7 ok.
- Step 3 (reach): 5 reached (mockito-3173, logstash-16681, druid-14092, dubbo-10638, gson-1100);
  2 refused by Peeky (fastjson2-2285, gson-1703).
