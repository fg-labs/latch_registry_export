import logging
import sys
from typing import Callable
from typing import List

import defopt

from latch_registry_export.tools.hello import hello

_tools: List[Callable[..., None]] = [
    hello,
]


def setup_logging(level: str = "INFO") -> None:
    """Set up basic logging to print to the console."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s:%(funcName)s:%(lineno)s [%(levelname)s]: %(message)s",
    )


def run() -> None:
    """Set up logging, then hand over to defopt for running command line tools."""
    setup_logging()
    logger = logging.getLogger("latch_registry_export")
    logger.info("Executing: " + " ".join(sys.argv))
    defopt.run(
        funcs=_tools,
        argv=sys.argv[1:],
        version=True,
    )
    logger.info("Finished executing successfully.")
