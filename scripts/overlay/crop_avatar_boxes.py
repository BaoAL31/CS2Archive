"""Crop named CS2 scoreboard avatars, retaining colored borders.

Usage:
  python scripts/overlay/crop_avatar_boxes.py <scoreboard.png> <output_dir>
"""
from __future__ import annotations
import sys
from pathlib import Path
from PIL import Image

# Validated against Krabeni second-half scoreboard. Coordinates are scoreboard
# image coordinates, not full video coordinates. All boxes are 76x77.
PLAYERS = [
    ("Krabeni", 47), ("heel1n", 131), ("SENER1", 215), ("nako", 299), ("-Mo", 383),
    ("q bby", 587), ("Sbeen", 671), ("wh1temink", 754), ("Aluzy", 839), ("h4rnxy", 923),
]
Y0, WIDTH, HEIGHT = 24, 76, 77

def crop_avatars(source: Path, output: Path) -> None:
    image = Image.open(source)
    output.mkdir(parents=True, exist_ok=True)
    for index, (name, x) in enumerate(PLAYERS, 1):
        image.crop((x, Y0, x + WIDTH, Y0 + HEIGHT)).save(
            output / f"{index:02d}-{name}.png"
        )

if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: crop_avatar_boxes.py scoreboard.png output_dir")
    crop_avatars(Path(sys.argv[1]), Path(sys.argv[2]))
