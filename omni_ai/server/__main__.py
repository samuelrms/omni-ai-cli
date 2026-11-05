"""python -m omni_ai.server: the `ai serve` command."""
import sys

from . import main

sys.exit(main(sys.argv[1:]))
