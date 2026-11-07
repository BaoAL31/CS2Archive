"""csdm 3.20.1 can fail with `unknown demo source` while exiting 0.

The retry in extract_steamids used to require a non-zero exit code, so the
fallback never fired and PGL (PBDEMS2) demos silently produced zero players —
the 2026-10-31 grand-final backlog died on "Missing required fields ...
['steam_id']" even though every demo was on disk.
"""
from pathlib import Path
from unittest.mock import Mock

from cs2archive.pov.extract_steamids import extract_steamids


def _csdm(returncodes, stdout, stderr, json_name="demo.json"):
    """Mock csdm.cmd: each call pops the next returncode; writes JSON on export."""
    calls = {"n": 0}
    tmp = Mock()
    tmp.glob.return_value = [Mock()] if json_name else []

    def run(cmd, capture_output, text, timeout):
        rc = returncodes[calls["n"]]
        calls["n"] += 1
        if rc == 0 and json_name:
            (Mock(),)
        return Mock(returncode=rc, stdout=stdout, stderr=stderr)

    return run, calls, tmp


def test_zero_exit_with_unknown_source_still_retries(monkeypatch, tmp_path):
    """csdm exits 0 with 'unknown demo source' on stderr: must retry with --source."""
    from cs2archive.pov import extract_steamids as mod

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):
        seen_cmds.append(list(cmd))
        if "--source" in cmd:
            # extract_steamids globs the TemporaryDirectory(dir=TMP_DIR) it
            # opened; the cmd's --output-folder names that directory.
            out_dir = Path(cmd[cmd.index("--output-folder") + 1])
            (out_dir / "demo.json").write_text(
                '{"players": [{"name": "khaN", "steamId": "765"}]}',
                encoding="utf-8")
            return Mock(returncode=0, stdout="Exported 1 matches", stderr="")
        return Mock(returncode=0, stdout="No demos to export",
                    stderr="Error analyzing demo: unknown demo source, please "
                           "specify the source with the -source flag")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    demo = tmp_path / "demo.dem"
    demo.write_bytes(b"x")
    monkeypatch.setattr(mod, "TMP_DIR", tmp_path)

    players = extract_steamids(str(demo))
    assert len(seen_cmds) == 2, "must retry with --source challengermode"
    assert seen_cmds[1][-2:] == ["--source", "challengermode"]
    assert players == {"khaN": "765"}


def test_plain_failure_without_unknown_source_returns_empty(monkeypatch, tmp_path):
    from cs2archive.pov import extract_steamids as mod

    def fake_run(cmd, capture_output, text, timeout):
        return Mock(returncode=0, stdout="No demos to export",
                    stderr="Error analyzing demo: corrupted file")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    demo = tmp_path / "demo.dem"
    demo.write_bytes(b"x")
    monkeypatch.setattr(mod.Path, "exists", lambda self: True)
    monkeypatch.setattr(mod, "TMP_DIR", tmp_path)

    assert extract_steamids(str(demo)) == {}
