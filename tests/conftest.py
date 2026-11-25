"""Load the integration's pure modules without Home Assistant installed.

``coordinator`` only needs ``homeassistant.core.HomeAssistant`` / ``callback``;
the package ``__init__`` (config entries, voluptuous) is skipped by registering
a bare package module that points at the source directory.
"""
import importlib
from pathlib import Path
import sys
import types

_PKG_DIR = Path(__file__).parents[1] / "custom_components" / "tesira_forte"

if "homeassistant.core" not in sys.modules:
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    ha.core = core
    sys.modules.setdefault("homeassistant", ha)
    sys.modules["homeassistant.core"] = core

if "tesira_forte" not in sys.modules:
    pkg = types.ModuleType("tesira_forte")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["tesira_forte"] = pkg


def load(name: str):
    return importlib.import_module(f"tesira_forte.{name}")
