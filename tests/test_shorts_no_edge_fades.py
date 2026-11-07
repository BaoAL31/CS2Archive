from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2archive.shorts import render_shorts


@pytest.mark.parametrize('kill_feed', [False, True])
@pytest.mark.parametrize('use_cpu', [False, True])
def test_short_composite_preserves_unfaded_picture_and_audio(monkeypatch, tmp_path, kill_feed, use_cpu):
    commands = []
    output = tmp_path/'short.mp4'
    output.write_bytes(b'encoded')
    def capture(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(render_shorts.subprocess, 'run', capture)
    render_shorts._composite_9x16(
        Path('source.mp4'), output, kill_feed=kill_feed,
        use_cpu=use_cpu, src_duration=15.0,
    )
    command = commands[0]
    graph = command[command.index('-filter_complex')+1]
    assert 'fade=' not in graph
    assert '-af' not in command
    assert command[command.index('-c:a')+1] == 'copy'
