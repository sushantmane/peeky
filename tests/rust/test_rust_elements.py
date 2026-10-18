"""Rust containers of every scalar element type, read from memory and checked against
the program's own `{:?}` output; and Peeky's float formatting against rustc's."""

import json
import os
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from peeky.cli import main
from peeky.rust_symbols import rust_float_debug

ROOT = Path(__file__).resolve().parents[2]
NAMES = (
    "v_u8",
    "v_i8",
    "v_u16",
    "v_i16",
    "v_u32",
    "v_i32",
    "v_u64",
    "v_i64",
    "v_usize",
    "v_isize",
    "v_bool",
    "v_f32",
    "v_f64",
)
PROGRAM = """fn params(a: &[f32], b: &Vec<u16>, c: &mut [i8], d: &[bool], e: &mut Vec<u64>) -> usize {
    c[0] = -128;
    e.push(1);
    a.len() + b.len() + c.len() + d.len() + e.len() // PARAMS
}

fn main() {
    let v_u8: Vec<u8> = vec![0, 1, 255];
    let v_i8: Vec<i8> = vec![-128, 0, 127];
    let v_u16: Vec<u16> = vec![65535, 7];
    let v_i16: Vec<i16> = vec![-32768, 300];
    let v_u32: Vec<u32> = vec![4294967295];
    let v_i32: Vec<i32> = vec![-2147483648, 5];
    let v_u64: Vec<u64> = vec![18446744073709551615, 0];
    let v_i64: Vec<i64> = vec![-9223372036854775808];
    let v_usize: Vec<usize> = vec![3, 2, 1];
    let v_isize: Vec<isize> = vec![-1];
    let v_bool: Vec<bool> = vec![true, false, true];
    let v_f32: Vec<f32> = vec![
        0.1, -0.0, 1e-7, 3.4028235e38, f32::NAN, f32::INFINITY, 16777217.0, 1e16, 0.000244140625,
    ];
    let v_f64: Vec<f64> = vec![0.1 + 0.2, 1e15, 1e16, 5e-324, -1.5, f64::NEG_INFINITY, 2.5e-5];
    let mut c = v_i8.clone();
    let mut e = v_u64.clone();
    let n = params(&v_f32, &v_u16, &mut c, &v_bool, &mut e);
    println!("v_u8={:?}", v_u8); // OBSERVE
    println!("v_i8={:?}", v_i8);
    println!("v_u16={:?}", v_u16);
    println!("v_i16={:?}", v_i16);
    println!("v_u32={:?}", v_u32);
    println!("v_i32={:?}", v_i32);
    println!("v_u64={:?}", v_u64);
    println!("v_i64={:?}", v_i64);
    println!("v_usize={:?}", v_usize);
    println!("v_isize={:?}", v_isize);
    println!("v_bool={:?}", v_bool);
    println!("v_f32={:?}", v_f32);
    println!("v_f64={:?}", v_f64);
    println!("c={:?}", c);
    println!("e={:?}", e);
    println!("n={n}");
}
"""


@pytest.fixture
def crate(tmp_path, monkeypatch):
    names = ("CARGO", "RUSTC", "DAP", "DWARFDUMP", "LINKER")
    if not all(os.environ.get("PEEKY_RUST_" + name) for name in names):
        pytest.skip("set PEEKY_RUST_* tool paths to enable Rust conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "crate"
    shutil.copytree(ROOT / "examples/rust", root, ignore=shutil.ignore_patterns("target"))
    (root / "src/main.rs").write_text(PROGRAM)
    return root


def probe(capsys, crate, marker, selectors):
    at = next(i for i, text in enumerate(PROGRAM.splitlines(), 1) if f"// {marker}" in text)
    code = main(
        [
            "probe",
            str(crate),
            "--at",
            f"src/main.rs:{at}",
            "--capture",
            ",".join(selectors),
            "--json",
            "--quiet",
            "--no-save",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    return {row["selector"]: row for row in result["rows"]}, result["output"]


def printed(output):
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


@pytest.mark.timeout(300)
def test_every_element_type_reads_as_the_program_prints_it(crate, capsys):
    rows, output = probe(capsys, crate, "OBSERVE", NAMES)
    expected = printed(output)
    readable = all(row.get("view") == "memory" for row in rows.values())
    if os.environ.get("PEEKY_EXPECT_READ_MEMORY"):
        assert readable, {name: row.get("value") for name, row in rows.items()}
    if readable:
        assert {name: rows[name]["value"] for name in NAMES} == {
            name: expected[name] for name in NAMES
        }


@pytest.mark.timeout(300)
def test_reference_parameters_of_every_kind_read_their_elements(crate, capsys):
    rows, output = probe(capsys, crate, "PARAMS", ("a", "b", "c", "d", "e", "e[2]", "a[6]"))
    expected = printed(output)
    if os.environ.get("PEEKY_EXPECT_READ_MEMORY") or rows["a"].get("view") == "memory":
        assert rows["a"]["value"] == expected["v_f32"] and rows["b"]["value"] == expected["v_u16"]
        assert rows["c"]["value"] == expected["c"] and rows["e"]["value"] == expected["e"]
        assert rows["d"]["value"] == expected["v_bool"]
        assert (rows["e[2]"]["type"], rows["e[2]"]["value"]) == ("u64", "1")
        assert (rows["a[6]"]["type"], rows["a[6]"]["value"]) == ("f32", "16777216.0")


FORMATTER = """use std::io::BufRead;
fn main() {
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap();
        let (bits, hex) = line.split_once(' ').unwrap();
        if bits == "32" {
            println!("{:?}", f32::from_bits(u32::from_str_radix(hex, 16).unwrap()));
        } else {
            println!("{:?}", f64::from_bits(u64::from_str_radix(hex, 16).unwrap()));
        }
    }
}
"""


@pytest.mark.timeout(300)
def test_float_formatting_matches_rustc(tmp_path):
    rustc, linker = os.environ.get("PEEKY_RUST_RUSTC"), os.environ.get("PEEKY_RUST_LINKER")
    if not (rustc and linker):
        pytest.skip("set PEEKY_RUST_RUSTC and PEEKY_RUST_LINKER")
    source = tmp_path / "floats.rs"
    source.write_text(FORMATTER)
    binary = tmp_path / "floats"
    subprocess.run(
        [rustc, "-O", "-C", f"linker={linker}", "-o", str(binary), str(source)], check=True
    )
    rng = random.Random(61)
    cases = []
    for bits, exponent_bits in ((32, 8), (64, 11)):
        mantissa = bits - 1 - exponent_bits
        edges = [0, 1, 2]
        for exponent in range(0, (1 << exponent_bits) - 1, 1 if bits == 32 else 7):
            edges += [exponent << mantissa, (exponent << mantissa) | ((1 << mantissa) - 1)]
        pack, word = ("<f", "<I") if bits == 32 else ("<d", "<Q")
        ties = [
            struct.unpack(
                word, struct.pack(pack, rng.randint(-(10**7), 10**7) / 2 ** rng.randint(0, 12))
            )[0]
            for _ in range(2000)
        ]
        randoms = [rng.getrandbits(bits) for _ in range(6000)]
        for raw in edges + ties + randoms:
            cases.append((bits, raw, raw | (1 << (bits - 1))))
    lines = [(bits, raw) for bits, a, b in cases for raw in (a, b)]
    out = subprocess.run(
        [str(binary)],
        input="".join(f"{bits} {raw:x}\n" for bits, raw in lines),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    mismatches = []
    for (bits, raw), expected in zip(lines, out, strict=True):
        pack, word = ("<f", "<I") if bits == 32 else ("<d", "<Q")
        value = struct.unpack(pack, struct.pack(word, raw))[0]
        if rust_float_debug(value, bits) != expected:
            mismatches.append((bits, hex(raw), expected, rust_float_debug(value, bits)))
    assert not mismatches, mismatches[:10]
