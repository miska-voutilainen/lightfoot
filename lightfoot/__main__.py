"""`python -m lightfoot`: the same as the `lightfoot` command."""

import sys

from .cli import main

sys.exit(main())
