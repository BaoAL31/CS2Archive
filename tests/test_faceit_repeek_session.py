"""Demo download must not close the room until Repeek capture also finished."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scrapers.faceit import hold_browser_until_both, run_download_then_repeek
from scrapers.repeek_snapshot import RepeekCaptureError


def test_hold_refuses_to_close_if_download_missing():
    with pytest.raises(RepeekCaptureError) as ei:
        hold_browser_until_both(download_ok=False, repeek_ok=True)
    assert ei.value.code == "FACEIT_DOWNLOAD"


def test_hold_refuses_to_close_if_repeek_missing():
    with pytest.raises(RepeekCaptureError) as ei:
        hold_browser_until_both(download_ok=True, repeek_ok=False)
    assert ei.value.code == "REPEEK_INCOMPLETE"


def test_hold_allows_close_only_when_both_ok():
    hold_browser_until_both(download_ok=True, repeek_ok=True)


def test_download_then_repeek_runs_capture_before_close(monkeypatch: pytest.MonkeyPatch):
    order: list[str] = []

    def download(_page):
        order.append("download")
        return Path("demo.zst")

    def capture(_page, _match_id):
        order.append("repeek")
        return Path("strips")

    def close():
        order.append("close")

    monkeypatch.setattr("scrapers.faceit._capture_repeek_roster", capture)
    saved = run_download_then_repeek(
        page=object(),
        match_id="1-abc",
        download_fn=download,
        close_fn=close,
    )
    assert saved.name == "demo.zst"
    assert order == ["download", "repeek", "close"]


def test_close_not_called_early_if_repeek_fails(monkeypatch: pytest.MonkeyPatch):
    order: list[str] = []

    def download(_page):
        order.append("download")
        return Path("demo.zst")

    def capture(_page, _match_id):
        order.append("repeek")
        raise RepeekCaptureError("REPEEK_CARD_COUNT", "got 9")

    def close():
        order.append("close")

    monkeypatch.setattr("scrapers.faceit._capture_repeek_roster", capture)
    with pytest.raises(RepeekCaptureError) as ei:
        run_download_then_repeek(
            page=object(),
            match_id="1-abc",
            download_fn=download,
            close_fn=close,
        )
    assert ei.value.code == "REPEEK_CARD_COUNT"
    # finally-equivalent: close still runs after both were attempted
    assert order == ["download", "repeek", "close"]


def test_close_not_called_if_download_fails_before_repeek(monkeypatch: pytest.MonkeyPatch):
    order: list[str] = []

    def download(_page):
        order.append("download")
        return None

    def capture(_page, _match_id):
        order.append("repeek")
        return Path("strips")

    def close():
        order.append("close")

    monkeypatch.setattr("scrapers.faceit._capture_repeek_roster", capture)
    with pytest.raises(RepeekCaptureError) as ei:
        run_download_then_repeek(
            page=object(),
            match_id="1-abc",
            download_fn=download,
            close_fn=close,
        )
    assert ei.value.code == "FACEIT_DOWNLOAD"
    assert order == ["download", "close"]
    assert "repeek" not in order
