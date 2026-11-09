"""Load the repository's scripts as modules for testing."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = load("kodi_addon_schema", "scripts/kodi_addon_schema.py")
get_kodi_addon = load("get_kodi_addon", "files/get_kodi_addon.py")
