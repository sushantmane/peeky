"""The premise of the owned-local proof's macro-line rule (rust_mir.may_invoke_macro):
rustc puts the code of a macro from another crate only on a line where a macro
invocation starts. Checked per toolchain by building the same program with and without
collapsing macro debuginfo: the instructions are identical, so every instruction whose
collapsed location is in main.rs but whose own location is elsewhere is collapsed macro
code, and its line must be one may_invoke_macro accepts."""

import os
import re
import subprocess

import pytest

from peeky.rust_mir import may_invoke_macro

PROGRAM = 'use std::collections::HashMap;\nuse std::ops::AddAssign;\n\nmacro_rules! local_vec { ($($x:expr),*) => { vec![$($x),*] }; }\nmacro_rules! two_lines {\n    ($a:expr) => {\n        $a + 1\n    };\n}\n\n#[derive(Debug, Clone, Default, PartialEq)]\nstruct P {\n    x: i64,\n    v: Vec<i32>,\n}\n\nstruct W(i64);\nimpl AddAssign for W {\n    fn add_assign(&mut self, o: W) {\n        self.0 += o.0;\n    }\n}\n\nfn parse(s: &str) -> Result<i64, std::num::ParseIntError> {\n    let v = s.trim().parse::<i64>()?;\n    Ok(v)\n}\n\nfn desugar(n: usize) -> usize {\n    let mut acc = Vec::new();\n    for i in 0..n {\n        acc.push(i);\n    }\n    let r = 1..=n;\n    let total: usize = r.sum();\n    let mut w = W(1);\n    w += W(2);\n    let s = String::from("abc");\n    let len = s.len();\n    let t = &s[1..];\n    let c = || acc.len() + t.len();\n    let k = c();\n    let m: HashMap<i32, String> = HashMap::new();\n    let got = m.get(&1).cloned().unwrap_or_default();\n    let p = P::default();\n    let q = p.clone();\n    let eq = p == q;\n    let lv = local_vec![1, 2, 3];\n    let tl = two_lines!(5);\n    let cc = concat!("a", "b");\n    let ln = line!();\n    let fmt = format!("{}{}", cc, ln);\n    let st = String::new(); let u = std::\n        vec![1u8];\n    let mv = vec![\n        1,\n        2,\n    ];\n    let arr = [1, 2, 3];\n    let sl = &arr[..];\n    let idx = sl[1];\n    let bx = Box::new(5);\n    let deref = *bx;\n    let opt = Some(vec![1]);\n    if let Some(ref inner) = opt { let _ = inner.len(); }\n    let it: Vec<i32> = lv.iter().map(|x| x * 2).collect();\n    let mut sorted = it.clone();\n    sorted.sort();\n    let joined = sorted.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(",");\n    assert!(joined.len() > 0);\n    debug_assert_eq!(1, 1);\n    let r2 = parse("42").unwrap_or(0);\n    let lifetimes: &\'static str = "x";\n    let ch = \'!\';\n    let raw = r#"a */ b"#;\n    let e = env!("PEEKY_PROBE_ENV");\n    let words: Vec<String> = "a b".split(\' \').map(String::from).collect();\n    let mut owned = String::new();\n    owned += "x";\n    let dropped = vec![9];\n    drop(dropped);\n    w.0 as usize + (total + len + k + got.len() + eq as usize + tl as usize + fmt.len() + u.len() + st.len()\n        + mv.len() + idx as usize + deref as usize + it.len() + r2 as usize + lifetimes.len()\n        + ch.len_utf8() + raw.len() + e.len() + words.len() + owned.len())\n}\n\nasync fn later(v: Vec<i32>) -> usize {\n    v.len()\n}\n\nmod módulo {\n    pub use std::vec;\n}\n\nextern "C" fn sink(v: Vec<u8>) -> usize {\n    v.len()\n}\n\nfn tricky() -> usize {\n    // a lone carriage return is no line break for rustc:\r(old note)\n    let a = 2;\n    let b = 6; let u = std::\n        /* see /* nested */ note */ vec![sink(vec![1]); a];\n    let w = crate::\n        módulo::vec![1u8];\n    b + u.len() + w.len()\n}\n\nfn main() {\n    println!("{}", tricky());\n    println!("{}", desugar(3)); // MAIN\n    let _f = later(vec![1]);\n    let mut p = P::default();\n    p.v.push(1);\n    println!("{:?} {}", p, p.x);\n}\n'

_META = re.compile(r"^(![0-9]+) = (?:distinct )?!(\w+)\((.*)\)\s*$")
_FIELDS = {
    key: re.compile(rf'(?:^|, ){key}: ("(?:[^"\\]|\\.)*"|![0-9]+|[0-9]+)')
    for key in ("line", "scope", "file", "filename")
}


def _ir(path):
    """(metadata, [(function, [!dbg or None per instruction])])."""
    meta, functions, current = {}, [], None
    for raw in path.read_text().split("\n"):
        if match := _META.match(raw):
            fields = {
                key: (found.group(1).strip('"') if (found := pattern.search(match[3])) else None)
                for key, pattern in _FIELDS.items()
            }
            meta[match[1]] = (match[2], fields)
        elif raw.startswith("define "):
            current = (raw.split("@", 1)[1].split("(", 1)[0], [])
            functions.append(current)
        elif raw.startswith("}"):
            current = None
        elif (
            current is not None
            and raw.startswith("  ")
            and not raw.lstrip().startswith(("#dbg", ";"))
        ):
            found = re.search(r"!dbg (![0-9]+)", raw)
            current[1].append(found.group(1) if found else None)
    return meta, functions


def _location(meta, reference):
    if reference is None:
        return None
    _, fields = meta[reference]
    scope = fields["scope"]
    for _ in range(256):
        kind, scoped = meta.get(scope, (None, {}))
        if kind is None:
            return None
        if kind in ("DISubprogram", "DILexicalBlock", "DILexicalBlockFile") and scoped["file"]:
            return meta[scoped["file"]][1]["filename"], int(fields["line"])
        scope = scoped["scope"]
    return None


@pytest.mark.timeout(300)
def test_collapsed_macro_code_is_only_on_lines_that_can_invoke_a_macro(tmp_path):
    rustc = os.environ.get("PEEKY_RUST_RUSTC")
    if not rustc:
        pytest.skip("set PEEKY_RUST_RUSTC")
    source = tmp_path / "main.rs"
    source.write_bytes(PROGRAM.encode())
    env = {**os.environ, "RUSTC_BOOTSTRAP": "1", "PEEKY_PROBE_ENV": "x"}
    builds = {}
    for collapse in ("yes", "no"):
        output = tmp_path / f"{collapse}.ll"
        subprocess.run(
            [
                rustc,
                "-g",
                "-Copt-level=0",
                "-Zmir-opt-level=0",
                "--edition",
                "2021",
                "--crate-type",
                "bin",
                "--emit=llvm-ir",
                f"-Ccollapse-macro-debuginfo={collapse}",
                "-o",
                str(output),
                str(source),
            ],
            check=True,
            env=env,
            capture_output=True,
            timeout=240,
        )
        builds[collapse] = _ir(output)
    (meta, collapsed), (own_meta, own) = builds["yes"], builds["no"]
    assert [name for name, _ in collapsed] == [name for name, _ in own]
    lines, paired = set(), 0
    for (name, left), (_, right) in zip(collapsed, own):
        assert len(left) == len(right), name
        for a, b in zip(left, right):
            here, there = _location(meta, a), _location(own_meta, b)
            if here and there:
                paired += 1
                if here[0].endswith("main.rs") and not there[0].endswith("main.rs"):
                    lines.add(here[1])
    assert paired > 1000 and len(lines) >= 8, (paired, lines)
    missed = sorted(line for line in lines if not may_invoke_macro(PROGRAM, line))
    text = PROGRAM.split("\n")
    assert not missed, [(line, text[line - 1]) for line in missed]
    # The path split over two lines collapses onto the line where it starts.
    start = next(i for i, t in enumerate(text, 1) if t.endswith("let u = std::"))
    assert start in lines and "vec!" not in text[start - 1]
