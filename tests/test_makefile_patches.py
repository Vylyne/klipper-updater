"""The context manager that temporarily edits klipper's Makefiles.

Restoration correctness matters more than almost anything else here: a
half-restored klipper tree silently affects every later build, not just this one.
"""

from __future__ import annotations

import pytest

from mcu_updater.build import makefile_patches
from mcu_updater.config import MakefilePatch, McuType

ORIGINAL = b"# klipper src makefile\nsrc-y += sched.c\n"


def _mcu(*patches: MakefilePatch) -> McuType:
    mcu = McuType(name="board", chipset="stm32f072xb")
    mcu.fw("klipper").makefile_patches = list(patches)
    return mcu


def _collect(events: list[tuple[str, str]]):
    return lambda stream, line: events.append((stream, line))


def test_line_is_appended_then_restored_byte_identically(paths, fake_root):
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(ORIGINAL)
    mcu = _mcu(MakefilePatch(file="src/Makefile", line="src-y += buffer.c"))

    with makefile_patches(paths, mcu, "klipper", lambda s, line: None):
        inside = makefile.read_bytes()
        assert inside == ORIGINAL + b"src-y += buffer.c\n"

    assert makefile.read_bytes() == ORIGINAL


def test_a_newline_is_added_when_the_file_lacks_a_trailing_one(paths, fake_root):
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(b"src-y += sched.c")  # no trailing \n
    mcu = _mcu(MakefilePatch(file="src/Makefile", line="src-y += buffer.c"))

    with makefile_patches(paths, mcu, "klipper", lambda s, line: None):
        assert makefile.read_bytes() == b"src-y += sched.c\nsrc-y += buffer.c\n"
    assert makefile.read_bytes() == b"src-y += sched.c"


def test_restores_even_when_the_body_raises(paths, fake_root):
    """A failed build must not leave klipper's tree modified."""
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(ORIGINAL)
    mcu = _mcu(MakefilePatch(file="src/Makefile", line="src-y += buffer.c"))

    with pytest.raises(RuntimeError):
        with makefile_patches(paths, mcu, "klipper", lambda s, line: None):
            raise RuntimeError("build blew up")

    assert makefile.read_bytes() == ORIGINAL


def test_an_already_present_line_is_left_alone_not_reverted(paths, fake_root):
    """Left over from an interrupted run.

    We can't tell whether it was ours or hand-written, and deleting a line the
    user added would be worse than leaving a duplicate.
    """
    existing = ORIGINAL + b"src-y += buffer.c\n"
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(existing)
    mcu = _mcu(MakefilePatch(file="src/Makefile", line="src-y += buffer.c"))

    events: list[tuple[str, str]] = []
    with makefile_patches(paths, mcu, "klipper", _collect(events)):
        assert makefile.read_bytes() == existing

    assert makefile.read_bytes() == existing
    assert any("already present" in line for _, line in events)


def test_a_missing_target_warns_and_continues(paths, fake_root):
    mcu = _mcu(MakefilePatch(file="src/nope/Makefile", line="src-y += buffer.c"))
    events: list[tuple[str, str]] = []
    with makefile_patches(paths, mcu, "klipper", _collect(events)):
        pass
    assert [s for s, _ in events] == ["warn"]
    assert "not found" in events[0][1]


def test_multiple_patches_all_applied_and_all_restored(paths, fake_root):
    src = fake_root / "klipper" / "src"
    (src / "stm32").mkdir()
    a, b = src / "Makefile", src / "stm32" / "Makefile"
    a.write_bytes(b"A\n")
    b.write_bytes(b"B\n")
    mcu = _mcu(
        MakefilePatch(file="src/Makefile", line="src-y += one.c"),
        MakefilePatch(file="src/stm32/Makefile", line="src-y += two.c"),
    )

    with makefile_patches(paths, mcu, "klipper", lambda s, line: None):
        assert a.read_bytes() == b"A\nsrc-y += one.c\n"
        assert b.read_bytes() == b"B\nsrc-y += two.c\n"

    assert a.read_bytes() == b"A\n"
    assert b.read_bytes() == b"B\n"


def test_no_patches_configured_is_a_no_op(paths, fake_root):
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(ORIGINAL)
    with makefile_patches(paths, _mcu(), "klipper", lambda s, line: None):
        assert makefile.read_bytes() == ORIGINAL
    assert makefile.read_bytes() == ORIGINAL


def test_dry_run_does_not_touch_the_file(paths, fake_root):
    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(ORIGINAL)
    mcu = _mcu(MakefilePatch(file="src/Makefile", line="src-y += buffer.c"))

    events: list[tuple[str, str]] = []
    with makefile_patches(paths, mcu, "klipper", _collect(events), dry_run=True):
        assert makefile.read_bytes() == ORIGINAL
    assert makefile.read_bytes() == ORIGINAL
    assert any("[dry-run]" in line for _, line in events)


def test_invalid_patches_are_skipped(paths, fake_root):
    (fake_root / "klipper" / "src" / "Makefile").write_bytes(ORIGINAL)
    mcu = _mcu(MakefilePatch(file="", line=""), MakefilePatch(file="src/Makefile", line=""))
    events: list[tuple[str, str]] = []
    with makefile_patches(paths, mcu, "klipper", _collect(events)):
        pass
    assert events == []


def test_patches_are_reverted_when_make_blows_up(paths, settings, fake_root, monkeypatch):
    """The end-to-end version of restore-on-exception, through real build().

    A patched klipper tree that survives a failed build corrupts every *later*
    build too, so revert has to hold even when make dies unexpectedly rather
    than merely exiting non-zero.
    """
    import os

    from mcu_updater import build as build_mod
    from mcu_updater.config import Registry
    from mcu_updater.errors import ToolMissingError

    makefile = fake_root / "klipper" / "src" / "Makefile"
    makefile.write_bytes(ORIGINAL)

    reg = Registry.load(paths)
    mcu = reg.add_type("board", "stm32f072xb")
    mcu.fw("klipper").makefile_patches = [
        MakefilePatch(file="src/Makefile", line="src-y += buffer.c")
    ]
    reg.save(paths)

    os.makedirs(paths.type_dir("board"), exist_ok=True)
    with open(paths.config_file("board", "klipper"), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")

    # Simulate a host with no build-essential. Patched rather than relying on
    # `make` being absent, which is true on the Windows dev box but not on CI.
    def no_make(cmd, **kwargs):
        raise ToolMissingError(f"'{cmd[0]}' was not found.", tool=cmd[0])

    monkeypatch.setattr(build_mod, "run_streamed", no_make)

    settings.dry_run = False
    events: list[tuple[str, str]] = []
    with pytest.raises(ToolMissingError):
        build_mod.build(paths, reg, settings, "board", "klipper", reporter=_collect(events))

    assert makefile.read_bytes() == ORIGINAL, "the patch must be reverted"
    assert any("Temporarily patched" in line for _, line in events)
    assert any("Restored" in line for _, line in events)
