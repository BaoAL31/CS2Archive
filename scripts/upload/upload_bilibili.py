"""
Upload a finished POV video to bilibili.tv (studio.bilibili.tv).

Uses Playwright + Chrome with `.bilibili_storage.json` (login once via
`scripts/upload/bilibili_login.py`). Reads the same `upload_meta.json` as YouTube.

Studio hard-caps tags at 10 chips — remaining tags are appended to the
description. Videos over ~3.8 GB are re-encoded to `video_bili.mp4`
(1080p / ~15 Mbps NVENC) before upload.

Usage:
    python scripts/upload/upload_bilibili.py --meta youtube/<run>/upload_meta.json
    python scripts/upload/upload_bilibili.py <video.mp4> --title "..." --thumbnail thumb.jpg

Called from upload_youtube.py / upload_pending.py with --also-bilibili.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

STORAGE = PROJECT_ROOT / ".bilibili_storage.json"
SHOTS = PROJECT_ROOT / "tmp" / "bili_shots"
FFMPEG = Path(r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe")
MAX_BILI_BYTES = int(3.8 * 1024**3)  # studio rejects ~4 GB+
BILI_TAG_LIMIT = 10


def log(msg: str) -> None:
    print(f"  [bili] {msg}", flush=True)


def shot(page, tag: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{int(time.time())}_{tag}.png"
    try:
        page.screenshot(path=str(path))
    except Exception as e:
        log(f"shot warn: {e}")


def pick_bili_tags(tags: list[str], variant: str | None = None) -> list[str]:
    """Pick ≤10 chips. Overlay variants prefer input/utility tags."""
    preferred: list[str] = []
    if variant == "overlay":
        for t in ("input overlay", "utility cam", "CS2 POV", "keyboard overlay"):
            if t in tags:
                preferred.append(t)
    out: list[str] = []
    for t in preferred + list(tags):
        t = (t or "").strip()
        if not t or t in out:
            continue
        out.append(t)
        if len(out) >= BILI_TAG_LIMIT:
            break
    return out


def build_description(description: str, all_tags: list[str]) -> str:
    base = (description or "").rstrip()
    if all_tags and "Tags:" not in base:
        base = f"{base}\n\nTags: {', '.join(all_tags)}"
    return base[:2000]


def ensure_bili_video(src: Path) -> Path:
    """Return a path under the ~4 GB studio limit, re-encoding if needed."""
    if not src.exists():
        raise FileNotFoundError(src)
    if src.stat().st_size <= MAX_BILI_BYTES:
        return src

    out = src.parent / "video_bili.mp4"
    if (
        out.exists()
        and 1_000_000 < out.stat().st_size <= MAX_BILI_BYTES
        and out.stat().st_mtime >= src.stat().st_mtime
    ):
        log(f"reusing {out.name} ({out.stat().st_size / 1e9:.2f} GB)")
        return out

    ff = str(FFMPEG) if FFMPEG.exists() else "ffmpeg"
    log(
        f"re-encoding {src.name} ({src.stat().st_size / 1e9:.2f} GB) → "
        f"{out.name} (1080p ~15 Mbps) for bilibili size limit"
    )
    cmd = [
        ff, "-y", "-hwaccel", "cuda", "-i", str(src),
        "-vf", "scale=1920:1080:flags=lanczos",
        "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
        "-b:v", "15M", "-maxrate", "18M", "-bufsize", "30M",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"bilibili re-encode failed: {(r.stderr or '')[-400:]}")
    if out.stat().st_size > MAX_BILI_BYTES:
        raise RuntimeError(
            f"re-encoded file still too large: {out.stat().st_size / 1e9:.2f} GB"
        )
    log(f"re-encode done ({out.stat().st_size / 1e9:.2f} GB)")
    return out


def resolve_publish(meta: dict) -> datetime | None:
    """Parse publish_at + timezone from upload_meta, if present."""
    raw = meta.get("publish_at")
    if not raw or raw == "auto":
        return None
    tz_name = meta.get("publish_timezone") or "Australia/Sydney"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Australia/Sydney")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def dismiss_cover_editor(page, close_selector: bool = False) -> None:
    """Confirm the cover editor (Crop then Confirm). Never click Cancel (resets form)."""
    t0 = time.time()
    while time.time() - t0 < 15:
        if page.get_by_text("Cover editor", exact=False).count():
            break
        page.wait_for_timeout(500)
    if not page.get_by_text("Cover editor", exact=False).count():
        log("cover editor did not open")
        return
    shot(page, "cover_before")

    # Cover uploads to bilibili async — wait until the image renders + Confirm enables
    t0 = time.time()
    while time.time() - t0 < 30:
        st = page.evaluate(
            """() => {
          const dlg = [...document.querySelectorAll('.el-dialog')]
            .find(d => /Cover editor/i.test(d.innerText || ''));
          if (!dlg) return {editor: false};
          const imgs = [...dlg.querySelectorAll('img')].filter(i => i.naturalWidth >= 500);
          const btn = [...dlg.querySelectorAll('button')]
            .find(b => /^\\s*Confirm\\s*$/i.test((b.innerText || '').trim()));
          return {
            editor: true,
            imgLoaded: imgs.length > 0,
            confirmDisabled: btn ? !!(btn.disabled || btn.classList.contains('is-disabled')) : null,
          };
        }"""
        )
        if st.get("editor") and st.get("imgLoaded") and st.get("confirmDisabled") is False:
            break
        page.wait_for_timeout(1000)

    page.evaluate(
        """() => {
      const nodes = [...document.querySelectorAll('div,span,button,a,p')];
      const el = nodes.find(n => {
        const t = (n.innerText || '').replace(/\\s+/g,' ').trim();
        return t === 'Crop' || t === '✓ Crop' || /^✓\\s*Crop$/.test(t);
      });
      if (el) el.click();
    }"""
    )
    page.wait_for_timeout(2500)

    page.evaluate(
        """() => {
      const btn = [...document.querySelectorAll('.el-dialog button')]
        .find(b => /^\\s*Confirm\\s*$/i.test((b.innerText || '').trim()));
      if (btn && !btn.disabled) btn.click();
    }"""
    )
    page.wait_for_timeout(3000)

    if close_selector:
        page.evaluate(
            """() => {
              const dlg = [...document.querySelectorAll('.el-dialog')]
                .find(d => /Cover editor|From local/i.test(d.innerText || ''));
              const x = dlg && dlg.querySelector('.el-dialog__headerbtn, .el-dialog__close');
              if (x) x.click();
            }"""
        )
        page.wait_for_timeout(1000)
    shot(page, "cover_done")


def set_cover(page, thumb: Path | None) -> None:
    if not thumb or not thumb.exists():
        log("no thumbnail — skipping cover set")
        return
    loc = page.get_by_text("Upload a cover", exact=False)
    if not loc.count():
        log("WARN: no 'Upload a cover' button found")
        return
    try:
        loc.first.click()
    except Exception as e:
        log(f"cover open miss: {type(e).__name__}")
    page.wait_for_timeout(2000)

    cover_input = page.locator("#cover-upload-btn")
    if not cover_input.count():
        inputs = page.locator('input[type="file"]')
        for i in range(inputs.count()):
            acc = (inputs.nth(i).get_attribute("accept") or "").lower()
            if any(x in acc for x in ("jpg", "png", "jpeg", "image")):
                cover_input = inputs.nth(i)
                break
    if not cover_input.count():
        log("WARN: no cover file input found")
        return
    try:
        cover_input.first.set_input_files(str(thumb))
        log(f"cover attached: {thumb.name}")
    except Exception as e:
        log(f"cover set miss: {type(e).__name__}")
        return
    page.wait_for_timeout(2000)
    dismiss_cover_editor(page, close_selector=True)


def fill_tags(page, tags: list[str]) -> None:
    page.evaluate(
        """() => {
      for (let i = 0; i < 20; i++) {
        const c = document.querySelector(
          '.el-tag__close, .tag-selctor__tag .el-icon-close, [class*=tag] .el-tag__close'
        );
        if (!c) break;
        c.click();
      }
    }"""
    )
    page.wait_for_timeout(300)

    # Scroll Tags into view — input is hidden when already at 10/10
    page.evaluate(
        """() => {
      const el = [...document.querySelectorAll('*')]
        .find(e => (e.innerText||'').trim() === 'Tags');
      if (el) el.scrollIntoView({block:'center'});
    }"""
    )
    page.wait_for_timeout(400)

    tag_in = page.locator(
        'input[placeholder*="Press Enter" i], input[placeholder*="tag" i], '
        'input[placeholder*="Tag"]'
    )
    for tag in tags:
        try:
            if not tag_in.count():
                # At 10/10 the input disappears — clear one chip and continue
                page.evaluate(
                    """() => {
                      const c = document.querySelector(
                        '.tag-selctor__tag.closable, .el-tag__close'
                      );
                      if (c) c.click();
                    }"""
                )
                page.wait_for_timeout(200)
            if tag_in.count():
                tag_in.first.click()
                tag_in.first.fill(tag)
                page.keyboard.press("Enter")
                page.wait_for_timeout(200)
        except Exception as e:
            log(f"tag fail {tag!r}: {e}")

    body = page.inner_text("body")
    m = re.search(r"(\d{1,2})/10", body)
    log(f"tags ui={m.group(0) if m else '?'} chips={tags}")


def set_schedule(page, when: datetime | None) -> None:
    if when is None:
        return
    date_s = when.strftime("%Y-%m-%d")
    time_s = when.strftime("%H:%M")
    if page.get_by_text("Scheduled Release", exact=True).count():
        page.get_by_text("Scheduled Release", exact=True).first.click()
        page.wait_for_timeout(600)
    full_s = f"{date_s} {time_s}"
    page.evaluate(
        """({date_s, time_s, full_s}) => {
      const setNative = (el, val) => {
        const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, val); else el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
      };
      for (const el of document.querySelectorAll('input')) {
        const v = el.value || '', t = el.type || '', ph = (el.placeholder || '').toLowerCase();
        if (t === 'datetime-local' || ph.includes('date and time')) { setNative(el, full_s); continue; }
        if (t === 'date' || /^\\d{4}-\\d{2}-\\d{2}$/.test(v) || ph.includes('date')) { setNative(el, date_s); continue; }
        if (t === 'time' || /^\\d{1,2}:\\d{2}$/.test(v) || ph.includes('time')) setNative(el, time_s);
      }
    }""",
        {"date_s": date_s, "time_s": time_s, "full_s": full_s},
    )
    for sel, val in (('input[type="date"]', date_s), ('input[type="time"]', time_s)):
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.fill(val)
            except Exception:
                pass
    # Composite el-date-picker (single text input) only parses real keystrokes —
    # force-commit the full datetime so the bound model isn't left with just time.
    try:
        picker = page.locator('input[placeholder="Please select date and time"]')
        if picker.count():
            picker.first.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(full_s)
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
    except Exception:
        pass
    log(f"schedule {date_s} {time_s}")


def wait_upload_complete(page, timeout_s: int = 45 * 60) -> None:
    deadline = time.time() + timeout_s
    i = 0
    while time.time() < deadline:
        if page.is_closed():
            raise RuntimeError("page closed during upload")
        try:
            status = page.evaluate(
                """() => {
                  const t = (document.body && document.body.innerText || '').slice(0, 5000);
                  const m = t.match(/(\\d{1,3})\\s*%/);
                  return {
                    pct: m ? parseInt(m[1], 10) : null,
                    completed: /upload completed/i.test(t),
                  };
                }"""
            )
        except Exception as e:
            if page.is_closed():
                raise RuntimeError("page closed during upload") from e
            log(f"pct poll warn: {e}")
            page.wait_for_timeout(5000)
            i += 1
            continue
        pct = status.get("pct")
        if i % 4 == 0:
            log(f"upload pct={pct} completed={status.get('completed')}")
        if status.get("completed") or (pct is not None and pct >= 100):
            log("file upload finished")
            return
        try:
            page.wait_for_timeout(15000)
        except Exception as e:
            raise RuntimeError("page closed during upload") from e
        i += 1
    raise TimeoutError("bilibili file upload did not reach 100%")


def extract_aid(url: str, body: str) -> str | None:
    m = re.search(r"/archive/edit/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/archive/edit/(\d+)", body)
    return m.group(1) if m else None


def lookup_aid_by_title(page, title: str) -> str | None:
    """Resolve aid from studio archives API when submit doesn't navigate to edit URL."""
    needle = (title or "")[:40]
    try:
        return page.evaluate(
            """async (needle) => {
              const r = await fetch(
                'https://api.bilibili.tv/intl/videoup/web2/archives?state=&pn=1&ps=20&lang_id=3&platform=web&lang=en_US&s_locale=en_US',
                {credentials:'include'}
              );
              const j = await r.json();
              const archives = j?.data?.archives || [];
              const hit = archives.find(a => (a.title || '').includes(needle));
              return hit ? String(hit.aid) : null;
            }""",
            needle,
        )
    except Exception as e:
        log(f"aid lookup failed: {e}")
        return None


def poll_aid_after_submit(page, title: str, tries: int = 6, wait_s: int = 10) -> str | None:
    """Poll archives API for the just-submitted video (processing can lag)."""
    needle = (title or "")[:40]
    for i in range(tries):
        try:
            aid = page.evaluate(
                """async (needle) => {
                  const r = await fetch(
                    'https://api.bilibili.tv/intl/videoup/web2/archives?state=&pn=1&ps=20&lang_id=3&platform=web&lang=en_US&s_locale=en_US',
                    {credentials:'include'}
                  );
                  const j = await r.json();
                  const archives = j?.data?.archives || [];
                  const hit = archives.find(a => (a.title || '').includes(needle));
                  return hit ? String(hit.aid) : null;
                }""",
                needle,
            )
            if aid:
                log(f"resolved aid via archives API (poll {i + 1}): {aid}")
                return aid
        except Exception as e:
            log(f"aid poll warn ({i + 1}): {e}")
        page.wait_for_timeout(wait_s * 1000)
    return None


def _write_meta(meta_path: Path | None, **fields) -> None:
    if not meta_path or not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(fields)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"meta write warn: {e}")


def upload_to_bilibili(
    video_path: Path,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    thumbnail_path: Path | None = None,
    meta_path: Path | None = None,
    publish_at: datetime | None = None,
    variant: str | None = None,
    headless: bool = False,
) -> str:
    """Upload to bilibili.tv. Returns aid string. Updates meta_path when given."""
    if not STORAGE.exists():
        raise FileNotFoundError(
            f"missing {STORAGE} — run: python scripts/upload/bilibili_login.py"
        )

    tags = list(tags or [])
    chips = pick_bili_tags(tags, variant)
    desc = build_description(description, tags)
    video = ensure_bili_video(video_path)
    log(f"video={video} ({video.stat().st_size / 1e9:.2f} GB)")
    log(f"title={title[:80]!r}")
    log(f"chips({len(chips)})={chips}")

    _write_meta(meta_path, bilibili_upload_status="uploading")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        ctx = browser.new_context(
            storage_state=str(STORAGE),
            viewport={"width": 1440, "height": 960},
        )
        page = ctx.new_page()
        page.set_default_timeout(60000)

        page.goto("https://studio.bilibili.tv/archive/new", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if "SESSDATA" not in {c["name"] for c in ctx.cookies()}:
            raise RuntimeError("bilibili session expired — re-run scripts/upload/bilibili_login.py")
        log("logged in")

        with page.expect_file_chooser(timeout=20000) as fc:
            page.locator("#step-one__upload-btn").click()
        fc.value.set_files(str(video))
        log("video attached")
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        if "exceeds the limit" in body.lower():
            raise RuntimeError("size exceeds bilibili limit after re-encode")

        page.locator('input[maxlength="100"]').first.wait_for(timeout=60000)
        page.locator('input[maxlength="100"]').first.fill(title[:100])

        # Vue-friendly description set
        page.evaluate(
            """(text) => {
              const ta = document.querySelector('textarea');
              if (!ta) return;
              const d = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
              if (d && d.set) d.set.call(ta, text); else ta.value = text;
              ta.dispatchEvent(new Event('input', {bubbles:true}));
              ta.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            desc,
        )
        page.locator("textarea").first.click()
        page.keyboard.press("End")
        page.keyboard.type(" ")
        page.keyboard.press("Backspace")

        fill_tags(page, chips)
        set_cover(page, thumbnail_path)
        if page.get_by_text("Cover editor", exact=False).count():
            page.evaluate(
                """() => {
                  const dlg = [...document.querySelectorAll('.el-dialog')]
                    .find(d => /Cover editor/i.test(d.innerText||''));
                  const x = dlg && dlg.querySelector('.el-dialog__headerbtn');
                  if (x) x.click();
                }"""
            )
            page.wait_for_timeout(800)

        set_schedule(page, publish_at)
        wait_upload_complete(page)

        # Final pass before submit
        set_schedule(page, publish_at)
        fill_tags(page, chips)
        shot(page, "before_submit")

        if page.get_by_role("button", name="Upload Now").count():
            page.get_by_role("button", name="Upload Now").first.click()
        else:
            page.locator("button:has-text('Upload Now')").first.click()
        log("clicked Upload Now")
        page.wait_for_timeout(8000)
        shot(page, "after_submit")

        body = page.inner_text("body")
        if "SUBMIT_ARCHIVE_ERROR" in body or page.evaluate(
            """() => /please|required|error/i.test(
              [...document.querySelectorAll('.el-form-item__error,.el-message')]
                .map(e => e.innerText).join(' ')
            )"""
        ):
            # soft check — studio may still succeed
            log("validation toast may be present — check screenshots")

        aid = extract_aid(page.url, body)
        if not aid:
            # Submit often returns to archive-list while Processing (no edit URL).
            page.goto("https://studio.bilibili.tv/archive-list", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            aid = poll_aid_after_submit(page, title)
            if aid:
                log(f"resolved aid via archives API: {aid}")
        ctx.storage_state(path=str(STORAGE))
        browser.close()

    if not aid:
        _write_meta(meta_path, bilibili_upload_status="submitted_no_aid")
        raise RuntimeError("upload submitted but could not parse bilibili aid from URL")

    _write_meta(
        meta_path,
        bilibili_upload_status="completed",
        bilibili_aid=aid,
        bilibili_url=f"https://studio.bilibili.tv/archive/edit/{aid}?from=video",
        bilibili_tags=chips,
    )
    log(f"uploaded aid={aid}")
    return aid


def is_bilibili_pending(meta: dict) -> bool:
    return not (
        meta.get("bilibili_upload_status") == "completed" and meta.get("bilibili_aid")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload video to bilibili.tv")
    parser.add_argument("video", nargs="?", help="Path to video file")
    parser.add_argument("--meta", help="Path to upload_meta.json")
    parser.add_argument("--title", help="Video title")
    parser.add_argument("--description", "-d", default="", help="Description")
    parser.add_argument("--tags", help="Comma-separated tags")
    parser.add_argument("--thumbnail", "-t", help="Cover image")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()

    meta: dict = {}
    meta_path: Path | None = None
    if args.meta:
        meta_path = Path(args.meta)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    elif args.video:
        candidate = Path(args.video).parent / "upload_meta.json"
        if candidate.exists():
            meta_path = candidate
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

    video = Path(args.video or meta.get("video_path", ""))
    if not video.exists():
        print(f"[ERROR] video not found: {video}", flush=True)
        return 1

    title = args.title or meta.get("title")
    if not title:
        print("[ERROR] title required (--title or upload_meta.json)", flush=True)
        return 1

    description = args.description or meta.get("description", "")
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    else:
        tags = list(meta.get("tags") or [])

    thumb = args.thumbnail or meta.get("thumbnail_path")
    thumb_path = Path(thumb) if thumb else None
    if thumb_path and not thumb_path.exists():
        # Prefer .jpg sibling of .png
        alt = thumb_path.with_suffix(".jpg")
        thumb_path = alt if alt.exists() else None

    publish = resolve_publish(meta)
    variant = meta.get("variant")

    try:
        aid = upload_to_bilibili(
            video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_path=thumb_path,
            meta_path=meta_path,
            publish_at=publish,
            variant=variant,
            headless=args.headless,
        )
    except Exception as e:
        print(f"[ERROR] bilibili upload failed: {e}", flush=True)
        _write_meta(meta_path, bilibili_upload_status="failed", bilibili_error=str(e)[:500])
        return 1

    print(f"  Bilibili: aid={aid}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
