"""
MCU classes for klipper-updater - OOP representation of MCU configuration and devices.
Maps to mcus.json schema structure.
"""


class MCUFirmware:
    """Represents firmware config for one target (Klipper or Katapult)."""

    def __init__(self, enabled=True, extra_src=""):
        self.enabled = True  # default to enabled if not specified
        self.extra_src = ""

    def enable(self):
        """Enable this firmware type."""
        self.enabled = True

    def disable(self):
        """Disable this firmware type."""
        self.enabled = False

    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def enabled_str(self) -> str:
        """Return 'true' or 'false' string for JSON serialization."""
        return "True" if self.enabled else "False"


class MCUFirmwareConfig(MCUFirmware):
    """Wrapper to hold katapult/klipper settings with extra_src."""

    def __init__(self, enabled=None, extra_src=""):
        super().__init__(enabled=enabled, extra_src=extra_src)

    def dict_repr(self) -> dict:
        return {
            "installed": self.enabled_str,  # JSON uses lowercase 'true'/'false'
            "extra_src": self.extra_src
        }


class MCU:
    """Represents an individual device with serial and chipset detection."""

    def __init__(self):
        self.serial = ""
        self.chipset = ""

    @classmethod
    def create(cls, path: str) -> "MCU":
        """Parse a device path like usb-katapult_stm32f072xb_12345-if00"""
        mcu = cls()
        
        if not path.startswith("/dev/serial/by-id/usb-"):
            return mcu

        # Remove prefix: /dev/serial/by-id/usb-
        remainder = path[28:]  # len("/dev/serial/by-id/usb-") == 28
        
        # Split into parts: e.g. "katapult_stm32f072xb_12345-if00"
        parts = remainder.split("_", 2)
        
        if len(parts) >= 2:
            mcu.chipset = parts[1]  # stm32f072xb or rp2040 etc.
            
            # Last part is the serial (e.g., "12345-if00")
            if len(parts) == 2:
                mcu.serial = parts[-1]
            else:
                mcu.serial = parts[2]

        return mcu

    def get_klipper_path(self) -> str:
        """Generate expected Klipper device path."""
        return f"/dev/serial/by-id/usb-klipper_{self.chipset}_{self.serial}"

    def get_katapult_path(self) -> str:
        """Generate expected Katapult device path."""
        return f"/dev/serial/by-id/usb-katapult_{self.chipset}_{self.serial}"


class MCUType:
    """Represents a unique MCU configuration group (chipset + build args)."""

    def __init__(self, name: str, family: str = "", chipset: str = ""):
        self.name = name
        self.family = family
        self.chipset = chipset
        self.devices = []  # list of serial strings for this type
        self.katapult = MCUFirmwareConfig()
        self.klipper = MCUFirmwareConfig()

    def add_device(self, serial: str):
        """Add a device serial to this MCU type."""
        if serial not in self.devices:
            self.devices.append(serial)

    def remove_device(self, serial: str) -> bool:
        """Remove a device serial from this MCU type. Returns True if removed."""
        if serial in self.devices:
            self.devices.remove(serial)
            return True
        return False

    @property
    def has_devices(self) -> bool:
        return len(self.devices) > 0

    def to_dict(self) -> dict:
        """Convert this MCUType to a dictionary for JSON serialization."""
        return {
            "chipset": self.chipset,
            "katapult": self.katapult.dict_repr(),
            "klipper": self.klipper.dict_repr(),
            "serials": self.devices.copy()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MCUType":
        """Create an MCUType instance from a dictionary."""
        # Handle both new schema (with nested katapult/klipper objects) 
        # and old schema (where katapult/klipper might be dicts or missing)
        chipset = data.get("chipset", "")
        
        # Check if this is the new format with katapult/klipper as dict-like
        has_katapult_dict = "katapult" in data and isinstance(data["katapult"], dict)
        has_klipper_dict = "klipper" in data and isinstance(data["klipper"], dict)

        katapult_config = MCUFirmwareConfig() if has_katapult_dict else None
        klipper_config = MCUFirmwareConfig() if has_klipper_dict else None
        
        if has_katapult_dict:
            installed_val = data["katapult"].get("installed", "False")
            extra_src_val = data["katapult"].get("extra_src", "")
            katapult_config.enabled = (installed_val == "True" or 
                                      (isinstance(installed_val, str) and installed_val.lower() == "true"))
            katapult_config.extra_src = extra_src_val

        if has_klipper_dict:
            # Klipper config doesn't have enabled flag in typical usage
            klipper_config.enabled = True  # default
            klipper_config.extra_src = data["klipper"].get("extra_src", "")

        mcu_type = cls(name=data.get("name", ""), family="", chipset=chipset)
        
        if katapult_config:
            mcu_type.katapult = katapult_config
        
        if klipper_config:
            mcu_type.klipper = klipper_config
        
        # Add devices/serials
        serials_list = data.get("serials", [])
        if isinstance(serials_list, list):
            for s in serials_list:
                if s and not isinstance(s, dict):  # handle old format arrays of dicts
                    mcu_type.add_device(str(s))

        return mcu_type


# Convenience function to create MCUType from a dict directly (for legacy compatibility)
def load_mcu_type_from_dict(data: dict) -> MCUType | None:
    """Legacy helper - attempts to parse old-style dictionary format."""
    if not isinstance(data, dict):
        return None
    
    name = data.get("name", "")
    chipset = data.get("chipset", "") or ""
    
    # Check for nested objects (new format) vs flat values (old format)
    has_nested = "katapult" in data and isinstance(data["katapult"], dict)
    
    if not has_nested:
        # Old format - try to extract chipset from string like "stm32f072xb_katapult_stm32f072xb_110032..."
        parts = []
        
        if isinstance(data.get("chipset"), str):
            parts.append(data["chipset"])
        
        # Check for old-style extra_src containing serials
        extra_src = data.get("extra_src", "")
        if isinstance(extra_src, str) and "_" in extra_src:
            # Parse embedded serial like "katapult_stm32f072xb_110032000450505539323520-if00"
            parts = [part for part in extra_src.split("_") if len(part) > 6]
        
        chipset_str = "_".join(parts) if parts else ""
    
    return MCUType(name=name, family="", chipset=chipset_str)
