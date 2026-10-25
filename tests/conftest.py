import importlib.util
import sys
from pathlib import Path

import discord.ext

# Test the working tree, not the copy installed in site-packages: both share the
# `discord.ext` namespace, so register the package from source before any test imports it.
_pkg = Path(__file__).resolve().parent.parent / "discord" / "ext" / "voice_recv"
_spec = importlib.util.spec_from_file_location(
    "discord.ext.voice_recv", _pkg / "__init__.py", submodule_search_locations=[str(_pkg)]
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["discord.ext.voice_recv"] = _mod
_spec.loader.exec_module(_mod)
discord.ext.voice_recv = _mod
