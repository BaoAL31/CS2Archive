"""Capture Repeek last-30 cards from an already-open FACEIT room page.

Repeek is installed in ``.sessions/faceit``. Demo download uses
``~/.chrome-debug`` (seeded from the main Chrome profile), so we copy
the extension into that debug profile before Chrome launches.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

REPEEK_ID = "mokknliiomknodkdmpcellamkopbdmao"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FACEIT_SESSION = PROJECT_ROOT / ".sessions" / "faceit"
DEBUG_PROFILE = Path.home() / ".chrome-debug"
STRIPS_ROOT = PROJECT_ROOT / "renders" / "stat-strips"

NEED_PLAYERS = 10
NEED_PER_SIDE = 5
CARD_W_RANGE = (340.0, 450.0)
CARD_H_RANGE = (115.0, 165.0)
COL_W_RANGE = (340.0, 520.0)
COL_H_RANGE = (680.0, 900.0)
MIN_COLUMN_GAP = 120.0
ROW_Y_TOL = 8.0
PNG_MIN_W = 340
PNG_MIN_H = 650
PNG_MIN_VAR = 80.0


class RepeekCaptureError(RuntimeError):
    """Hard fail — FACEIT/Repeek layout changed; do not continue the extract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"[REPEEK_ERROR] {code}: {message}")
        self.payload = {"error": True, "code": code, "message": message}


_LAST30 = re.compile(r"Last\s+\d+\s+matches")


def _flatten_cdp_text(node: dict, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if node.get("nodeType") == 3:
        return node.get("nodeValue") or ""
    parts: list[str] = []
    for child in node.get("children") or []:
        parts.append(_flatten_cdp_text(child, depth + 1))
    for sr in node.get("shadowRoots") or []:
        parts.append(_flatten_cdp_text(sr, depth + 1))
    return "".join(parts)


def _quad_box(client: Any, backend_node_id: int) -> dict | None:
    try:
        model = client.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})
    except Exception:
        return None
    content = (model.get("model") or {}).get("border") or (model.get("model") or {}).get("content") or []
    if len(content) < 8:
        return None
    xs, ys = content[0::2], content[1::2]
    x, y = min(xs), min(ys)
    w, h = max(xs) - x, max(ys) - y
    if w < 8 or h < 8:
        return None
    return {"x": x, "y": y, "w": w, "h": h, "a": w * h, "bid": backend_node_id}


def _cdp_boxes_from_tree(node: dict, client: Any, stack: list[dict], acc: list[dict]) -> None:
    chain = stack + [node]
    if node.get("nodeType") == 1:
        compact = re.sub(r"\s+", " ", _flatten_cdp_text(node, 0)).strip()
        if _LAST30.search(compact) and len(compact) < 400:
            best = None
            for cand in chain:
                bid = cand.get("backendNodeId")
                if not bid:
                    continue
                box = _quad_box(client, bid)
                if not box:
                    continue
                if 70 <= box["h"] <= 220 and 240 <= box["w"] <= 680:
                    if best is None or box["a"] > best["a"]:
                        best = box
            if best is None:
                bid = node.get("backendNodeId")
                raw_box = _quad_box(client, bid) if bid else None
                if raw_box and raw_box["h"] < 70 and 180 <= raw_box["w"] <= 720:
                    pad = 96
                    raw_box["x"] -= pad
                    raw_box["w"] += pad
                    raw_box["h"] = max(raw_box["h"], 72)
                    raw_box["a"] = raw_box["w"] * raw_box["h"]
                    best = raw_box
            if best and 180 <= best["w"] <= 720 and 70 <= best["h"] <= 220:
                best["tlen"] = len(compact)
                acc.append(best)
    for child in node.get("children") or []:
        _cdp_boxes_from_tree(child, client, chain, acc)
    for sr in node.get("shadowRoots") or []:
        _cdp_boxes_from_tree(sr, client, chain, acc)


def _overlap_frac(a: dict, b: dict) -> float:
    x0 = max(a["x"], b["x"])
    y0 = max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / max(1.0, min(a["w"] * a["h"], b["w"] * b["h"]))


def _dedupe_boxes(cand: list[dict]) -> list[dict]:
    cand = sorted(cand, key=lambda c: -c.get("a", c["w"] * c["h"]))
    kept: list[dict] = []
    for c in cand:
        if any(_overlap_frac(c, k) > 0.45 for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda b: (round(b["y"]), round(b["x"])))
    return kept


def _cdp_session(page: Any) -> Any:
    sess = getattr(page, "_repeek_cdp", None)
    if sess is None:
        sess = page.context.new_cdp_session(page)
        sess.send("DOM.enable")
        page._repeek_cdp = sess
    return sess


def _widget_ready(text: str) -> bool:
    """True when Last-N stats have numbers, not skeleton bars."""
    return bool(
        _LAST30.search(text)
        and re.search(r"\d+/\d+/\d+", text)
        and re.search(r"\d+%", text)
        and re.search(r"\d+\.\d+", text)
    )


def _collect_widgets(node: dict, acc: list[dict]) -> None:
    if node.get("nodeType") == 1:
        compact = re.sub(r"\s+", " ", _flatten_cdp_text(node, 0)).strip()
        last_n = len(_LAST30.findall(compact))
        if last_n == 1 and re.match(r"Last\s+\d+\s+matches", compact) and 24 < len(compact) < 280:
            acc.append({
                "ready": _widget_ready(compact),
                "n": len(compact),
                "bid": node.get("backendNodeId"),
            })
    for child in node.get("children") or []:
        _collect_widgets(child, acc)
    for sr in node.get("shadowRoots") or []:
        _collect_widgets(sr, acc)


def repeek_widget_status(page: Any) -> dict:
    """Count Last-30 widgets and how many have finished stats."""
    try:
        client = _cdp_session(page)
        doc = client.send("DOM.getDocument", {"depth": -1, "pierce": True})
        raw: list[dict] = []
        _collect_widgets(doc.get("root") or {}, raw)
    except Exception as e:
        raise RepeekCaptureError("REPEEK_STATUS", f"CDP widget count failed: {e}") from e
    raw.sort(key=lambda w: w["n"])
    kept: list[dict] = []
    seen: set[int] = set()
    for w in raw:
        bid = w.get("bid")
        if bid in seen:
            continue
        if bid is not None:
            seen.add(bid)
        kept.append(w)
    return {"widgets": len(kept), "ready": sum(1 for w in kept if w["ready"])}


def find_card_boxes(page: Any) -> list[dict]:
    """Locate player plates via CDP pierce. Empty/fallback is a hard fail."""
    try:
        client = _cdp_session(page)
        doc = client.send("DOM.getDocument", {"depth": -1, "pierce": True})
        acc: list[dict] = []
        _cdp_boxes_from_tree(doc.get("root") or {}, client, [], acc)
        boxes = _dedupe_boxes(acc)
    except RepeekCaptureError:
        raise
    except Exception as e:
        raise RepeekCaptureError("REPEEK_LOCATE", f"CDP locate failed: {e}") from e
    if not boxes:
        raise RepeekCaptureError("REPEEK_NO_CARDS", "CDP found no Last-30 player plates")
    return boxes


def _scroll_roster(page: Any) -> None:
    """Nudge the matchroom so below-fold Repeek widgets mount and fetch."""
    try:
        page.mouse.move(400, 700)
        for _ in range(8):
            page.mouse.wheel(0, 240)
            page.wait_for_timeout(250)
        page.mouse.wheel(0, -2400)
        page.wait_for_timeout(400)
    except Exception:
        pass


def wait_repeek_ready(page: Any, *, need: int = 10, timeout_ms: int = 90_000) -> dict:
    """Block until Last-30 cards exist and stats are filled in."""
    import time
    t0 = time.monotonic()
    last: dict | None = None
    stable = 0
    _scroll_roster(page)
    while (time.monotonic() - t0) * 1000 < timeout_ms:
        st = repeek_widget_status(page)
        print(f"[repeek] wait widgets={st['widgets']} ready={st['ready']}", flush=True)
        if st["ready"] >= need:
            if last == st:
                stable += 1
                if stable >= 2:
                    return st
            else:
                stable = 1
            last = st
        else:
            stable = 0
            last = st
            _scroll_roster(page)
        page.wait_for_timeout(1500)
    raise RepeekCaptureError(
        "REPEEK_NOT_READY",
        f"timed out after {timeout_ms}ms (last={last})",
    )


AVATAR_PAD_L = 12
AVATAR_PAD_T = 40
CARD_PAD_R = 6
CARD_PAD_B = 10


def _clip_for(box: dict, vw: dict, *, pad_l: float = 2, pad_t: float = 2,
              pad_r: float = 4, pad_b: float = 4) -> dict:
    x = float(box["x"]) - pad_l
    y = float(box["y"]) - pad_t
    w = float(box["w"]) + pad_l + pad_r
    h = float(box["h"]) + pad_t + pad_b
    vw_w, vw_h = float(vw["width"]), float(vw["height"])
    if x < -2 or y < -2 or x + w > vw_w + 2 or y + h > vw_h + 2:
        raise RepeekCaptureError(
            "REPEEK_CLIP_CLIPPED",
            f"clip {x:.0f},{y:.0f} {w:.0f}x{h:.0f} does not fit viewport "
            f"{vw_w:.0f}x{vw_h:.0f} — layout or viewport changed",
        )
    return {
        "x": max(0.0, x),
        "y": max(0.0, y),
        "width": w,
        "height": h,
    }


def _column_boxes(player_boxes: list[dict]) -> list[dict]:
    """Union each side's player plates, padded for hanging avatars."""
    if not player_boxes:
        return []
    xs = [float(b["x"]) for b in player_boxes]
    split = (min(xs) + max(xs)) / 2
    out: list[dict] = []
    for side, pred in (("left", lambda b: float(b["x"]) < split),
                       ("right", lambda b: float(b["x"]) >= split)):
        group = [b for b in player_boxes if pred(b)]
        if not group:
            continue
        x0 = min(float(b["x"]) for b in group) - AVATAR_PAD_L
        y0 = min(float(b["y"]) for b in group) - AVATAR_PAD_T
        x1 = max(float(b["x"]) + float(b["w"]) for b in group) + CARD_PAD_R
        y1 = max(float(b["y"]) + float(b["h"]) for b in group) + CARD_PAD_B
        out.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "side": side})
    return out


def assert_roster_layout(player_boxes: list[dict]) -> list[dict]:
    """Require a 5v5 FACEIT roster. Any drift raises ``RepeekCaptureError``."""
    n = len(player_boxes)
    if n != NEED_PLAYERS:
        raise RepeekCaptureError(
            "REPEEK_CARD_COUNT",
            f"expected {NEED_PLAYERS} player plates, got {n}",
        )
    for i, b in enumerate(player_boxes):
        w, h = float(b["w"]), float(b["h"])
        if not (CARD_W_RANGE[0] <= w <= CARD_W_RANGE[1]
                and CARD_H_RANGE[0] <= h <= CARD_H_RANGE[1]):
            raise RepeekCaptureError(
                "REPEEK_CARD_SIZE",
                f"card {i} {w:.0f}x{h:.0f} outside "
                f"w{CARD_W_RANGE}/h{CARD_H_RANGE}",
            )
    xs = [float(b["x"]) for b in player_boxes]
    split = (min(xs) + max(xs)) / 2
    n_left = sum(1 for b in player_boxes if float(b["x"]) < split)
    n_right = n - n_left
    if n_left != NEED_PER_SIDE or n_right != NEED_PER_SIDE:
        raise RepeekCaptureError(
            "REPEEK_SIDE_COUNT",
            f"expected {NEED_PER_SIDE}v{NEED_PER_SIDE}, got {n_left}v{n_right}",
        )
    rows: list[list[dict]] = []
    for b in sorted(player_boxes, key=lambda c: float(c["y"])):
        if rows and abs(float(b["y"]) - float(rows[-1][0]["y"])) <= ROW_Y_TOL:
            rows[-1].append(b)
        else:
            rows.append([b])
    if len(rows) != NEED_PER_SIDE or any(len(r) != 2 for r in rows):
        raise RepeekCaptureError(
            "REPEEK_ROW_ALIGN",
            f"expected {NEED_PER_SIDE} rows of 2, got {[len(r) for r in rows]}",
        )
    cols = _column_boxes(player_boxes)
    if len(cols) != 2 or [c["side"] for c in cols] != ["left", "right"]:
        raise RepeekCaptureError(
            "REPEEK_COLUMNS",
            f"expected left+right columns, got {cols!r}",
        )
    left, right = cols
    for col in cols:
        w, h = float(col["w"]), float(col["h"])
        if not (COL_W_RANGE[0] <= w <= COL_W_RANGE[1]
                and COL_H_RANGE[0] <= h <= COL_H_RANGE[1]):
            raise RepeekCaptureError(
                "REPEEK_COLUMN_SIZE",
                f"{col['side']} {w:.0f}x{h:.0f} outside "
                f"w{COL_W_RANGE}/h{COL_H_RANGE}",
            )
    gap = float(right["x"]) - (float(left["x"]) + float(left["w"]))
    if gap < MIN_COLUMN_GAP:
        raise RepeekCaptureError(
            "REPEEK_COLUMN_GAP",
            f"left/right gap {gap:.0f}px < {MIN_COLUMN_GAP:.0f}px "
            "(rosters overlapping or not two columns)",
        )
    return cols


def assert_column_pngs(left: Path, right: Path) -> None:
    from PIL import Image
    for side, path in (("left", left), ("right", right)):
        if not path.is_file():
            raise RepeekCaptureError(
                "REPEEK_PNG_MISSING",
                f"{side} column png missing: {path}",
            )
        im = Image.open(path).convert("RGB")
        if im.width < PNG_MIN_W or im.height < PNG_MIN_H:
            raise RepeekCaptureError(
                "REPEEK_PNG_SIZE",
                f"{side} {im.width}x{im.height} "
                f"min {PNG_MIN_W}x{PNG_MIN_H}",
            )
        gray = list(im.convert("L").getdata())
        mean = sum(gray) / len(gray)
        var = sum((p - mean) ** 2 for p in gray) / len(gray)
        if var < PNG_MIN_VAR:
            raise RepeekCaptureError(
                "REPEEK_PNG_BLANK",
                f"{side} looks empty (luma var {var:.1f} < {PNG_MIN_VAR})",
            )
    li, ri = Image.open(left), Image.open(right)
    if abs(li.height - ri.height) > 40 or abs(li.width - ri.width) > 40:
        raise RepeekCaptureError(
            "REPEEK_PNG_MISMATCH",
            f"left {li.width}x{li.height} vs right {ri.width}x{ri.height}",
        )


def _screenshot_clip(page: Any, box: dict, dest: Path, vw: dict, **pad: float) -> None:
    clip = _clip_for(box, vw, **pad)
    try:
        page.screenshot(path=str(dest), clip=clip)
    except Exception as e:
        raise RepeekCaptureError(
            "REPEEK_SCREENSHOT",
            f"screenshot {dest.name} failed: {e}",
        ) from e
    if not dest.exists() or dest.stat().st_size < 8_000:
        raise RepeekCaptureError(
            "REPEEK_SCREENSHOT",
            f"{dest.name} missing or tiny after screenshot",
        )


def strips_dir(match_id: str) -> Path:
    return STRIPS_ROOT / match_id


def repeek_installed(profile: Path) -> bool:
    ext = profile / "Default" / "Extensions" / REPEEK_ID
    return ext.is_dir() and any(ext.iterdir())


def _merge_repeek_prefs(src_prefs: Path, dst_prefs: Path) -> bool:
    """Copy the Repeek extensions.settings entry into dst Preferences."""
    try:
        src = json.loads(src_prefs.read_text(encoding="utf-8"))
        dst = json.loads(dst_prefs.read_text(encoding="utf-8")) if dst_prefs.exists() else {}
    except (OSError, ValueError):
        return False
    entry = ((src.get("extensions") or {}).get("settings") or {}).get(REPEEK_ID)
    if not isinstance(entry, dict):
        return False
    ext = dst.setdefault("extensions", {})
    settings = ext.setdefault("settings", {})
    settings[REPEEK_ID] = entry
    dst_prefs.parent.mkdir(parents=True, exist_ok=True)
    dst_prefs.write_text(json.dumps(dst, ensure_ascii=False), encoding="utf-8")
    return True


def seed_repeek_into_debug_profile(
    src_profile: Path = FACEIT_SESSION,
    dst_profile: Path = DEBUG_PROFILE,
) -> bool:
    """Copy the installed Repeek extension into the debug Chrome profile.

    Must run while Chrome is not using ``dst_profile``. Returns True if the
    extension files landed.
    """
    src_ext = src_profile / "Default" / "Extensions" / REPEEK_ID
    if not src_ext.is_dir():
        return False
    dst_ext = dst_profile / "Default" / "Extensions" / REPEEK_ID
    dst_ext.parent.mkdir(parents=True, exist_ok=True)
    if dst_ext.exists():
        shutil.rmtree(dst_ext, ignore_errors=True)
    shutil.copytree(src_ext, dst_ext)
    src_les = src_profile / "Default" / "Local Extension Settings" / REPEEK_ID
    if src_les.exists():
        dst_les = dst_profile / "Default" / "Local Extension Settings" / REPEEK_ID
        dst_les.parent.mkdir(parents=True, exist_ok=True)
        if dst_les.exists():
            shutil.rmtree(dst_les, ignore_errors=True)
        shutil.copytree(src_les, dst_les)
    _merge_repeek_prefs(
        src_profile / "Default" / "Preferences",
        dst_profile / "Default" / "Preferences",
    )
    return repeek_installed(dst_profile)


SETTLE_TIMEOUT_MS = 15_000
LATE_FETCH_WAIT_MS = 6_000


def _settled_boxes(page: Any, timeout_ms: int = SETTLE_TIMEOUT_MS) -> tuple:
    """Poll card locate + layout assert until the grid settles.

    A single measurement right after scroll-to-top can catch the page
    mid-settle (rows split apart) and fail ``REPEEK_ROW_ALIGN`` on a room
    whose steady-state grid is a perfect 5x2. Keep polling; still raises
    the last error if the layout never settles (fail loud).
    """
    import time
    t0 = time.monotonic()
    last_err: RepeekCaptureError | None = None
    while (time.monotonic() - t0) * 1000 < timeout_ms:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
        try:
            boxes = find_card_boxes(page)
            cols = assert_roster_layout(boxes)
            return boxes, cols
        except RepeekCaptureError as e:
            last_err = e
    assert last_err is not None
    raise last_err


def capture_from_page(page: Any, match_id: str, out_dir: Path | None = None) -> Path:
    """Screenshot left/right roster columns. Raises on any layout/PNG mismatch."""
    out_dir = out_dir or strips_dir(match_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    wait_repeek_ready(page)
    boxes, cols = _settled_boxes(page)
    # Below-fold cards can still show skeleton stat bars when the grid first
    # settles (the wait probe double-counts nested hosts). Give late Last-30
    # fetches a grace window, then re-locate + re-assert before shooting.
    page.wait_for_timeout(LATE_FETCH_WAIT_MS)
    boxes = find_card_boxes(page)
    cols = assert_roster_layout(boxes)
    vw = page.viewport_size or {"width": 1920, "height": 1440}
    for col in cols:
        dest = out_dir / f"repeek_{col['side']}.png"
        _screenshot_clip(page, col, dest, vw, pad_l=0, pad_t=0, pad_r=0, pad_b=0)
        print(f"[repeek] {col['side']} {col['w']:.0f}x{col['h']:.0f}", flush=True)
    left_png = out_dir / "repeek_left.png"
    right_png = out_dir / "repeek_right.png"
    assert_column_pngs(left_png, right_png)
    (out_dir / "repeek_meta.json").write_text(json.dumps({
        "match_id": match_id,
        "cards": len(boxes),
        "columns": [{k: v for k, v in c.items() if k in ("x", "y", "w", "h", "side")}
                    for c in cols],
        "boxes": [{k: v for k, v in b.items() if k in ("x", "y", "w", "h", "bid")}
                  for b in boxes],
    }, indent=2), encoding="utf-8")
    return out_dir


def run_standalone(match_id: str, out_dir: Path | None = None) -> Path:
    """Headed Chrome on ``.sessions/faceit`` (Repeek is installed there)."""
    from playwright.sync_api import sync_playwright

    if not repeek_installed(FACEIT_SESSION):
        raise RepeekCaptureError(
            "REPEEK_NOT_INSTALLED",
            f"Repeek missing in {FACEIT_SESSION}",
        )
    room = f"https://www.faceit.com/en/cs2/room/{match_id}"
    out_dir = out_dir or strips_dir(match_id)
    print(f"[repeek] profile={FACEIT_SESSION}", flush=True)
    print(f"[repeek] room={room}", flush=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(FACEIT_SESSION),
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation", "--disable-extensions"],
            viewport={"width": 1920, "height": 1440},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(room, wait_until="load", timeout=60_000)
            n_sw = len(ctx.service_workers)
            if n_sw < 1:
                raise RepeekCaptureError(
                    "REPEEK_NO_EXTENSION",
                    "no service workers — Repeek did not load",
                )
            print(f"[repeek] service_workers={n_sw}", flush=True)
            captured = capture_from_page(page, match_id, out_dir)
            print(f"[repeek] out={captured}", flush=True)
            return captured
        finally:
            ctx.close()


def main() -> None:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Standalone Repeek room capture")
    ap.add_argument("--match-id", default="1-75510475-266d-4a7a-a384-72ed968f005d")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = Path(args.out) if args.out else None
    try:
        run_standalone(args.match_id, out)
    except RepeekCaptureError as e:
        print(json.dumps(e.payload), flush=True)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
