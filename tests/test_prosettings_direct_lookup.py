"""A player missing from the prosettings LIST still has a settings page.

2026-11-07: the dem0n Cache card said 1920x1080 / 16:9 Native — prosettings'
DEFAULT — because ``resolve_video_settings`` only consulted the scraped LIST
cache and dem0n is not on it. His real settings are 1280x960 / 4:3 Stretched, so
the render was started at the wrong aspect and had to be killed.
"""
from unittest.mock import patch

from scrapers import prosettings


def test_list_miss_falls_back_to_the_direct_player_page():
    direct = {"resolution": "1280x960", "aspect_ratio": "4:3",
              "scaling_mode": "Stretched", "width": 1280, "height": 960,
              "source": "prosettings"}
    with patch.object(prosettings, "load_cache", return_value={}), \
         patch.object(prosettings, "scrape_and_cache", return_value={}), \
         patch.object(prosettings, "scrape_player_video_settings",
                      return_value=direct, create=True) as direct_scrape, \
         patch.object(prosettings, "scrape_player_viewmodel", return_value={}):
        out = prosettings.resolve_video_settings("dem0n", fetch_viewmodel=False)
    assert out["resolution"] == "1280x960"
    assert out["aspect_ratio"] == "4:3"
    assert out["scaling_mode"] == "Stretched"
    assert direct_scrape.called


def test_unknown_player_hard_fails_instead_of_using_a_default():
    """A silent 1920x1080 default is how dem0n got rendered at the wrong aspect.

    A wrong-aspect POV is worse than no POV: it costs a full render and is only
    caught by eye. The resolver must raise, not guess.
    """
    import pytest

    with patch.object(prosettings, "load_cache", return_value={}), \
         patch.object(prosettings, "scrape_and_cache", return_value={}), \
         patch.object(prosettings, "scrape_player_video_settings",
                      return_value=None, create=True), \
         patch.object(prosettings, "scrape_player_viewmodel", return_value={}):
        with pytest.raises(prosettings.VideoSettingsUnresolved) as exc:
            prosettings.resolve_video_settings("nobody-xyz", fetch_viewmodel=False)
    assert "nobody-xyz" in str(exc.value)
