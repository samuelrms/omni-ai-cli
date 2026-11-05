"""Makes `python -m unittest tests.test_x` find the isolation module the same way `discover -s tests` does."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import isolation  # noqa: E402,F401
