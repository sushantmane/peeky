#!/usr/bin/env python3
"""Build the Java adapter bundle from a source checkout: `peeky setup-java --output DIR`.

For checkouts and CI, where Peeky may not be installed: it runs this checkout's
peeky.java_setup, and prints the bundle's manifest (adapter.json) as JSON.
"""

import sys
from pathlib import Path

# This checkout's Peeky, ahead of any installed one, so the bundle matches these sources.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from peeky.java_setup import main  # noqa: E402

if __name__ == "__main__":
    main()
