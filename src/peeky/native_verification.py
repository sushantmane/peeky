"""C and C++ verification using the shared fresh-build integrity lifecycle."""

from peeky.compiled_verification import CompiledVerification


class NativeVerification(CompiledVerification):
    @property
    def language(self):
        return self.target.language
