"""Lets you run the hub without installing it: python3 -m hub serve"""

import sys

from .cli import main

sys.exit(main())
