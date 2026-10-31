# Java candidates examined

| instance | result | note |
|---|---|---|
| projectlombok__lombok-3009 | accepted (E) | control slot; ant, JDK 17; build 2 min, grade 12 s |
| reactivex__rxjava-7597 | accepted (D) | Gradle 7.6.2, JDK 11; build 1.5 min, grade 15 s |
| apache__lucene-12196 | skipped | category slot full: E (cause clear from report + MultiFieldQueryParser.applySlop, simple state); not A: clean build + test in the image takes 22 s (4 CPUs); validated (base fails, gold passes twice), files kept in scratch |
| google__gson-1093 | skipped | category slot full: E (JsonWriter.value(double) ignores lenient; clear from report and code, simple state) |
| javaparser__javaparser-4561 | skipped | category slot full: E (UnsupportedOperationException names TextBlockLiteralExpr, missing visit in TypeExtractor); not A: clean install + tests 24 s |
| apache__druid-14092 | skipped | category slot full: E (report names DruidLeaderClient treating 503 as a valid response without refreshing the leader cache); not A: offline clean install of the needed modules 46 s + test 21 s (4 CPUs); validated (base fails, gold passes twice), files kept in scratch |
| projectlombok__lombok-3571 | skipped | category slot full: E (nested records not treated as implicitly static when building the builder type reference; clear from report and handler code, one small record) |
| apache__lucene-13494 | skipped | category slot full: E (NPE stack trace points at the null denseCounts in StringValueFacetCounts for an empty match set); not A: lucene clean build + test of one module measured at 22 s (lucene-12196) |
| google__gson-2479 | skipped | category slot full: E (feature request: GsonBuilder should reject adapters for Object/JsonElement; fully specified in the report) |
| javaparser__javaparser-4538 | skipped | category slot full: E (report and stack trace point at PostOrderIterator peeking an empty stack for a childless root) |
| apache__druid-16875 | skipped | category slot full: E (Guice error names WorkerTaskCountStatsMonitor, whose constructor treats every non-MiddleManager as an Indexer); not A: druid server tests measured at about 1 min (druid-14092) |
| projectlombok__lombok-3594 | skipped | category slot full: E (report shows the dropped type argument; HandleSuperBuilder.getTypeParamExpressions has no case for annotated type arguments; one small class) |
| apache__lucene-12022 | accepted (D) | Gradle, JDK 17 (+ toolchain 19); build 2.8 min, grade 25 s; clean build + test 21 s so not A |
| google__gson-1100 | skipped | category slot full: E (stack trace points at DefaultDateTypeAdapter.write formatting a null Date) |
| apache__druid-13704 | skipped | category slot full: E (feature request: add a pow op to ArithmeticPostAggregator; fully specified in the report) |
| projectlombok__lombok-3052 | accepted (B) | ant, JDK 11; javac argument-type cache keyed by position; build 1.9 min, grade 9 s |
| apache__lucene-13170 | skipped | category slot full: E (report names the off-by-one in OpenNLPSentenceBreakIterator.preceding and gives the fix) |
| google__gson-2024 | skipped | category slot full: E (feature request: new FieldNamingPolicy UPPER_CASE_WITH_UNDERSCORES; fully specified in the report) |
| apache__druid-15402 | accepted (D) | Maven, JDK 11; build 5.8 min (downloads), grade 1 min; clean install + test 48 s so not A |
| projectlombok__lombok-3326 | accepted (B) | ant, JDK 11; NPE inside javac from a stale cached erasure; build 1.7 min, grade 9 s |
| apache__lucene-11760 | skipped | category slot full: E (report states the cause: IntervalBuilder.NO_INTERVALS returns NO_MORE_DOCS from docID() when unpositioned) |
| apache__druid-14136 | skipped | category slot full: E (report gives the cause: VersionedIntervalTimeline.lookup and Joda overlaps() on a zero-length interval; B also applies but the report explains the Joda behaviour) |
| projectlombok__lombok-3042 | skipped | category slot full: E (stack trace shows the NPE in JavacHandlerUtil.clearTypes scanning a null subtree; fix is a null check) |
| apache__lucene-13704 | accepted (D) | Gradle 8.10, JDK 21; int overflow at encoded values near the top of the range; build 2.9 min, grade 30 s; clean build + test 24 s so not A |
