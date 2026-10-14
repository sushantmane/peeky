"""C verification using the shared fresh-build integrity lifecycle."""

from peeky.compiled_verification import CompiledVerification


class NativeVerification(CompiledVerification):
    language = "c"
