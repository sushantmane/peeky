#!/bin/bash
# Runs inside a value-study image (experiments/value-study/bugs/<bug>/Dockerfile): builds its
# project as ADR 0008's phase 1 asks, with clang 18 at -O0 and DWARF 5, its command line
# recorded, -fdebug-macro, and a build ID. jq's grammar and lexer are generated without
# `#line` (bison -l, flex -L); C++ uses libc++ without its system-header pragma.
# Usage: build.sh <jq|valkey|micropython|fmt3158|fmt3272|json> [variant]
# Variants: nomacro (no -fdebug-macro), libstdcxx (C++ with libstdc++), lines (jq's grammar
# and lexer with their `#line`), gcc (GCC 13), dwarf4 (-gdwarf-4), optimized (the project's own
# -O level). Prints the binary's path, relative to the project ($TESTBED, /testbed by default).
# Needs nothing else of Peeky's: a Dockerfile can prebuild an image with it (README.md). Runs
# as root, as the images do: it may install libc++ (apt) and writes its log to /.
set -u
project=$1 variant=${2:-}
flags="-g -gdwarf-5 -grecord-command-line"
[ "$variant" = dwarf4 ] && flags="-g -gdwarf-4 -grecord-command-line"
[ "$variant" != nomacro ] && [ "$variant" != gcc ] && flags="$flags -fdebug-macro"
cc=clang-18 cxx=clang++-18
if [ "$variant" = gcc ]; then
  # GCC knows neither -grecord-command-line nor -fdebug-macro: it records its switches.
  cc=gcc-13 cxx=g++-13 flags="-g -gdwarf-5 -grecord-gcc-switches"
fi
cxxlib=() link=()
if [ "$variant" != libstdcxx ] && [ "$variant" != gcc ] && [[ $project == fmt* || $project == json ]]; then
  # libstdc++'s headers all have `#pragma GCC system_header`, which drops every MD5.
  if ! dpkg -s libc++-18-dev >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq --no-install-recommends libc++-18-dev libc++abi-18-dev >/dev/null 2>&1
  fi
  cxxlib=(-stdlib=libc++ -D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER) link=(-stdlib=libc++)
fi
case $project in
  jq) binary=jq level=-O2 ;;
  valkey) binary=src/valkey-server level=-O3 ;;
  micropython) binary=ports/unix/build-standard/micropython level=-Os ;;
  fmt3158) binary=build-clang/bin/ranges-test level=-O3 ;;
  fmt3272) binary=build-clang/bin/xchar-test level=-O3 ;;
  json) binary=build-clang/tests/test-udt_cpp11 level=-O3 ;;
  *) echo "unknown project $project" >&2; exit 2 ;;
esac
opt=-O0
[ "$variant" = optimized ] && opt=$level
log=/build-$project${variant:+-$variant}.log
cd "${TESTBED:-/testbed}" || exit 1
build() {
  case $project in
    jq)
      make distclean >/dev/null 2>&1
      autoreconf -fi >/dev/null 2>&1
      ./configure --with-oniguruma=builtin --disable-shared CC=$cc CFLAGS="$opt $flags" \
        LDFLAGS=-Wl,--build-id=sha1 || return 1
      if [ "$variant" = lines ]; then
        touch src/parser.y src/lexer.l && make -j4
      else
        # Its Makefile runs flex without LFLAGS: the lexer is generated first, as make would.
        flex -L -o src/lexer.c --header-file=src/lexer.h src/lexer.l && touch src/lexer.h &&
          touch src/parser.y && make -j4 YFLAGS=-l
      fi ;;
    valkey)
      make distclean >/dev/null 2>&1
      make -j4 CC=$cc OPTIMIZATION=$opt DEBUG="$flags" LDFLAGS=-Wl,--build-id=sha1 valkey-server ;;
    micropython)
      make -C mpy-cross CC=$cc && make -C ports/unix VARIANT=standard clean &&
        make -C ports/unix -j4 VARIANT=standard CC=$cc COPT=$opt CFLAGS_EXTRA="$flags" \
          LDFLAGS_EXTRA=-Wl,--build-id=sha1 STRIP=true ;;
    *)
      rm -rf build-clang
      cmake -S . -B build-clang -DCMAKE_BUILD_TYPE=Debug -DCMAKE_C_COMPILER=$cc \
        -DCMAKE_CXX_COMPILER=$cxx -DCMAKE_CXX_FLAGS_DEBUG="$opt $flags ${cxxlib[*]}" \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,--build-id=sha1 ${link[*]}" -DJSON_BuildTests=ON &&
        cmake --build build-clang --parallel 4 --target "$(basename "$binary")" ;;
  esac
}
start=$SECONDS
build >"$log" 2>&1 || { tail -30 "$log" >&2; exit 1; }
echo "built $binary in $((SECONDS - start)) s" >&2
echo "$binary"
