"""Style-01 (performance proof) thumbnail — same layout as html_examples.html #proof.

Renders via Chromium so CSS (drop-shadow, contain, Montserrat) matches the study.
FACEIT pipeline step 6 calls this through ``scripts/faceit/faceit_thumbnail.py``.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"
WIDTH, HEIGHT = 1280, 720
BADGE_DEFAULT = "INPUTS + UTIL CAMS"

# Solo / 3-up (locked by tests/test_proof_01.py). Duo (one costar) matches
# html_examples.html #proof: POV far left, teammate at 360px — not under the K-D.
_MAIN_SOLO = "width: 480px; height: 640px; left: 130px; bottom: -22px;"
_MAIN_DUO = "width: 560px; height: 700px; left: -20px; bottom: -20px;"
_LEFT_3UP = "width: 300px; height: 400px; left: -20px; bottom: -14px;"
_RIGHT_3UP = "width: 300px; height: 400px; left: 520px; bottom: -14px;"
_RIGHT_DUO = "width: 360px; height: 450px; left: 360px; bottom: -16px;"

# CSS copied from exports/pov_market/thumbnail_study/html_examples.html #proof.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  @font-face {{
    font-family: Montserrat;
    src: url("{font}");
    font-weight: 800;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #17191d; font-family: Montserrat, Arial, sans-serif; }}
  .thumb {{
    position: relative;
    width: 1280px;
    height: 720px;
    overflow: hidden;
    color: white;
    background: url("{bg}") center / cover no-repeat;
  }}
  .thumb::before {{
    content: "";
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, .13);
  }}
  .product-badge {{
    position: absolute;
    z-index: 20;
    top: 18px;
    left: 18px;
    padding: 9px 15px 8px;
    border-radius: 8px;
    background: rgba(8, 10, 13, .90);
    border-left: 4px solid #e66342;
    font-size: 21px;
    line-height: 1;
    letter-spacing: .3px;
    {badge_display}
  }}
  .portrait {{
    position: absolute;
    z-index: 5;
    object-fit: contain;
    object-position: bottom;
    filter: drop-shadow(0 2px 0 #fff) drop-shadow(0 -2px 0 #fff)
            drop-shadow(2px 0 0 #fff) drop-shadow(-2px 0 0 #fff)
            drop-shadow(0 9px 16px rgba(0,0,0,.75));
  }}
  .portrait.main {{
    {main_box}
    z-index: 6;
  }}
  .portrait.left {{
    {left_box}
    z-index: 7;
    {left_display}
  }}
  .portrait.right {{
    {right_box}
    z-index: 7;
    {right_display}
  }}
  .headline {{
    position: absolute;
    z-index: 10;
    text-transform: none;
    text-shadow: 0 5px 0 #111, 0 8px 18px rgba(0,0,0,.8);
  }}
  .accent {{ color: #efc34f; }}
  .proof-copy {{
    right: 56px;
    top: 50%;
    bottom: auto;
    transform: translateY(-50%);
    width: 560px;
    text-align: center;
  }}
  .proof-copy .name {{ font-size: 84px; line-height: .9; }}
  .proof-copy .score {{ font-size: 142px; line-height: 1; letter-spacing: -7px; }}
  .proof-copy .sub {{ font-size: 30px; letter-spacing: 1px; {sub_display} }}
</style>
</head>
<body>
  <section class="thumb" id="proof">
    <div class="product-badge">{badge}</div>
    <img class="portrait left" src="{left}" alt="">
    <img class="portrait right" src="{right}" alt="">
    <img class="portrait main" src="{main}" alt="">
    <div class="headline proof-copy">
      <div class="name">{name}</div>
      <div class="score accent">{score}</div>
      <div class="sub">{sub}</div>
    </div>
  </section>
</body>
</html>
"""


def _uri(path: Path | None) -> str:
    if path is None:
        return ""
    return Path(path).resolve().as_uri()


def build_html(
    *,
    bg: Path,
    main_avatar: Path,
    name: str,
    score: str,
    sub: str = "",
    costar_avatar: Path | None = None,
    costar_left: Path | None = None,
    costar_right: Path | None = None,
    badge: str = BADGE_DEFAULT,
    font: Path | None = None,
) -> str:
    """HTML for style 01. Paths become file:// URIs for Chromium."""
    font = font or FONT_PATH
    right = costar_right or costar_avatar
    has_left = costar_left is not None and Path(costar_left).is_file()
    has_right = right is not None and Path(right).is_file()
    has_sub = bool(sub.strip())
    has_badge = bool(badge.strip())
    duo = has_right and not has_left
    return _TEMPLATE.format(
        font=_uri(font),
        bg=_uri(bg),
        main=_uri(main_avatar),
        left=_uri(costar_left) if has_left else "",
        right=_uri(right) if has_right else "",
        name=_esc(name),
        score=_esc(score),
        sub=_esc(sub),
        badge=_esc(badge),
        main_box=_MAIN_DUO if duo else _MAIN_SOLO,
        left_box=_LEFT_3UP,
        right_box=_RIGHT_DUO if duo else _RIGHT_3UP,
        left_display="display: none;" if not has_left else "",
        right_display="display: none;" if not has_right else "",
        sub_display="display: none;" if not has_sub else "",
        badge_display="display: none;" if not has_badge else "",
    )


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_png(html: str, dest: Path) -> Path:
    """Screenshot #proof at 1280x720 via Playwright Chromium."""
    from playwright.sync_api import sync_playwright

    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    html_path = dest.with_suffix(".proof01.html")
    html_path.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--allow-file-access-from-files"])
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.goto(html_path.as_uri(), wait_until="load")
            page.evaluate("() => document.fonts.ready")
            page.locator("#proof").screenshot(path=str(dest), type="png")
            browser.close()
    finally:
        html_path.unlink(missing_ok=True)
    return dest


def render(
    *,
    bg: Path,
    main_avatar: Path,
    name: str,
    score: str,
    dest: Path,
    sub: str = "",
    costar_avatar: Path | None = None,
    costar_left: Path | None = None,
    costar_right: Path | None = None,
    badge: str = BADGE_DEFAULT,
) -> Path:
    dest = Path(dest)
    html = build_html(
        bg=bg,
        main_avatar=main_avatar,
        name=name,
        score=score,
        sub=sub,
        costar_avatar=costar_avatar,
        costar_left=costar_left,
        costar_right=costar_right,
        badge=badge,
    )
    png_dest = dest if dest.suffix.lower() == ".png" else dest.with_suffix(".png")
    render_png(html, png_dest)
    if dest.suffix.lower() in {".jpg", ".jpeg"}:
        Image.open(png_dest).convert("RGB").save(dest, "JPEG", quality=95, subsampling=0)
        if png_dest != dest:
            png_dest.unlink(missing_ok=True)
        return dest
    return png_dest
