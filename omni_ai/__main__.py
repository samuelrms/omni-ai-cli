"""python -m omni_ai: the ai command."""
import sys

from .cli import entry
from .osutils import repair_launcher, utf8_stdio

utf8_stdio()
repair_launcher()
sys.exit(entry())
