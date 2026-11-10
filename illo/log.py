"""Keep log setup in one place."""

import logging


def configure() -> None:
    """Use plain process logs without request bodies or credentials."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
