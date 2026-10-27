__version__ = "0.1.0"


def add_version(parser):
    """`--version`: `peeky 0.1.0`, from `__version__`, the one place the version is kept."""
    parser.add_argument("--version", action="version", version=f"peeky {__version__}")
