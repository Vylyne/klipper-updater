#!/usr/bin/env python3
"""Back-compat entry point.

The implementation moved into the ``klipper_updater`` package next to this file.
This shim stays so that muscle memory, cron entries, and anything invoking
``~/klipper-updater/src/updatefw.py`` keep working unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from klipper_updater.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
