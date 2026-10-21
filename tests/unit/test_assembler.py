"""Inline assembly that can read a file is refused before building (#180): the
assembler's `.incbin` and `.include` read a file no dependency file lists. Judged in a
C or C++ translation unit as `-E` gives it, and in a Rust crate's `asm!`, `global_asm!`
and `naked_asm!`; a string outside inline assembly is never judged."""

import pytest

from peeky.assembler import ASSEMBLER_REFUSAL, c_refusal, judged, rust_refusal
from peeky.errors import classify


def c(text, cpp=False):
    refused = c_refusal(text.encode(), cpp)
    return None if refused is None else str(refused)


@pytest.mark.parametrize(
    "code",
    [
        '__asm__(".incbin \\"/abs/data.bin\\"");',
        'void f(void) { asm volatile(".include \\"x.s\\""); }',
        'void f(void) { __asm__ __volatile__ goto(".INCBIN \\"x\\"" :::: out); out:; }',
        '__asm(".section .rodata\\n.inc" "bin \\"x\\"\\n.text");',
        # Escapes spell it as well: `\x2e` and `\056` are the dot.
        '__asm__("\\x2eincbin \\"x\\"");',
        '__asm__("\\056include \\"x\\"");',
        # A macro can spell it from pieces.
        '__asm__(".irpc c,n\\n.incbi\\\\c \\"x\\"\\n.endr");',
        '__asm__(".macro m d\\n.\\\\d \\"x\\"\\n.endm\\nm incbin");',
        # So can an operand where a directive's name is.
        'void f(void) { __asm__(".inc%c0 \\"x\\"" :: "i"(0)); }',
        'void f(void) { __asm__("nop\\n%c0 \\"x\\"" :: "i"(0)); }',
        # A comment in the template counts too: inside asm, a refusal is fine.
        '__asm__("nop # .incbin");',
        # A `)` in a character literal does not end the group.
        'void f(void) { __asm__("nop" :: "r"(\')\') : ".incbin"); }',
    ],
)
def test_c_inline_assembly_that_reads_a_file_is_refused(code):
    refused = c(code)
    assert refused is not None and refused.startswith(ASSEMBLER_REFUSAL), code
    assert "the asm at <unknown>:1" in refused


@pytest.mark.parametrize(
    "code",
    [
        # Strings outside inline assembly are never judged.
        'int printf(const char *, ...); int main(void) { printf(".incbin \\"x\\""); }',
        'const char *s = "asm(\\".incbin\\")"; const char *t = "__asm__(\\".include\\")";',
        "int incbin(int include) { return include; }",
        # Asm labels and ordinary templates.
        'int fopen_(void) __asm("_" "fopen" "$DARWIN_EXTSN");',
        'void f(int *p) { __asm__ volatile("" ::: "memory"); __asm__("mov %0, %1" : "=r"(*p) '
        ': "r"(1)); }',
        'void f(void) { __asm__(".L%=:\\n jmp .L%=\\n movl %%eax, %%ebx" ::); }',
        'void f(void) { __asm__ goto("jmp %l0" :::: done); done:; }',
        # In C, `R"(...)"` is a name and a string, not a raw string.
        'int R; void f(void) { __asm__("nop"); } const char *s = R"(.incbin)";',
    ],
)
def test_other_c_is_not_refused(code):
    assert c(code) is None, code


def test_the_place_is_read_from_the_line_markers():
    output = (
        '# 1 "a.c"\n'
        "int x;\n"
        '# 1 "/sdk/include/blob.h" 1 3\n'
        "\n"
        "\n"
        '__asm__(".incbin \\"x\\"");\n'
        '# 3 "a.c" 2\n'
        "int main(void) { return 0; }\n"
    )
    assert "the asm at /sdk/include/blob.h:3 uses `.incbin`" in c(output)
    # A `#pragma` line holds no asm, whatever its string says.
    assert c('# 1 "a.c"\n#pragma message("__asm__(\\".incbin\\")")\nint x;\n') is None


def test_cpp_raw_strings_and_constant_templates():
    assert "uses `.incbin`" in c('asm(R"x(.incbin "f")x");', cpp=True)
    # A raw string's line that starts with `#` is no directive line.
    assert "uses `.incbin`" in c('asm(R"(\n# .incbin "f"\n)");', cpp=True)
    assert c('const char *s = R"(asm(".incbin"))";', cpp=True) is None
    # Clang's constant-expression templates are not evaluated: refused.
    refused = c('constexpr const char *s() { return "nop"; }\nasm((s()));', cpp=True)
    assert "a template that is not a string literal" in refused
    assert c('void f() { asm volatile("nop"); }', cpp=True) is None


def test_the_refusal_is_unsupported_before_launch():
    refused = c_refusal(b'__asm__(".incbin \\"x\\"");', False)
    assert refused.before_launch is True
    error = classify(refused)
    assert error["code"] == "UNSUPPORTED" and "declared source" in error["hint"], error


MANIFEST = '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n'


def rust(main, **others):
    texts = {"Cargo.toml": MANIFEST, "Cargo.lock": "", "src/main.rs": main, **others}
    rust = ("src/main.rs", *others)
    refused = rust_refusal(texts, rust)
    return None if refused is None else str(refused)


@pytest.mark.parametrize(
    "code",
    [
        'use std::arch::global_asm;\nglobal_asm!(".globl b", ".incbin \\"/abs/f\\"");',
        'fn main() { unsafe { core::arch::asm!(".include \\"x.s\\"") } }',
        'fn main() { unsafe { std::arch::r#asm!(".Incbin \\"x\\"") } }',
        '#[unsafe(naked)] extern "C" fn f() { core::arch::naked_asm!(".incbin \\"x\\"") }',
        'global_asm!(concat!(".inc", "bin \\"x\\""));',
        'global_asm!("\\x2eincbin \\"x\\"");',
        'global_asm!("\\u{2e}include \\"x\\"");',
        'global_asm!(r#".incbin "x""#);',
        'global_asm!("nop\\n\\\n    .incbin \\"x\\"");',
        'global_asm!(stringify!(.incbin "x"));',
        'global_asm!(".irp d, incbin", ".\\\\d \\"x\\"", ".endr");',
        # An operand where a directive's name is: a symbol named `.incbin`.
        'global_asm!("{} \\"x\\"", sym S);',
        'global_asm!(".inc{} \\"x\\"", sym S);',
    ],
)
def test_rust_inline_assembly_that_reads_a_file_is_refused(code):
    refused = rust(code)
    assert refused is not None and refused.startswith(ASSEMBLER_REFUSAL), code
    assert "src/main.rs:" in refused


@pytest.mark.parametrize(
    "code, others",
    [
        # Strings and comments outside inline assembly are never judged.
        ('fn main() { println!(".incbin \\"x\\""); } // .include', {}),
        (
            'use std::arch::asm;\nfn main() { println!(".incbin"); unsafe { asm!("nop") } }',
            {},
        ),
        (
            'global_asm!(".globl {0}", "{0}:", "ret", sym f);\n'
            'fn main() { unsafe { core::arch::asm!("mov {0}, 1", out(reg) _) } }',
            {},
        ),
        ('global_asm!(/* .incbin "x" */ "nop");', {}),
        # A crate whose asm macro is wrapped or renamed, with no directive anywhere.
        (
            'macro_rules! m { ($t:literal) => { core::arch::global_asm!($t); } }\nm!("nop");',
            {"tests/t.rs": 'fn t() { let _ = "include"; }'},
        ),
    ],
)
def test_other_rust_is_not_refused(code, others):
    assert rust(code, **others) is None, code


@pytest.mark.parametrize(
    "code, others, place",
    [
        # A template from a macro's argument: any literal in the crate counts.
        (
            "macro_rules! m { ($t:literal) => { core::arch::global_asm!($t); } }\n"
            'm!(".incbin \\"x\\"");',
            {},
            "src/main.rs:2",
        ),
        (
            "macro_rules! m { ($a:literal, $b:literal) => { core::arch::global_asm!("
            'concat!($a, $b)); } }\nm!(".inc", "bin \\"x\\"");',
            {},
            "src/main.rs:2",
        ),
        # A renamed asm macro.
        ('use core::arch::global_asm as g;\ng!(".incbin \\"x\\"");', {}, "src/main.rs:2"),
        # A macro the caller names.
        (
            "macro_rules! call { ($m:ident, $t:literal) => { $m!($t); } }\n"
            'use core::arch::global_asm;\ncall!(global_asm, ".incbin \\"x\\"");',
            {},
            "src/main.rs:3",
        ),
        # Another declared file's text, by `include_str!`.
        (
            'core::arch::global_asm!(include_str!("blob.rs"));',
            {"src/blob.rs": '.incbin "x"\n'},
            "src/blob.rs:1",
        ),
    ],
)
def test_rust_templates_from_elsewhere_count_any_directive_in_the_crate(code, others, place):
    refused = rust(code, **others)
    assert refused is not None and f"{place} uses `.inc" in refused, refused
    assert "inline assembly may take it" in refused


def test_judged_names_what_reads_a_file():
    assert judged('.incbin "x"') == "uses `.incbin`, which reads a file"
    assert "assembler macro" in judged(".macro m\n.endm")
    assert judged("ldr x0, =my.includes") is None
    assert judged(".section .text\n.globl f") is None
