# Adapter and build foundation review — 2026-10-14

This slice extracts Python adapter configuration, adds standalone trusted build preparation, and qualifies an experimental headless Java launcher directly over DAP. Java is not yet exposed through the worker or agent.

## Independent findings and dispositions

- **Build input/output overlap and prebuilt artifacts:** the architecture reviewer found a recipe could copy an existing binary and call it a prepared artifact. Fixed: artifacts require fresh build commands and cannot overlap copied inputs. Regression tests cover both cases.
- **Ambient compiler configuration:** inherited flags such as JAVA_TOOL_OPTIONS/CFLAGS could change a build without appearing in its recipe. Fixed: minimal inherited environment allowlist plus explicit overrides; effective environment is included in the manifest. Undeclared tool dependencies still prevent a hermetic-build claim.
- **Bootstrap subprocess credentials/output:** the integration review identified inherited worker credentials and unbounded compiler capture. Fixed: strip DEBUG_TOKEN, cap retained diagnostics, monitor output size and deadline, and reap the owned process group. Bootstrap regressions cover credential stripping and noisy output.
- **Adapter capability overclaim:** Java upstream initialization describes services the shim does not implement. Fixed: a positive capability list and command/argument rejection for unsupported evaluation, conditional breakpoints, logpoints and expressions. Tests cover these rejections.

The testing reviewer independently checked Python adapter/worker compatibility (33 adapter/DAP/integration tests passed). The architecture reviewer checked fresh builds/integrity (35 build/worker tests passed), trusted command boundaries, archive checksums before extraction/compilation, staged bundle publication, source-map handling and Java capability restrictions. No blocking finding remained for the experimental scope.

## Integration gates still open

The Java source map is explicitly supplied by trusted preparation; fixture mappings are checked against javap debug metadata. A generic compiled-source resolver is not implemented. The build manifest identifies prepared files, not which image a running process loaded. Before Java becomes a worker target, integrate artifact checks before launch/mutation/verification, source mapping, pending-breakpoint resolution, session/stop identity, uncertain mutation handling, and JVM-descendant cleanup failure tests. Candidate rebuild and independent checker acceptance also remain required.
