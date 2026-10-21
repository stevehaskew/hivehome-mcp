"""The only place that imports apyhiveapi.

Importing apyhiveapi.hive replaces sys.excepthook with a handler that logs the
crashing frame's local variables (which can include the Hive password), so the
original hook is put back straight away.
"""

import sys

_original_excepthook = sys.excepthook

from apyhiveapi import Auth, Hive  # noqa: E402
from apyhiveapi.helper import hive_exceptions  # noqa: E402

sys.excepthook = _original_excepthook

__all__ = ["Auth", "Hive", "hive_exceptions"]
