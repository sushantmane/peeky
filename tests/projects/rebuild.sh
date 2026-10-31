#!/bin/bash
# Runs inside a value-study image (experiments/value-study/bugs/<bug>/Dockerfile): rebuilds
# its project with clang 18 as ADR 0008's phase 1 asks, then measures the binary
# (measure.py). Usage: rebuild.sh <jq|valkey|micropython|fmt3158|fmt3272|json> [nomacro|libstdcxx]
set -u
project=$1 variant=${2:-}
flags="-g -gdwarf-5 -grecord-command-line"
[ "$variant" != nomacro ] && flags="$flags -fdebug-macro"
cxx=() link=()
if [ "$variant" != libstdcxx ] && [[ $project == fmt* || $project == json ]]; then
  # libstdc++'s headers all have `#pragma GCC system_header`, which drops every MD5.
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq --no-install-recommends libc++-18-dev libc++abi-18-dev >/dev/null 2>&1
  cxx=(-stdlib=libc++ -D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER) link=(-stdlib=libc++)
fi
log=/rebuild.log
cd /testbed
start=$SECONDS
case $project in
  jq) binary=jq compiler=clang-18 ;;
  valkey) binary=src/valkey-server compiler=clang-18 ;;
  micropython) binary=ports/unix/build-standard/micropython compiler=clang-18 ;;
  fmt3158) binary=build-clang/bin/ranges-test compiler=clang++-18 ;;
  fmt3272) binary=build-clang/bin/xchar-test compiler=clang++-18 ;;
  json) binary=build-clang/tests/test-udt_cpp11 compiler=clang++-18 ;;
  *) echo "unknown project $project" >&2; exit 2 ;;
esac
build() {
  case $project in
    jq)
      make distclean >/dev/null 2>&1
      autoreconf -fi >/dev/null 2>&1
      ./configure --with-oniguruma=builtin --disable-shared CC=clang-18 CFLAGS="-O0 $flags" \
        LDFLAGS=-Wl,--build-id=sha1 && touch src/parser.y src/lexer.l && make -j4 ;;
    valkey)
      make distclean >/dev/null 2>&1
      make -j4 CC=clang-18 OPTIMIZATION=-O0 DEBUG="$flags" LDFLAGS=-Wl,--build-id=sha1 valkey-server ;;
    micropython)
      make -C mpy-cross CC=clang-18 && make -C ports/unix VARIANT=standard clean &&
        make -C ports/unix -j4 VARIANT=standard CC=clang-18 COPT=-O0 CFLAGS_EXTRA="$flags" \
          LDFLAGS_EXTRA=-Wl,--build-id=sha1 STRIP=true ;;
    *)
      cmake -S . -B build-clang -DCMAKE_BUILD_TYPE=Debug -DCMAKE_C_COMPILER=clang-18 \
        -DCMAKE_CXX_COMPILER=clang++-18 -DCMAKE_CXX_FLAGS="${flags#-g } ${cxx[*]}" \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,--build-id=sha1 ${link[*]}" -DJSON_BuildTests=ON &&
        cmake --build build-clang --parallel 4 --target "$(basename "$binary")" ;;
  esac
}
build >"$log" 2>&1 || { tail -30 "$log"; exit 1; }
echo "built $binary in $((SECONDS - start)) s"
PYTHONPATH=/peeky/src PYTHONDONTWRITEBYTECODE=1 python3 /peeky/tests/projects/measure.py \
  llvm-dwarfdump-18 "/testbed/$binary" /testbed "$compiler"
