"""Silent fallbacks on the render path must be hard failures.

The theme (2026-11-07): every "just use a default and carry on" cost a full
render and was only caught by eye. dem0n plays 4:3 1280x960 Stretched;
prosettings had no list entry, so the resolver returned 1920x1080 16:9 Native and
the capture started at the wrong aspect. Same class, four sites:

  1. scrapers.prosettings.resolve_video_settings   -> unknown player
  2. cs2archive.pov.render_pov                     -> capture aspect unknown
  3. cs2archive.pov.render_pov                     -> viewmodel lookup failed
  4. cs2archive.overlay.overlay_pov                -> no tick/frame mapping

Benign, deliberate fallbacks elsewhere (avatar fetch, playlist naming,
optional niceties) are NOT covered here — this module pins the ones that change
the delivered pixels.
"""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# ── 1. prosettings ──────────────────────────────────────────────────────────

def test_unknown_player_raises_video_settings_unresolved():
    from scrapers import prosettings

    with patch.object(prosettings, "load_cache", return_value={}), \
         patch.object(prosettings, "scrape_and_cache", return_value={}), \
         patch.object(prosettings, "scrape_player_video_settings", return_value=None), \
         patch.object(prosettings, "scrape_player_viewmodel", return_value={}):
        with pytest.raises(prosettings.VideoSettingsUnresolved):
            prosettings.resolve_video_settings("nobody-xyz", fetch_viewmodel=False)


def test_direct_player_page_wins_over_the_list_cache():
    from scrapers import prosettings

    direct = {"resolution": "1280x960", "width": 1280, "height": 960,
              "aspect_ratio": "4:3", "scaling_mode": "Stretched",
              "source": "prosettings"}
    with patch.object(prosettings, "load_cache", return_value={}), \
         patch.object(prosettings, "scrape_and_cache", return_value={}), \
         patch.object(prosettings, "scrape_player_video_settings", return_value=dict(direct)), \
         patch.object(prosettings, "scrape_player_viewmodel", return_value={}):
        out = prosettings.resolve_video_settings("dem0n", fetch_viewmodel=False)
    assert out["aspect_ratio"] == "4:3"
    assert out["resolution"] == "1280x960"


# ── 2. overlay tick/frame mapping ───────────────────────────────────────────

def _overlay_module():
    import cs2archive.overlay.overlay_pov as op
    return op


def test_overlay_refuses_to_guess_tick_mapping_without_round_durations():
    """No per-round durations and no frame mapping => exit, not a linear guess."""
    op = _overlay_module()
    fn = getattr(op, "_map_video_frames_to_ticks", None)
    if fn is None:
        pytest.skip("mapping helper is inline; covered by the sys.exit source guard")
    with pytest.raises(SystemExit):
        fn([], {}, {}, {}, 0, 60, 100)


def test_overlay_source_has_no_linear_tick_fallback():
    """Guard the exact fallback that used to ship wrong key timings."""
    src = Path("cs2archive/overlay/overlay_pov.py").read_text(encoding="utf-8")
    assert "linear tick mapping from base_tick" not in src.lower()
    # the old guess survives only as the explanatory comment on the hard fail
    assert "tick = rs + int(offset_sec * TICKRATE)" not in src


# ── 3. render_pov capture aspect ────────────────────────────────────────────

def test_render_pov_source_requires_a_known_aspect():
    src = Path("cs2archive/pov/render_pov.py").read_text(encoding="utf-8")
    assert 'capture_size_for_player(_acct) or capture_size_for_aspect("")' not in src, \
        "the 16:9 default capture fallback must be gone"
    assert "RENDER_CAPTURE_ASPECT_UNKNOWN" in src
    assert "RENDER_VIDEO_SETTINGS_UNRESOLVED" in src


def test_pipeline_reports_video_settings_failure_as_an_error():
    src = Path("cs2archive/pov/pipeline.py").read_text(encoding="utf-8")
    assert "VIDEO_SETTINGS_UNRESOLVED" in src
    assert "[WARN] video settings lookup failed" not in src


# ── 4. create_backlog refuses to write a guessed card ───────────────────────

def test_create_backlog_aborts_when_video_settings_unresolved(monkeypatch, tmp_path):
    from cs2archive.pov import create_backlog as cb
    from scrapers.prosettings import VideoSettingsUnresolved

    monkeypatch.setattr(cb, "BACKLOG_DIR", tmp_path)
    monkeypatch.setattr(cb, "_ensure_player_account", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_ensure_video_settings", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_existing_avatar_path", lambda *a, **k: "x.png")
    called = {}

    def fake_video_fields(nick):
        called["nick"] = nick
        raise VideoSettingsUnresolved(nick)

    monkeypatch.setattr("scrapers.prosettings.backlog_video_fields", fake_video_fields)
    # create_backlog_entry makes the demo path relative to the repo root, so
    # the fake demo must live inside it.
    repo_demo = Path(cb.PROJECT_ROOT) / ".tmp" / "silent-fallback-probe.dem"
    repo_demo.parent.mkdir(parents=True, exist_ok=True)
    repo_demo.write_bytes(b"x")
    monkeypatch.setattr(cb, "_resolve_demo_for_map", lambda *a, **k: repo_demo)

    demo = tmp_path / "d.dem"
    demo.write_bytes(b"x")
    ratings = tmp_path / "r.json"
    ratings.write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(SystemExit):
        import asyncio
        asyncio.run(cb.create_backlog_entry(
            match_url="https://www.hltv.org/matches/1/x",
            match_slug="slug",
            player="dem0n",
            map_name="Cache",
            rating=2.15,
            kd="20-6",
            team="FUT",
            demo_path=tmp_path,
            ratings_path=ratings,
            tournament="PGL",
            demo_steamids={"dem0n": "76561198875633179"},
        ))


def test_player_page_first_video_block_wins_over_the_monitor_block():
    """prosettings repeats Resolution/Aspect Ratio (player, then monitor).

    The page-wide label map kept the LAST value, so dem0n resolved to
    1920x1080 16:9 (the monitor) instead of his real 1280x960 4:3 Stretched —
    the exact wrong-aspect render this whole module exists to prevent.
    """
    from bs4 import BeautifulSoup
    from scrapers.prosettings import scrape_player_video_settings

    html = """
    <html><body>
      <h2>Video Settings</h2>
      <table>
        <tr><th>Resolution</th><td>1280x960</td></tr>
        <tr><th>Aspect Ratio</th><td>4:3</td></tr>
        <tr><th>Scaling Mode</th><td>Stretched</td></tr>
      </table>
      <h2>Monitor</h2>
      <table>
        <tr><th>Resolution</th><td>1920x1080</td></tr>
        <tr><th>Aspect Ratio</th><td>16:9</td></tr>
      </table>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    with patch("scrapers.prosettings.requests.Session") as sess:
        resp = Mock(status_code=200, text=html)
        sess.return_value.get.return_value = resp
        out = scrape_player_video_settings("dem0n", session=sess.return_value)
    assert out["resolution"] == "1280x960"
    assert out["aspect_ratio"] == "4:3"
    assert out["scaling_mode"] == "Stretched"
