#!/usr/bin/env bash
# Build Venice on Apple Silicon without Rosetta.
# grpc-java publishes only x86_64 macOS protoc-gen-grpc-java binaries. This builds the
# exact pinned generator source (default 1.59.1) natively against Homebrew protobuf and
# abseil, adapting only string_view -> std::string call sites, then writes a Gradle init
# script that points Venice's protobuf plugin at it. Nothing in the Venice repo changes.
set -euo pipefail
VERSION="${1:-1.59.1}"
OUT="${PEEKY_VENICE_DIR:-$HOME/.peeky/venice}"
command -v brew >/dev/null || { echo "Homebrew is required (brew install protobuf abseil)" >&2; exit 1; }
PB="$(brew --prefix protobuf)"; AB="$(brew --prefix abseil)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
for f in java_generator.cpp java_generator.h java_plugin.cpp; do
  curl -sfL --max-time 60 -o "$WORK/$f" \
    "https://raw.githubusercontent.com/grpc/grpc-java/v$VERSION/compiler/src/java_plugin/cpp/$f"
done
python3 - "$WORK/java_generator.cpp" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
for pattern, replacement in [
    (r'MixedLower\(method->name\(\)\)', 'MixedLower(std::string(method->name()))'),
    (r'ToAllUpperCase\(method->name\(\)\)', 'ToAllUpperCase(std::string(method->name()))'),
    (r'"get_" \+ method->name\(\) \+', '"get_" + std::string(method->name()) +'),
    (r'\{"service", service->name\(\)\}', '{"service", std::string(service->name())}'),
    (r'= service->name\(\) \+ "', '= std::string(service->name()) + "'),
    (r'const std::string service_name = service->name\(\);',
     'const std::string service_name = std::string(service->name());'),
    (r'return service->name\(\) \+ "Grpc";', 'return std::string(service->name()) + "Grpc";'),
]:
    text = re.sub(pattern, replacement, text)
open(path, "w").write(text)
PY
mkdir -p "$OUT"
clang++ -std=c++17 -O2 -Wno-deprecated-declarations -I"$PB/include" -I"$AB/include" \
  -o "$OUT/protoc-gen-grpc-java" "$WORK/java_generator.cpp" "$WORK/java_plugin.cpp" \
  -L"$PB/lib" -lprotoc -lprotobuf -L"$AB/lib" \
  $(ls "$AB"/lib/libabsl_*.dylib | grep -v '\.[0-9]*\.dylib$')
cat > "$OUT/native-grpc.init.gradle" <<GRADLE
// Apple Silicon without Rosetta: natively built protoc-gen-grpc-java $VERSION.
allprojects {
  afterEvaluate { project ->
    def protobuf = project.extensions.findByName('protobuf')
    if (protobuf != null) {
      protobuf.plugins { grpc { path = '$OUT/protoc-gen-grpc-java' } }
    }
  }
}
GRADLE
file "$OUT/protoc-gen-grpc-java"
echo "Use: ./gradlew -I $OUT/native-grpc.init.gradle assemble"
