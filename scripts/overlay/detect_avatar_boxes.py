"""Detect CS2 scoreboard avatar boxes from colored borders."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import cv2
import numpy as np

# HSV hue bands for CS2 border colors: green, orange, pink, yellow, blue.
_COLOR_HUES = ((35, 85), (5, 25), (140, 179), (20, 40), (85, 130))

def detect_avatar_boxes(image: str | Path) -> dict:
    frame = cv2.imread(str(image))
    if frame is None:
        raise FileNotFoundError(image)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, w = frame.shape[:2]
    roi = hsv[:max(90, round(h * .12)), :]
    mask = np.zeros(roi.shape[:2], np.uint8)
    for lo, hi in _COLOR_HUES:
        mask |= cv2.inRange(roi, (lo, 90, 45), (hi, 255, 255))
    # Colored border top/side pixels; remove isolated HUD noise.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    counts = (mask > 0).sum(axis=0)
    active = counts >= max(5, round(roi.shape[0] * .08))
    runs = []
    for x in np.flatnonzero(active):
        if not runs or x > runs[-1][-1] + 1: runs.append([int(x)])
        else: runs[-1].append(int(x))
    runs = [(r[0], r[-1] + 1) for r in runs if 28 <= r[-1]-r[0] <= 60]
    # Merge border fragments belonging to one avatar; select ten scoreboard boxes.
    merged=[]
    for a,b in runs:
        if merged and a-merged[-1][1] <= 5: merged[-1]=(merged[-1][0],b)
        else: merged.append((a,b))
    candidates=[(a,b) for a,b in merged if 35 <= b-a <= 58]
    if len(candidates) != 10:
        raise RuntimeError(f"avatar border detector found {len(candidates)} boxes, expected 10: {candidates}")
    candidates.sort()
    left, right = candidates[:5], candidates[5:]
    y0, y1 = 0, min(roi.shape[0], 70)
    return {"width": w, "height": h, "aspect": f"{w}:{h}", "y0": y0, "y1": y1,
            "LEFT": left, "RIGHT": right}

if __name__ == '__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('image'); args=ap.parse_args()
    print(json.dumps(detect_avatar_boxes(args.image), indent=2))
