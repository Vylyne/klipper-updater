#!/usr/bin/env python3
"""What is plugged into which port, as the kernel sees it.

    ./scripts/usb_topology.py            # the tree
    ./scripts/usb_topology.py --free     # ...including empty hub ports

Built for the case `/dev/serial/by-id` cannot answer: a CH340 has no unique
serial, so several identical adapters collapse onto indistinguishable names and
the only thing telling them apart is **where they are plugged in**. That is a
topology question, and this prints the topology.

Everything comes from `/sys/bus/usb/devices`, which is the kernel's own view -
no lsusb, no pyusb, no root. `--root` points it at a copied or synthetic tree,
which is how it is tested off-target.

Reading the names: `6-1.6.7.1.3` is bus 6, then the port at each hop down. So
two devices differing only at one position are on the same hub, in different
ports of it - and that is exactly the information a udev rule needs.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

SYSFS = "/sys/bus/usb/devices"

#: Things worth naming when we see them, since the product strings are vague.
KNOWN = {
    ("1a86", "7523"): "CH340 serial (Knomi?)",
    ("1a86", "7522"): "CH340 serial (Knomi?)",
    ("1a86", "55d4"): "CH9102/CH340K serial (Knomi?)",
    ("0483", "df11"): "STM32 in DFU mode",
    ("1d50", "606f"): "Katapult/Klipper CAN",
}


def read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


class Device:
    def __init__(self, root: str, name: str) -> None:
        self.name = name
        self.path = os.path.join(root, name)
        self.vid = read(os.path.join(self.path, "idVendor")) or ""
        self.pid = read(os.path.join(self.path, "idProduct")) or ""
        self.product = read(os.path.join(self.path, "product")) or ""
        self.vendor = read(os.path.join(self.path, "manufacturer")) or ""
        self.serial = read(os.path.join(self.path, "serial")) or ""
        self.speed = read(os.path.join(self.path, "speed")) or ""
        self.ports = int(read(os.path.join(self.path, "maxchild")) or 0)
        self.children: list[Device] = []
        self.ttys: list[str] = []
        self.links: list[str] = []

    @property
    def is_hub(self) -> bool:
        return self.ports > 0

    @property
    def parent_name(self) -> Optional[str]:
        """`6-1.6.7` -> `6-1.6`; `6-1` -> `usb6`; a root hub -> None."""
        if self.name.startswith("usb"):
            return None
        if "." in self.name:
            return self.name.rsplit(".", 1)[0]
        bus = self.name.split("-", 1)[0]
        return f"usb{bus}"

    @property
    def port(self) -> str:
        """The port number on its parent."""
        if "." in self.name:
            return self.name.rsplit(".", 1)[1]
        if "-" in self.name:
            return self.name.split("-", 1)[1]
        return "-"

    def label(self) -> str:
        known = KNOWN.get((self.vid.lower(), self.pid.lower()))
        text = self.product or self.vendor or "?"
        if known:
            text = f"{text}  <- {known}" if self.product else known
        return text


def collect(root: str) -> dict[str, Device]:
    """Every USB device, minus the `:1.0` interface entries."""
    devices: dict[str, Device] = {}
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        sys.exit(f"cannot read {root}: {exc}")
    for name in names:
        if ":" in name:  # an interface, not a device
            continue
        devices[name] = Device(root, name)
    return devices


def attach_ttys(devices: dict[str, Device], tty_root: str, dev_root: str) -> None:
    """Map ttyUSBn back to the USB device, then hang by-id/by-path names on it.

    The tty lives under the *interface* (`6-1.6.7.1.3:1.0/ttyUSB0`), so the owning
    device is the interface's directory name up to the colon.
    """
    try:
        ttys = sorted(os.listdir(tty_root))
    except OSError:
        ttys = []

    owner: dict[str, str] = {}
    for tty in ttys:
        link = os.path.join(tty_root, tty, "device")
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        # .../6-1.6.7.1.3:1.0  ->  6-1.6.7.1.3
        interface = os.path.basename(target)
        if ":" not in interface:
            interface = os.path.basename(os.path.dirname(target))
        device = interface.split(":", 1)[0]
        if device in devices:
            devices[device].ttys.append(tty)
            owner[tty] = device

    # The friendly names, so the tree shows what you would actually put in a config.
    for kind in ("by-id", "by-path"):
        directory = os.path.join(dev_root, kind)
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            target = os.path.basename(os.path.realpath(os.path.join(directory, entry)))
            device = owner.get(target)
            if device:
                devices[device].links.append(f"{kind}/{entry}")


def render(dev: Device, devices: dict[str, Device], show_free: bool, prefix="") -> None:
    kids = {d.port: d for d in dev.children}

    # Every child is shown, ALWAYS - free slots are merely added on top. Listing
    # only ports 1..maxchild would silently hide any device whose port number
    # disagrees with what the hub claims, and a device this cannot see is the one
    # failure a topology tool must not have.
    slots = sorted(kids, key=lambda p: (len(p), p))
    if show_free and dev.is_hub:
        every = [str(i) for i in range(1, dev.ports + 1)]
        slots = sorted(set(slots) | set(every), key=lambda p: (len(p), p))

    for index, slot in enumerate(slots):
        last = index == len(slots) - 1
        stem = "`-- " if last else "|-- "
        child = kids.get(slot)
        if child is None:
            print(f"{prefix}{stem}port {slot}: -")
            continue

        tag = f"[{child.ports} ports]" if child.is_hub else ""
        print(
            f"{prefix}{stem}port {slot}: {child.name}  "
            f"{child.vid}:{child.pid}  {child.label()} {tag}".rstrip()
        )
        pad = prefix + ("    " if last else "|   ")
        if child.serial:
            print(f"{pad}    serial: {child.serial}")
        for tty in child.ttys:
            print(f"{pad}    tty:    /dev/{tty}")
        for link in child.links:
            print(f"{pad}    {link}")
        render(child, devices, show_free, pad)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=SYSFS, help=f"sysfs USB tree (default {SYSFS})")
    parser.add_argument("--tty-root", default="/sys/class/tty")
    parser.add_argument("--dev-root", default="/dev/serial")
    parser.add_argument(
        "--free", action="store_true", help="show empty hub ports too - where to plug the next one"
    )
    args = parser.parse_args(argv)

    devices = collect(args.root)
    if not devices:
        print(f"no USB devices under {args.root}", file=sys.stderr)
        return 1

    attach_ttys(devices, args.tty_root, args.dev_root)

    for device in devices.values():
        parent = devices.get(device.parent_name or "")
        if parent is not None:
            parent.children.append(device)

    roots = [d for d in devices.values() if d.parent_name is None or d.parent_name not in devices]
    for root in sorted(roots, key=lambda d: d.name):
        tag = f"[{root.ports} ports]" if root.is_hub else ""
        print(f"{root.name}  {root.label()} {tag}".rstrip())
        render(root, devices, args.free)
        print()

    hubs = [d for d in devices.values() if d.is_hub and not d.name.startswith("usb")]
    used = sum(len(h.children) for h in hubs)
    total = sum(h.ports for h in hubs)
    print(f"{len(hubs)} external hub(s), {used}/{total} ports in use")
    return 0


if __name__ == "__main__":
    sys.exit(main())
