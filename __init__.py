import os, sys

ADDON_DIR = os.path.dirname(__file__)
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)

# ✅ initialize colorizer add-on
from . import colorizer  # <-- THIS LINE MATTERS

from .main import init_addon
init_addon()