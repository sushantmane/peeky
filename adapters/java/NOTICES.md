# Experimental Java adapter provenance

The setup script downloads the exact Microsoft java-debug source archive and five runtime jars pinned by URL and SHA-256 in `dependencies.lock.json`. It compiles the debugger core sources, with one small patch whose text is in the setup script (`eventhub-log-guard`: a log line no longer kills java-debug's event thread), plus Peeky's launcher. Downloaded source/jars/classes must not be committed.

Microsoft java-debug is EPL-1.0. The produced bundle retains upstream `LICENSE.txt` and `ThirdPartyNotices.txt` in `notices/`. Upstream source: https://github.com/microsoft/java-debug/tree/6a597f2bcccdd0aac7e91631588361826fde11f3 . Sources remain available at the pinned URL in the lock. No JDT LS code is bundled.

Dependency coordinates and licenses (their original notices/licenses remain in the jars):

- org.apache.commons:commons-lang3:3.18.0 — Apache-2.0
- com.google.code.gson:gson:2.8.9 — Apache-2.0
- io.reactivex.rxjava2:rxjava:2.2.21 — Apache-2.0
- org.reactivestreams:reactive-streams:1.0.4 — CC0-1.0
- commons-io:commons-io:2.14.0 — Apache-2.0

This experimental source launcher and tiny fixtures do not establish comprehensive Java support. Before distributing a release, review the full bundled dependency notices, source availability requirements, and dependency security status. Setup creates a local bundle and never publishes anything.
