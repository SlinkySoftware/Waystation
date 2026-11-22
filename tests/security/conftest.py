"""The security corpora share ``hostile`` (archive and document builders): make it importable (pytest runs with
``--import-mode=importlib``, which does not put test directories on ``sys.path``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
