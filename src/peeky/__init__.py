__version__ = "0.2.0"


def add_version(parser):
    """`--version`: `peeky 0.2.0`, from `__version__`, the one place the version is kept."""
    parser.add_argument("--version", action="version", version=f"peeky {__version__}")


def state_home():
    """Peeky's state directory: $PEEKY_STATE_DIR, else ~/.peeky. Absolute but not
    symlink-resolved, so detached children with another working directory agree."""
    import os
    from pathlib import Path

    return Path(os.path.abspath(os.environ.get("PEEKY_STATE_DIR") or str(Path.home() / ".peeky")))
