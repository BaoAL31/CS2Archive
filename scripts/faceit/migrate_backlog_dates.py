from __future__ import annotations
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.faceit.backlog_paths import match_date_for_demo, faceit_backlog_dir
ROOT=Path(__file__).resolve().parents[2]; old=ROOT/'backlog'/'faceit'
for p in list(old.glob('*/*.json')):
 d=json.loads(p.read_text()); demo=ROOT/d['demo_path']; date=d.get('match_date') or match_date_for_demo(demo if demo.exists() else p)
 d['match_date']=date; pri=d.get('priority',p.parent.name); out=faceit_backlog_dir(ROOT/'backlog',date,pri)/p.name; out.parent.mkdir(parents=True,exist_ok=True)
 d['pipeline_cmd']=d.get('pipeline_cmd','').replace(f'backlog/faceit/{pri}/',f'backlog/faceit/{date}/{pri}/')
 out.write_text(json.dumps(d,indent=2)+'\n'); p.unlink()
for p in list(old.iterdir()):
 if p.is_dir() and not any(p.iterdir()): p.rmdir()
print('migrated')
