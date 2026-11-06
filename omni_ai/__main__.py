"""python -m omni_ai: the ai command."""
import sys

from .cli import entry
from .osutils import repair_launcher

repair_launcher()
sys.exit(entry())
