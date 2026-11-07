"""The pipeline's step-2 gate must not run before HLAE is adapted.

Bug (2026-11-05): ``ensure_hlae_for_cs2()`` lives inside ``render_pov.py``, but
``pipeline.step_render()`` calls ``assert_render_versions()`` BEFORE spawning
render_pov. With CSDM's settings.json still naming an HLAE for a previous CS2
build, the gate hard-failed RENDER_HLAE_CS2_MISMATCH and the auto-adapt below it
never ran — three wasted daemon cycles on wicadia Cache.
"""
from pathlib import Path


def test_pipeline_adapts_hlae_before_the_gate(monkeypatch, tmp_path):
    """step_render must call ensure_hlae_for_cs2() before assert_render_versions()."""
    from cs2archive.pov import pipeline
    from cs2archive.pov import render_version_check as rvc

    order: list[str] = []

    demo = tmp_path / "d.dem"
    demo.write_bytes(b"x")

    monkeypatch.setattr(
        rvc, "ensure_hlae_for_cs2",
        lambda **k: order.append("ensure") or ((2, 192, 8),
                                               tmp_path / "HLAE.exe", "pin"))
    monkeypatch.setattr(
        rvc, "assert_render_versions",
        lambda *a, **k: order.append("gate") or {"demo": "1.41.9.1", "cs2": "1.41.9.2",
                                                 "hlae": "2.192.8", "csdm": "3.20.1"})

    obj = pipeline.Pipeline.__new__(pipeline.Pipeline)
    obj.demo_path = demo
    obj.args = type("A", (), {"hlaedbg": False})()
    obj.meta = {}
    obj.run_id = "t"
    obj.player = "p"
    obj.map_name = "m"
    obj.steam_id = "1"

    # Stop right after the version gate: the NVENC probe / render spawn would
    # need real hardware, and the ordering is the whole point here.
    def boom(*a, **k):
        raise SystemExit(0)

    monkeypatch.setattr(pipeline.subprocess, "run", boom)

    try:
        obj.step_render()
    except SystemExit:
        pass

    assert "ensure" in order, "step_render must adapt HLAE itself"
    assert order.index("ensure") < order.index("gate"), (
        f"HLAE must be adapted BEFORE the version gate, got {order}")
