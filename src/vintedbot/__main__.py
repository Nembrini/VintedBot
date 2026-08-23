"""Allow running the CLI as ``python -m vintedbot``."""

import sys

from vintedbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
