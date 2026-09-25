"""Prosettings crosshair: scrape data-field rows, map to cvars, prefer over demo."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from scrapers.prosettings import (  # noqa: E402
    crosshair_convars,
    crosshair_summary,
    resolve_crosshair,
    scrape_player_crosshair,
)

_FIXTURE = """
<section><h3>Crosshair</h3><table class="settings"><tbody>
<tr data-field="cl_crosshairstyle"><th>Style</th><td>Classic Static</td></tr>
<tr data-field="cl_crosshair_recoil"><th>Follow Recoil</th><td>No</td></tr>
<tr data-field="cl_crosshairdot"><th>Dot</th><td>No</td></tr>
<tr data-field="cl_crosshairsize"><th>Length</th><td>1</td></tr>
<tr data-field="cl_crosshairthickness"><th>Thickness</th><td>1.5</td></tr>
<tr data-field="cl_crosshairgap"><th>Gap</th><td>-4</td></tr>
<tr data-field="cl_crosshair_drawoutline"><th>Outline</th><td>No</td></tr>
<tr data-field="cl_crosshaircolor"><th>Color</th><td>Custom</td></tr>
<tr data-field="cl_crosshaircolor_r"><th>Red</th><td>0</td></tr>
<tr data-field="cl_crosshaircolor_g"><th>Green</th><td>255</td></tr>
<tr data-field="cl_crosshaircolor_b"><th>Blue</th><td>165</td></tr>
<tr data-field="cl_crosshairusealpha"><th>Alpha</th><td>Yes</td></tr>
<tr data-field="cl_crosshairalpha"><th>Alpha Value</th><td>255</td></tr>
<tr data-field="cl_crosshair_t"><th>T Style</th><td>No</td></tr>
<tr data-field="cl_crosshairgap_useweaponvalue"><th>Deployed Weapon Gap</th><td>No</td></tr>
</tbody></table></section>
"""


class _Resp:
    status_code = 200
    text = _FIXTURE


class _Sess:
    def get(self, *a, **k):
        return _Resp()


def test_scrape_reads_data_field_rows():
    out = scrape_player_crosshair("donk", session=_Sess())

    assert out["cl_crosshairstyle"] == "Classic Static"
    assert out["cl_crosshairgap"] == "-4"
    assert out["cl_crosshaircolor_g"] == "255"
    assert out["cl_crosshairusealpha"] == "Yes"


def test_convars_match_donk_prosettings():
    out = scrape_player_crosshair("donk", session=_Sess())
    cvars = crosshair_convars(out)

    assert "cl_crosshairstyle 4" in cvars
    assert "cl_crosshairsize 1" in cvars
    assert "cl_crosshairthickness 1.5" in cvars
    assert "cl_crosshairgap -4" in cvars
    assert "cl_crosshair_drawoutline 0" in cvars
    assert "cl_crosshaircolor 5" in cvars
    assert "cl_crosshaircolor_r 0" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 165" in cvars
    assert "cl_crosshairusealpha 1" in cvars
    assert "cl_crosshair_recoil 0" in cvars
    assert len(cvars) == len(set(cvars))


def test_custom_colors_use_exact_rgb():
    cvars = crosshair_convars({
        "cl_crosshaircolor": "Custom", "cl_crosshaircolor_r": "0",
        "cl_crosshaircolor_g": "255", "cl_crosshaircolor_b": "255"})
    assert "cl_crosshaircolor 5" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 255" in cvars
    assert not [c for c in cvars if re.match(r"cl_crosshaircolor [0-4]$", c)]


def test_named_colors_and_unknown_style():
    assert "cl_crosshaircolor 1" in crosshair_convars({"cl_crosshaircolor": "Green"})
    assert "cl_crosshaircolor 4" in crosshair_convars({"cl_crosshaircolor": "Cyan"})
    cvars = crosshair_convars({"cl_crosshairstyle": "Something New", "cl_crosshairsize": "2"})
    assert "cl_crosshairsize 2" in cvars
    assert not [c for c in cvars if c.startswith("cl_crosshairstyle")]
    assert crosshair_convars({}) == []


def test_summary_names_style_size_color():
    out = scrape_player_crosshair("donk", session=_Sess())

    assert crosshair_summary(out) == "Classic Static, size 1, thickness 1.5, gap -4, custom rgb(0,255,165)"
    assert crosshair_summary({}) is None


def test_resolve_retries_transient_scrape_failures(monkeypatch):
    import time

    import scrapers.prosettings as ps

    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def flaky(nick, session=None):
        calls.append(nick)
        if len(calls) < 3:
            raise ConnectionError("flaky")
        return {"cl_crosshairsize": "2"}

    monkeypatch.setattr(ps, "scrape_player_crosshair", flaky)
    out = ps.resolve_crosshair_settings("donk")

    assert out == {"cl_crosshairsize": "2"}
    assert calls == ["donk"] * 3


def test_resolve_prefers_prosettings_then_demo(monkeypatch):
    import scrapers.prosettings as ps

    monkeypatch.setattr(ps, "scrape_player_crosshair", lambda nick, session=None: {"cl_crosshairsize": "2"})
    called = []
    cvars, info = ps.resolve_crosshair("donk", lambda: called.append(1) or ["demo"])
    assert cvars == ["cl_crosshairsize 2"]
    assert info["source"] == "prosettings"
    assert called == []

    monkeypatch.setattr(ps, "scrape_player_crosshair", lambda nick, session=None: {})
    cvars, info = ps.resolve_crosshair("donk", lambda: ["demo"])
    assert cvars == ["demo"]
    assert info["source"] == "demo"

    cvars, info = ps.resolve_crosshair("", lambda: [])
    assert cvars == [] and info["source"] == "none"
