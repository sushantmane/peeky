# Sourced by round 2's docker/prepare.sh in /testbed: the fuzzer test program built for Peeky,
# in place (tests/Makefile builds its programs in tests/, as jq and valkey build in their trees),
# with clang 18 at -O0, its other flags kept (the Makefile's warnings and -DDEBUGLEVEL=1). tests/
# is cleaned first: objects that GCC built there (the image's warm-up run) would be linked in.
rebuild="cd /testbed && CFLAGS=-O0 LDFLAGS=$LINK make -C tests fuzzer CC=clang-18 MOREFLAGS='$FLAGS'"
make -C tests clean >/dev/null 2>&1
eval "${rebuild#cd /testbed && }" >/dev/null 2>&1
file tests/fuzzer | grep -q ELF
native_note "\`tests/fuzzer\` is built for Peeky, in place of the project's own build of it: \`make -C tests test-fuzzer\` runs it while it is current." \
  "tests/fuzzer" "$rebuild" \
  "peeky probe . --binary tests/fuzzer --at tests/fuzzer.c:LINE --capture a,b -- -T10s" \
  "A plain \`make -C tests fuzzer\` after an edit rebuilds it with the project's own flags (\`-O3\`), which Peeky refuses; \`make clean\` removes it. The rebuild command above brings Peeky's build back."
