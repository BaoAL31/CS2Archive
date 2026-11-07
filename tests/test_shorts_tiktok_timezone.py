import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cs2archive.upload.upload_youtube_shorts import _run_tiktok


def test_tiktok_web_uses_sydney_wall_time_from_committed_utc_slot():
    flow = Mock()
    module = SimpleNamespace(DEFAULT_PROFILE_DIR='.test-profile', run_schedule_flow=flow)
    meta = {'publish_at_utc': '2099-10-31T07:00:00Z', 'publish_timezone': 'Australia/Sydney'}
    with patch('cs2archive.upload.upload_youtube_shorts._read_meta', return_value=meta), \
         patch('cs2archive.upload.upload_youtube_shorts._pin_tiktok_fingerprint'), \
         patch('cs2archive.upload.upload_youtube_shorts._write_meta'), \
         patch('cs2archive.overlay._common.prefer_cs2util_scripts'), \
         patch.dict(sys.modules, {'tiktok_studio_navigator': module}):
        _run_tiktok(Path('short.mp4'), 'title', None, None, 'meta.json')
    assert flow.call_args.kwargs['schedule_date'] == '2099-10-31'
    assert flow.call_args.kwargs['schedule_time'] == '18:00'
