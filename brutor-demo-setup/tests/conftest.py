"""setup.py and verify.py are scripts, not a package: put their directory on
sys.path so `import setup` / `import verify` load them as verify.py does."""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
