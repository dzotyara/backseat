"""One log format for every entry point."""

import logging
from collections.abc import Iterable


def setup_logging(level: str, *, quiet: Iterable[str] = ()) -> None:
    """Log to stderr at `level`; the `quiet` loggers report only warnings and errors."""
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)
