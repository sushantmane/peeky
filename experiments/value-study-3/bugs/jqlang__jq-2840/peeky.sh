# Sourced by round 2's docker/prepare.sh in /testbed: jq built for Peeky in place, as prepare.sh
# builds jqlang__jq-2650 (jq builds only in its source tree).
make distclean >/dev/null 2>&1 || true
autoreconf -fi >/dev/null 2>&1
./configure --with-oniguruma=builtin --disable-shared CC=clang-18 CFLAGS="-O0 $FLAGS" \
  LDFLAGS="$LINK" YFLAGS=-l >/dev/null
# Its Makefile runs flex without LFLAGS: the lexer is generated first, as make would.
flex -L -o src/lexer.c --header-file=src/lexer.h src/lexer.l && touch src/lexer.h
touch src/parser.y && make -j4 >/dev/null 2>&1
file jq | grep -q ELF
native_note "\`/testbed\` is built for Peeky, in place of the project's own build: \`make\` and \`make check\` use it." \
  "./jq" "cd /testbed && make -j4" \
  "peeky probe . --binary ./jq --at src/execute.c:LINE --capture a,b -- '.a' input.json" \
  "configure recorded these flags, and \`YFLAGS=-l\`: bison writes \`src/parser.c\` without \`#line\`, so the grammar's actions are probed there, not in \`src/parser.y\`."
