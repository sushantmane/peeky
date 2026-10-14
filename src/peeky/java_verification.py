"""Java verification using the shared fresh-build integrity lifecycle."""

from peeky.compiled_verification import CompiledVerification


class JavaVerification(CompiledVerification):
    language = "java"
