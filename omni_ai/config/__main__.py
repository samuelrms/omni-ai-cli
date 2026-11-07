import sys

from . import main
from ..osutils import utf8_stdio

if __name__ == '__main__':
    utf8_stdio()
    main(sys.argv[1:])
