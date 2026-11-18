"""Supported platforms and how to build them from the configuration.

Each platform module provides ``parse_settings(name, data)`` (validates its
``[source.<name>]`` section) and ``create(name, settings)`` (returns a ``Platform``).
Modules are imported on demand: a platform's dependencies are only needed when it
is used.
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import KINDS, BackupPlan, DiskSource, Guest, Platform, PlatformError

__all__ = [
    "KINDS",
    "BackupPlan",
    "DiskSource",
    "Guest",
    "Platform",
    "PlatformError",
    "PLATFORMS",
    "create_platform",
    "parse_settings",
]

# type in the configuration -> module (fixed list: never a module name from the config)
PLATFORMS = {
    "proxmox": "antigrafa.platforms.proxmox",
}


def _module(platform_type: str):
    try:
        return importlib.import_module(PLATFORMS[platform_type])
    except KeyError:
        known = ", ".join(sorted(PLATFORMS))
        raise ValueError(f"unknown platform type {platform_type!r} (supported: {known})") from None


def parse_settings(platform_type: str, name: str, data: dict) -> Any:
    """Validate the settings of one source. Raises ``ConfigError`` or ``ValueError``."""
    return _module(platform_type).parse_settings(name, data)


def create_platform(platform_type: str, name: str, settings: Any) -> Platform:
    return _module(platform_type).create(name, settings)
