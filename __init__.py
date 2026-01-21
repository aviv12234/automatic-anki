
# __init__.py
import os, sys
ADDON_DIR = os.path.dirname(__file__)
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)

from .main import init_addon
init_addon()



