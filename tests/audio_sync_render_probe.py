"""Read-only probe of retained source audio using the render sidecar."""
import json
from pathlib import Path

from cs2archive.audio_sync import measure_audio_drift


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    jobs = [
        ("aurora-vs-vitality-m2-cache", "ZywOo", "76561198113666193",
         "2398754-aurora-vs-vitality-esl-pro-league-season-24"),
        ("mouz-vs-spirit-m2-mirage", "donk", "76561198386265483",
         "2398755-mouz-vs-spirit-esl-pro-league-season-24"),
    ]
    for stem, player, steam, demo_folder in jobs:
        folder = root / "renders" / f"pov-{stem}_{player}"
        sidecar = json.loads((folder / "combined.round_offsets.json").read_text())
        demo = root / "demos/hltv" / demo_folder / f"{stem}.dem"
        intercept, slope, marks = measure_audio_drift(
            folder / "combined.mp4", demo, steam,
            {int(k): v for k, v in sidecar["round_offsets"].items()},
            {int(k): tuple(v) for k, v in sidecar["per_round_ticks"].items()},
        )
        print(json.dumps(dict(player=player, intercept=intercept,
                              slope=slope, marks=marks)), flush=True)
