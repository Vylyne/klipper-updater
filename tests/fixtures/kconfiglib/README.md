# Vendored kconfiglib, for tests only

A copy of the `kconfiglib.py` that Klipper vendors at `lib/kconfiglib/`, taken
from `Klipper3d/klipper@master`. ISC licensed; `LICENSE.txt` is the upstream
notice and travels with it.

**This is a test fixture and nothing imports it at runtime.** The real thing is
always loaded from the firmware tree being configured, because Klipper's copy is
locally patched and a different one would silently disagree with the `Kconfig`
files it is parsing.

It is vendored rather than stubbed because the two failure modes worth testing
only exist against the genuine library:

* **Per-tree module identity.** Klipper and Katapult each vendor their own copy,
  so loading both yields two distinct module objects whose sentinels (`MENU`,
  `BOOL`, `STRING`, ...) are *different objects*. Comparing one tree's node kind
  against the other's constant silently returns False and every node renders as
  "unknown", with no error anywhere. A stub with shared constants cannot exhibit
  that, so a test using one would pass while the real thing broke.
* **Dependency evaluation.** `visible`, `assignable` and `range` come out of
  kconfiglib's own expression evaluation. Reimplementing that in a fake would be
  testing the fake.

To refresh: re-download from the same path and re-run the suite.
