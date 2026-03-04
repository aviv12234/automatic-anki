import os, sys

ADDON_DIR = os.path.dirname(__file__)
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)

# ✅ initialize colorizer add-on
from . import colorizer  # <-- THIS LINE MATTERS

from .main import init_addon
init_addon()


# Enable the Find‑purpose right‑click menu
try:
    from .purpose_finder import register_purpose_context_item
    register_purpose_context_item()
except Exception as e:
    from aqt.utils import showWarning
    showWarning(f"Could not enable Find-purpose feature: {e}")
