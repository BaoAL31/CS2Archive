"""One output filename convention for the renderer and its pending scanner."""
from pathlib import Path


def short_output_path(out_dir: Path, short: dict, name: str | None = None) -> Path:
    if name:
        base = name
    else:
        st = short["short_type"]
        nick, tick = short.get("pov_nick", "unknown"), short.get("start_tick", 0)
        if st in ("4k", "punch_up"):
            base = f"{len(short.get('kill_ticks', []))}k_multikill-{nick}-t{tick}"
        elif st == "clutch":
            base = f"{short.get('clutch_initial_count', 'XvX')}_clutch-{nick}-t{tick}"
        else:
            base = f"{st}-{nick}-t{tick}"
        if short.get("punch_up_tags"):
            base += "_" + "_".join(short["punch_up_tags"])
    return out_dir / f"{base}.mp4"
