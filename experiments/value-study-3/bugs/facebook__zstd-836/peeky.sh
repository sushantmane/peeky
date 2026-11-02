# Sourced by round 2's docker/prepare.sh in /testbed: the test programs the failing tests run
# (fuzzer and zstreamtest) built for Peeky, in place (tests/Makefile builds its programs in
# tests/, as jq and valkey build in their trees), with clang 18 at -O0, the Makefile's other
# flags kept (its warnings and -DZSTD_DEBUG=1).
rebuild="cd /testbed && CFLAGS=-O0 LDFLAGS=$LINK make -C tests fuzzer zstreamtest CC=clang-18 MOREFLAGS='$FLAGS'"
rm -f tests/fuzzer tests/zstreamtest
eval "${rebuild#cd /testbed && }" >/dev/null 2>&1
file tests/fuzzer | grep -q ELF && file tests/zstreamtest | grep -q ELF
native_note "\`tests/fuzzer\` and \`tests/zstreamtest\` are built for Peeky, in place of the project's own builds of them: \`make -C tests test-fuzzer\` and \`test-zstream\` run them while they are current." \
  "tests/fuzzer (and tests/zstreamtest)" "$rebuild" \
  "peeky probe . --binary tests/fuzzer --at tests/fuzzer.c:LINE --capture a,b -- -T10s" \
  "A plain \`make -C tests fuzzer\` after an edit rebuilds it with the project's own flags (\`-O3\`), which Peeky refuses; \`make clean\` removes them. The rebuild command above brings Peeky's builds back."
