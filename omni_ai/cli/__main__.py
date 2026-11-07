import sys

from . import entry
from ..osutils import utf8_stdio

if __name__ == '__main__':
    utf8_stdio()
    sys.exit(entry())
