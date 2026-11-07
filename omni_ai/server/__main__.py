"""python -m omni_ai.server: the `ai serve` command."""
import sys

from . import main
from ..osutils import utf8_stdio

utf8_stdio()
sys.exit(main(sys.argv[1:]))
