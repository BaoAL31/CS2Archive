"""CLI: scrape prosettings.net CS2 resolution/aspect into .data/pro_video_settings.json"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

from scrapers.prosettings import main

if __name__ == "__main__":
    main()
