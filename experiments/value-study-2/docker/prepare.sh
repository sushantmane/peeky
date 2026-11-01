#!/bin/bash
# prepare.sh <bug_id>: what a round-2 Peeky image adds to its bug's control image, after Peeky
# itself. It runs at `docker build`, with network, in /testbed:
#
# - C and C++: the project built for Peeky, with clang 18 at -O0 and DWARF 5, its command line
#   recorded, -fdebug-macro and a build ID, as the C and C++ cookbook's "Debug a binary your
#   project built" asks. Where the build system allows, it goes in a directory of its own and
#   the project's own build is left as the control image has it (fmt, json, MicroPython). jq
#   and valkey build only in the source tree, so there Peeky's build replaces the project's own.
# - Rust: `cargo vendor --locked vendor`, which a workspace's Peeky build needs, offline. Then
#   one Peeky build of /opt/peeky/study/rust.json, a config made by a fixed rule from the test
#   hint alone (rust_config below), so the agent starts with a current build. Git's index of
#   /testbed is refreshed after it.
# - Every bug: /opt/peeky/docs/PROJECT.md says what was prepared, and the guide's copy points
#   to it in one line under its title.
#
# Paths the build adds are listed in .git/info/exclude, so `git status` and the harness's
# agent.patch leave them out. No tracked file changes, but jq's generated parser and lexer
# (as the control image's own build regenerates them too), and ripgrep's Cargo.lock, which gets
# the `version = 3` line Peeky asks for.
set -euo pipefail
id=${1:?usage: prepare.sh <bug_id>}
FLAGS="-g -gdwarf-5 -grecord-command-line -fdebug-macro"
LINK=-Wl,--build-id=sha1
DOCS=/opt/peeky/docs
cd /testbed

exclude() { for path in "$@"; do echo "$path" >> .git/info/exclude; done; }

libcxx() {
  # libstdc++'s headers carry `#pragma GCC system_header`, which leaves a unit with no MD5s.
  # Pinned to clang 18's own, as tests/projects/Dockerfile pins it (#473).
  local v=1:18.1.3-1ubuntu1
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends libc++-18-dev=$v libc++1-18=$v \
    libc++abi-18-dev=$v libc++abi1-18=$v libunwind-18=$v >/dev/null
  rm -rf /var/lib/apt/lists/*
}

cmake_build() {  # cmake_build <target> [cmake options]: Peeky's build in build-peeky/
  local target=$1; shift
  libcxx
  cmake -S . -B build-peeky -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
    -DCMAKE_C_FLAGS_DEBUG="-O0 $FLAGS" \
    -DCMAKE_CXX_FLAGS_DEBUG="-O0 $FLAGS -stdlib=libc++ -D_LIBCPP_HAS_NO_PRAGMA_SYSTEM_HEADER" \
    -DCMAKE_EXE_LINKER_FLAGS="$LINK -stdlib=libc++" "$@" >/dev/null
  cmake --build build-peeky --parallel 4 --target "$target" >/dev/null
  exclude /build-peeky/
}

vendor() {
  # From the image's Cargo cache if it holds every crate, else from crates.io.
  if ! cargo vendor --locked --offline vendor >/dev/null 2>&1; then
    rm -rf vendor
    CARGO_NET_OFFLINE=false cargo vendor --locked vendor >/dev/null
  fi
  exclude /vendor/
}

rust_config() {  # rust_config <bug_id>: the image's config's analyse, run and features fields.
  # The rule (README, "Rust: the image builds the workspace for Peeky"): the package whose tests
  # the test hint's first command runs; the binary the hint names, else the test target that
  # command names or gives as its example, else its library's tests; and the package's `full`
  # feature where it has one. So the fix's package is named only where the hint names it.
  case $1 in
    burntsushi__ripgrep-2209) echo '"analyse": ["ripgrep"], "run": {"bin": "rg"}' ;;
    sharkdp__bat-3108) echo '"analyse": ["bat"], "run": {"bin": "bat"}' ;;
    tokio-rs__axum-1934) echo '"analyse": ["axum"], "run": {"package": "axum", "lib": true}' ;;
    tokio-rs__tokio-4867 | tokio-rs__tokio-6752)
      echo '"analyse": ["tokio"], "run": {"package": "tokio", "test": "sync_mpsc"}, "features": ["full"]' ;;
    uutils__coreutils-6690) echo '"analyse": ["coreutils"], "run": {"bin": "coreutils"}' ;;
    *) echo "no Rust config for $1" >&2; return 1 ;;
  esac
}

rust_prebuild() {  # rust_prebuild <bug_id>: Peeky's build of the image's config, and PROJECT.md
  local config=/opt/peeky/study/rust.json status=failed
  printf '{"workspace": ".", %s, "vendor": "vendor"}\n' "$(rust_config "$1")" > "$config"
  # A failed build leaves the image without one, and PROJECT.md says nothing of it. A build
  # refused with "Build again" is run once more, as its message says. So is one refused for a
  # version 2 Cargo.lock, once the line its message asks for is added (tokio-4867, ripgrep-2209).
  if peeky build . --config "$config" > /opt/peeky/study/prebuild.log 2>&1; then
    status=built
  elif grep -q 'add `version = 3` there' /opt/peeky/study/prebuild.log; then
    sed -i '1i version = 3' Cargo.lock
    echo "prepare.sh: added \`version = 3\` to Cargo.lock" >> /opt/peeky/study/prebuild.log
    if peeky build . --config "$config" >> /opt/peeky/study/prebuild.log 2>&1; then status=built; fi
  elif grep -qiE '\b(build|run) again\b' /opt/peeky/study/prebuild.log &&
    peeky build . --config "$config" >> /opt/peeky/study/prebuild.log 2>&1; then
    status=built
  fi
  echo "$status" > /opt/peeky/study/prebuild.status
  echo "peeky build: $status"; tail -5 /opt/peeky/study/prebuild.log
  git update-index -q --refresh || true
  cat > "$DOCS/PROJECT.md" <<'NOTE'
# This project in this container

The workspace's dependencies are vendored in `/testbed/vendor`, as `cargo vendor --locked vendor`
writes them. A Peeky build of a Rust workspace needs them, and there is no network. Git ignores
the directory. The project's own `cargo build` and `cargo test` don't use it.
NOTE
  [ "$status" = built ] || return 0
  cat >> "$DOCS/PROJECT.md" <<NOTE

\`$config\` is a Peeky config for this workspace
([Rust 7.2](lang/rust-cookbook.md#a-rust-workspace)):

\`\`\`json
$(cat "$config")
\`\`\`

Its Peeky build is current, so a probe with this \`--config\` runs at once. After an edit,
\`peeky build . --config $config\` rebuilds the analysed packages alone. A config that
analyses or runs something else builds the dependencies first, as any first build does.
NOTE
}

native_note() {  # native_note <what> <binary> <rebuild> <probe example> [extra paragraph]
  cat > "$DOCS/PROJECT.md" <<EOF
# This project in this container

$1 It was built with clang 18 at \`-O0 -g -gdwarf-5 -grecord-command-line -fdebug-macro\`,
linked with a build ID, as [C and C++ 7.1](lang/c-cpp-cookbook.md#debug-a-binary-your-project-built)
asks. So Peeky can read it with \`--binary\`.

- **Binary:** \`$2\`.
- **Probe:** \`$4\`.
- **After an edit, rebuild:** \`$3\`. A binary older than its sources is refused, naming the file.
${5:+
$5
}
EOF
}

case $id in
  jqlang__jq-2650)
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
    ;;
  valkey-io__valkey-*)
    rebuild="cd /testbed && make -j4 CC=clang-18 OPTIMIZATION=-O0 DEBUG='$FLAGS' LDFLAGS=$LINK"
    make distclean >/dev/null 2>&1 || true
    eval "${rebuild#cd /testbed && }" >/dev/null 2>&1
    test -x src/valkey-server
    native_note "\`/testbed/src\` is built for Peeky, in place of the project's own build: \`./runtest\` uses it." \
      "src/valkey-server" "$rebuild" \
      "peeky probe . --binary src/valkey-server --at src/server.c:LINE --capture a,b --until hits --background -- --port 7777" \
      "A plain \`make\` rebuilds everything with the project's own flags (GCC, \`-O3\`, LTO), which Peeky refuses. The rebuild command above brings Peeky's build back."
    ;;
  micropython__micropython-13569)
    rebuild="cd /testbed && make -C ports/unix -j4 VARIANT=standard BUILD=build-peeky CC=clang-18 COPT=-O0 CFLAGS_EXTRA='$FLAGS' LDFLAGS_EXTRA=$LINK STRIP=true"
    eval "${rebuild#cd /testbed && }" >/dev/null 2>&1
    test -x ports/unix/build-peeky/micropython
    exclude /ports/unix/build-peeky/
    native_note "\`ports/unix/build-peeky/micropython\` is built for Peeky. The project's own build, \`build-standard\`, is unchanged, and the tests run it unless \`MICROPY_MICROPYTHON\` names Peeky's." \
      "ports/unix/build-peeky/micropython" "$rebuild" \
      "peeky probe . --binary ports/unix/build-peeky/micropython --at py/compile.c:LINE --capture a,b -- script.py"
    ;;
  fmtlib__fmt-3158 | fmtlib__fmt-3272)
    test=ranges-test
    [ "$id" = fmtlib__fmt-3272 ] && test=xchar-test
    cmake_build "$test"
    native_note "\`build-peeky/\` is fmt built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
      "build-peeky/bin/$test (other tests: build them there by name)" \
      "cd /testbed && cmake --build build-peeky --parallel 4 --target $test" \
      "peeky probe . --binary build-peeky/bin/$test --at test/$test.cc:LINE --capture a,b -- --gtest_filter='suite.name'"
    ;;
  nlohmann__json-4237)
    cmake_build test-udt_cpp11 -DJSON_BuildTests=ON
    native_note "\`build-peeky/\` is the library's tests built for Peeky, with libc++. The project's own build, \`build/\`, is unchanged." \
      "build-peeky/tests/test-udt_cpp11 (other tests: build them there by name)" \
      "cd /testbed && cmake --build build-peeky --parallel 4 --target test-udt_cpp11" \
      "peeky probe . --binary build-peeky/tests/test-udt_cpp11 --at tests/src/unit-udt.cpp:LINE --capture a,b"
    ;;
  burntsushi__ripgrep-2209 | sharkdp__bat-3108 | tokio-rs__* | uutils__coreutils-6690)
    vendor
    rust_prebuild "$id"
    ;;
  *)
    if [ -d /opt/venv ]; then
      cat > "$DOCS/PROJECT.md" <<'EOF'
# This project in this container

The project's virtual environment is `/opt/venv`, and it is active (`VIRTUAL_ENV`): Peeky runs
the program on it.
EOF
    else
      cat > "$DOCS/PROJECT.md" <<EOF
# This project in this container

\`peeky setup-java\` has run, with JDK 17 first on \`PATH\`. The project builds and runs on
\`$JAVA_HOME\`.
EOF
    fi
    ;;
esac

# The guide's copy points to PROJECT.md, in one line under its title.
sed -i '1a\
\
**In this container:** [PROJECT.md](PROJECT.md) says what is prepared for Peeky here.' "$DOCS/AGENT_GUIDE.md"
git status --short | head -20
