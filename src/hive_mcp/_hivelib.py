"""The only place that imports apyhiveapi.

pyhive-integration 1.0.x replaced sys.excepthook on import with a handler that
logged the crashing frame's local variables (which can include the Hive
password). 2.0 no longer does, but the original hook is still put back straight
away in case a later release brings it back.
"""

import sys

_original_excepthook = sys.excepthook

from apyhiveapi import Auth, Hive  # noqa: E402
from apyhiveapi.helper import hive_exceptions  # noqa: E402

sys.excepthook = _original_excepthook

__all__ = ["Auth", "Hive", "hive_exceptions"]
