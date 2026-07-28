import json, tempfile, subprocess, sys
from pathlib import Path
CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
demo = "demos/hltv/2396014-100-thieves-vs-spirit-blast-bounty-2026-season-2/100-thieves-vs-spirit-m3-dust2.dem"
with tempfile.TemporaryDirectory() as tmp:
    cmd = [CSDM, "json", demo, "--output-folder", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("FAILED:", r.stderr[:500])
        sys.exit(1)
    jf = list(Path(tmp).glob("*.json"))
    if not jf:
        print("No JSON output")
        sys.exit(1)
    data = json.loads(jf[0].read_text(encoding="utf-8"))
    rounds = data.get("rounds", [])
    print(f"Total rounds: {len(rounds)}")
    for r in rounds[:5]:
        print(f'  Round {r["number"]}: tick {r.get("startTick","?")} -> {r.get("endTick","?")}')
    if rounds:
        print(f'  Last round: {rounds[-1]["number"]}')
