"""Applies the test-only GCC survey hatch (tests/cpp_gcc_survey.py) when
PEEKY_TEST_CPP_GCC=1; without it, nothing here changes any test."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cpp_gcc_survey  # noqa: E402

cpp_gcc_survey.apply()


@pytest.fixture(autouse=True)
def _cpp_gcc_refusal_checked(request, monkeypatch):
    # test_cpp_gcc checks the refusal itself.
    if request.module.__name__.endswith("test_cpp_gcc"):
        monkeypatch.setattr(cpp_gcc_survey, "ENABLED", False)
