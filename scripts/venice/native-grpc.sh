#!/usr/bin/env bash
# Build Venice on Apple Silicon without Rosetta.
# grpc-java publishes only x86_64 macOS protoc-gen-grpc-java binaries. This builds the
# generator source of grpc-java v1.59.1 (Venice's pin) natively against Homebrew protobuf
# and abseil, adapting only string_view -> std::string call sites, then writes a Gradle
# init script that points Venice's protobuf plugin at it. Nothing in the Venice repo changes.
# Inputs are pinned to an immutable commit and SHA-256; any mismatch aborts.
set -euo pipefail
VERSION="1.59.1"
COMMIT="cb82a273489ab3fecbbcf33ec2f0268778552887"  # refs/tags/v1.59.1^{}
SHA_java_generator_cpp="4ec93cdb36398fb797f16e66a295a836a6eca916ec64ec4c183a3ab2236870bb"
SHA_java_generator_h="1da71b5a5abbf4cd092518aa7da48eae9c436209c4bc20839f22a12feb9655b9"
SHA_java_plugin_cpp="34fa60a41a9d2a3c68dbc35c006a7226966f2f39ee262dfd64f6e056157dc3f5"
OUT="${PEEKY_VENICE_DIR:-$HOME/.peeky/venice}"
command -v brew >/dev/null || { echo "Homebrew is required (brew install protobuf abseil)" >&2; exit 1; }
PB="$(brew --prefix protobuf)"; AB="$(brew --prefix abseil)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
for f in java_generator.cpp java_generator.h java_plugin.cpp; do
  curl -sfL --max-time 60 -o "$WORK/$f" \
    "https://raw.githubusercontent.com/grpc/grpc-java/$COMMIT/compiler/src/java_plugin/cpp/$f"
  var="SHA_${f//./_}"
  actual="$(shasum -a 256 "$WORK/$f" | cut -d' ' -f1)"
  [ "$actual" = "${!var}" ] || { echo "SHA-256 mismatch for $f" >&2; exit 1; }
done
python3 - "$WORK/java_generator.cpp" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
expected = [1, 2, 1, 1, 4, 2, 1]  # exact call sites in the pinned v1.59.1 source
for count, (pattern, replacement) in zip(expected, [
    (r'MixedLower\(method->name\(\)\)', 'MixedLower(std::string(method->name()))'),
    (r'ToAllUpperCase\(method->name\(\)\)', 'ToAllUpperCase(std::string(method->name()))'),
    (r'"get_" \+ method->name\(\) \+', '"get_" + std::string(method->name()) +'),
    (r'\{"service", service->name\(\)\}', '{"service", std::string(service->name())}'),
    (r'= service->name\(\) \+ "', '= std::string(service->name()) + "'),
    (r'const std::string service_name = service->name\(\);',
     'const std::string service_name = std::string(service->name());'),
    (r'return service->name\(\) \+ "Grpc";', 'return std::string(service->name()) + "Grpc";'),
]):
    text, found = re.subn(pattern, replacement, text)
    if found != count:
        sys.exit(f"Unexpected patch count for {pattern}: {found} != {count}")
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
