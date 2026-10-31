#!/bin/bash
# Checks an image that holds Peeky, as the study's containers run: no network, SYS_PTRACE and
# an unconfined seccomp profile. `peeky --version`, then a probe of a tiny Python script, C
# program (clang), dependency-free Rust crate and Java class, each of which must read
# total = 42.
#
#   harness/verify_peeky_image.sh <image>
set -euo pipefail
image=${1:?usage: verify_peeky_image.sh <image>}
docker run --rm -i --network none --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
  --memory 6g --entrypoint bash "$image" -s <<'EOF'
set -u
status=0
check() {  # check <label> <command...>: the probe's table must have `total ... 42`.
  local label=$1; shift
  local out
  out=$(cd /w && "$@" 2>&1)
  if printf '%s\n' "$out" | grep -Eq '[[:space:]]total[[:space:]]+[A-Za-z0-9]+[[:space:]]+42([[:space:]]|$)'; then
    echo "PASS $label"
  else
    echo "FAIL $label"; printf '%s\n' "$out" | tail -15 | sed 's/^/  | /'; status=1
    return 1
  fi
}
echo "$(peeky --version) (sha $(cat /opt/peeky/SHA))"
mkdir -p /w/c /w/r/src /w/j/demo
printf 'def f(n):\n    total = n * 2\n    return total\n\nprint(f(21))\n' > /w/t.py
printf '#include <stdio.h>\nint main(void) {\n    int n = 21;\n    int total = n * 2;\n    printf("%%d\\n", total);\n    return 0;\n}\n' > /w/c/t.c
printf '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n' > /w/r/Cargo.toml
printf 'fn main() {\n    let n: i32 = 21;\n    let total = n * 2;\n    println!("{}", total);\n}\n' > /w/r/src/main.rs
(cd /w/r && cargo generate-lockfile --offline -q)
printf 'package demo;\n\npublic class T {\n    public static void main(String[] args) {\n        int n = 21;\n        int total = n * 2;\n        System.out.println(total);\n    }\n}\n' > /w/j/demo/T.java
printf '{"sources": ["demo/T.java"], "main_class": "demo.T"}\n' > /w/j/t.json
check python peeky probe t.py --at 3 --capture total
check c peeky probe c/t.c --at 5 --capture total
check rust peeky probe r --at src/main.rs:4 --capture total
before=$status
if ! check java peeky probe j --config j/t.json --at demo/T.java:7 --capture total; then
  # The adapter bundle runs on the JVM `peeky setup-java` ran with (/usr/bin/java, JDK 21); an
  # image whose PATH puts an older JDK first needs the target config's "java" to name it.
  printf '{"sources": ["demo/T.java"], "main_class": "demo.T", "java": "/usr/bin/java"}\n' > /w/j/t2.json
  if check java-with-config-java peeky probe j --config j/t2.json --at demo/T.java:7 --capture total; then
    echo "WARN java: only with \"java\": \"/usr/bin/java\" in the target config; java on PATH is $(readlink -f "$(command -v java)")"
    status=$before
  fi
fi
exit $status
EOF
