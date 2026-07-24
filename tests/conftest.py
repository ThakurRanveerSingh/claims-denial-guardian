"""
Pytest configuration shared by every test module in this suite.

Makes `src/` importable as a package root (`import agents.sentinel`, and
`agents.investigator`/`agents.orchestrator` etc. in later slices) without
needing a `pip install -e .` step — that's Slice 4's job
(lld-sprint2.md §4.3's pyproject.toml + `[project.scripts]` work, not built
yet). Until then, something has to put `src/` on `sys.path` before test
modules import project code under it; doing it once here, for the whole
test session, avoids every individual test file needing its own sys.path
hack.
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
