"""How the chat looks: welcome box, input box with status bar, compact tool actions, markdown answers, spinner.

Split into submodules: color (ANSI/RGB), text (language/markdown/utils), screen (spinner/View/boxes),
inputbox (prompt_toolkit input). Names are re-exported here so ``omni_ai.ui.<name>`` keeps working.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'vendor'))

from . import color, screen, inputbox
from . import text as _text_mod

from .color import (
    COLOR, TRUECOLOR, BRAND, MODE_COLORS, MARK, LIMIT_WORDS, TOOL_RGB,
    _c, _mix, rgb, hue, rainbow, gradient, badge, shimmer,
    bold, dim, italic, red, green, yellow, blue, magenta, cyan,
)
from .text import (
    SECRET, LANG, TEXT, t, width, short, home_path, markdown, fuzzy, escape,
)
from .screen import (
    LOCK, Spinner, SPINNER, elapsed, out, View, replay,
    attention, set_title, clear_screen, orbit, splash, welcome,
    ORBIT_SLOTS, ORBIT_TOOLS, BIG_TITLE,
)
from .inputbox import SKIP_DIRS, project_files, InputBox
