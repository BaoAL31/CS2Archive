"""The PiP-only overlay branch must consume the deferred-scale output.

With ``--defer-scale`` the overlay graph starts with a ``scale_cuda`` head that
produces ``[__base]``. Every downstream chain has to start from that label, not
from ``[0:v]`` — otherwise ``[0:v]`` is consumed twice and ffmpeg fails with
"Invalid stream specifier" / a disconnected output instead of producing the
video.

These tests drive the REAL ``run_overlay`` branch selection and the REAL
``_build_pip_chain``; only the GPU encode, the flight-clip renderer and the
CS2/CS2UtilArchive dependencies are stubbed. The captured filter graph is
checked for label connectivity, which is what actually broke.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import cs2archive.overlay.overlay_pov as op
from cs2archive.overlay.overlay_utilcams import PipClip

SOURCE_LABEL = re.compile(r"^\d+:[vas]")


def _chain_labels(chain: str) -> tuple[list[str], list[str]]:
    """Split one filter chain into its input labels and output labels."""
    inputs: list[str] = []
    rest = chain.strip()
    while rest.startswith("["):
        end = rest.index("]")
        inputs.append(rest[1:end])
        rest = rest[end + 1:]
    outputs = re.findall(r"\[([A-Za-z0-9_]+)\]", rest)
    return inputs, outputs


def _disconnected(graph: str) -> list[str]:
    """Labels consumed before anything produces them (source pads excluded)."""
    produced: set[str] = set()
    problems: list[str] = []
    for chain in graph.split(";"):
        if not chain.strip():
            continue
        inputs, outputs = _chain_labels(chain)
        for label in inputs:
            if SOURCE_LABEL.match(label):
                continue
            if label not in produced:
                problems.append(f"{label} consumed before it is produced "
                                f"(chain: {chain[:120]})")
        produced.update(outputs)
    return problems


def _make_av(path: Path, *, size: str = "640x480", duration: float = 6.0,
             min_bytes: int = 100_000) -> None:
    r = subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=s={size}:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:d={duration}",
         "-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", str(path)],
        capture_output=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert path.stat().st_size > min_bytes


def _write_sidecar(work_dir: Path, duration: float = 6.0) -> None:
    (work_dir / "video.round_offsets.json").write_text(json.dumps({
        "total_rounds": 1,
        "total_duration_seconds": duration,
        "round_offsets": {"1": 0.0},
        "per_round_durations": {"1": duration},
        "per_round_ticks": {"1": [0, int(duration * 64)]},
    }), encoding="utf-8")


@pytest.fixture
def pip_work(tmp_path):
    work = tmp_path / ".overlay_work"
    work.mkdir()
    source = tmp_path / "source.mp4"
    _make_av(source)
    video = work / "video.mp4"
    shutil.copyfile(source, video)
    _write_sidecar(work)
    clip = tmp_path / "flight.mp4"
    _make_av(clip, size="320x240", duration=2.0, min_bytes=1)
    demo = tmp_path / "demo.dem"
    demo.write_bytes(b"not a real demo")
    return SimpleNamespace(work=work, source=source, video=video,
                           clip=clip, demo=demo)


def _run_pip_only(pip_work, monkeypatch, *, target_size):
    """Drive run_overlay down the PiP-only branch and capture the graph."""
    captured: dict[str, object] = {}

    def fake_encode(main_input, extra_inputs, fc_args, out_label, output_path,
                    **_kw):
        graph = ""
        if fc_args and fc_args[0] == "-filter_complex":
            graph = fc_args[1]
        elif fc_args and fc_args[0] == "-filter_complex_script":
            graph = Path(fc_args[1]).read_text(encoding="utf-8")
        captured["graph"] = graph
        captured["out_label"] = out_label
        shutil.copyfile(pip_work.source, output_path)

    monkeypatch.setattr(op, "_ffmpeg_encode", fake_encode)
    monkeypatch.setattr(op, "_concat_overlay_batches", lambda *a, **k: None)
    monkeypatch.setattr(op, "_load_player_throws", lambda *a, **k: [])
    monkeypatch.setattr(op, "_load_round_tick_ranges", lambda *a, **k: {})
    monkeypatch.setattr(
        op, "_render_throw_flight_clips",
        lambda *a, **k: [PipClip(clip_path=pip_work.clip, start_frame=30,
                                 end_frame=90, util_type="smoke", pip_index=0)])

    op.run_overlay(
        pip_work.video,
        pip_work.demo,
        "76561198041683378",
        None,
        0,                      # batches=0 -> non-batched branches
        util_cams_root=pip_work.work / "utility_cams",
        work_dir=pip_work.work,
        keyboard=False,         # PiP-only
        freeze=False,
        force_rebuild=True,
        target_size=target_size,
    )
    return captured


def test_pip_only_branch_consumes_the_scale_output(pip_work, monkeypatch, capsys):
    captured = _run_pip_only(pip_work, monkeypatch, target_size=(2560, 1440))
    graph = str(captured["graph"])
    assert "scale_cuda=2560:1440" in graph
    assert "[__base]" in graph
    assert _disconnected(graph) == [], _disconnected(graph)
    # [0:v] is the scale head's input and nothing else's.
    assert graph.count("[0:v]") == 1, graph[:400]
    assert "PiP composite" in capsys.readouterr().out


def test_pip_only_branch_without_scaling_starts_from_the_input(pip_work, monkeypatch):
    captured = _run_pip_only(pip_work, monkeypatch, target_size=None)
    graph = str(captured["graph"])
    assert "scale_cuda" not in graph
    assert "[__base]" not in graph
    assert _disconnected(graph) == [], _disconnected(graph)
    assert graph.count("[0:v]") == 1, graph[:400]


def test_build_pip_chain_honours_an_explicit_start_label(pip_work):
    clip = PipClip(clip_path=pip_work.clip, start_frame=0, end_frame=30,
                   util_type="smoke", pip_index=0)
    parts, _current, _sorted = op._build_pip_chain(
        [clip], 2560, 1440, 60.0, start_label="[__base]")
    chain = ";".join(parts)
    assert "[__base]" in chain
    assert "[0:v]" not in chain
    # The chain is a fragment: prepend the scale head that produces [__base]
    # and check the whole graph is connected.
    head = ("[0:v]scale_cuda=2560:1440:interp_algo=lanczos:format=nv12,"
            "hwdownload,format=nv12[__base]")
    assert _disconnected(f"{head};{chain}") == [], _disconnected(f"{head};{chain}")


def test_pip_dressing_is_sized_for_the_canvas_not_the_input(pip_work, monkeypatch):
    """The mask/shadow sprites must match the SCALED clip, not the raw input.

    With the deferred upscale the input is still at capture resolution while
    the PiP content is composited at export size. An input-sized mask makes
    ``alphamerge`` fail on a size mismatch (464x464 vs 144x144), so the whole
    overlay encode dies.
    """
    from PIL import Image

    _run_pip_only(pip_work, monkeypatch, target_size=(2560, 1440))

    body = op._pip_body(1440)
    inner = body - 2 * op.PIP_OUTLINE_THICKNESS
    mask = pip_work.work / f"pip_mask_inner_{inner}.png"
    shadow = pip_work.work / f"pip_shadow_{body}.png"
    assert mask.is_file(), sorted(p.name for p in pip_work.work.iterdir())
    assert shadow.is_file(), sorted(p.name for p in pip_work.work.iterdir())
    with Image.open(mask) as im:
        assert im.size == (inner, inner)
    with Image.open(shadow) as im:
        assert im.size[0] >= body and im.size[1] >= body


def test_pip_dressing_without_scaling_uses_the_input_height(pip_work, monkeypatch):
    from PIL import Image

    _run_pip_only(pip_work, monkeypatch, target_size=None)

    body = op._pip_body(480)          # the fixture's input height
    inner = body - 2 * op.PIP_OUTLINE_THICKNESS
    mask = pip_work.work / f"pip_mask_inner_{inner}.png"
    assert mask.is_file(), sorted(p.name for p in pip_work.work.iterdir())
    with Image.open(mask) as im:
        assert im.size == (inner, inner)
