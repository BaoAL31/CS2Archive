"""Download missing matches - no state file. Run parallel to upload."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrapers.hltv_acquire import acquire_match

MISSING = [
    ("2394981", "aurora-vs-g2-iem-cologne-major-2026"),
    ("2394982", "falcons-vs-monte-iem-cologne-major-2026"),
    ("2394989", "betboom-vs-vitality-iem-cologne-major-2026"),
    ("2394990", "aurora-vs-9z-iem-cologne-major-2026"),
    ("2394991", "natus-vincere-vs-falcons-iem-cologne-major-2026"),
    ("2394992", "g2-vs-legacy-iem-cologne-major-2026"),
    ("2394993", "9z-vs-the-mongolz-iem-cologne-major-2026"),
    ("2394994", "betboom-vs-fut-iem-cologne-major-2026"),
    ("2394995", "natus-vincere-vs-g2-iem-cologne-major-2026"),
    ("2394999", "falcons-vs-vitality-iem-cologne-major-2026"),
]

for mid, slug in MISSING:
    url = f"https://www.hltv.org/matches/{mid}/{slug}"
    print(f"\n=== [{mid}] {slug} ===")
    r = acquire_match(url, force=True, headless=False)
    if r.error:
        print(f"  FAIL: {r.error}")
    else:
        print(f"  OK")
