import json, sys, os
from pathlib import Path
os.environ['HF_HOME'] = 'D:/.cache/huggingface'
os.environ['HF_HUB_CACHE'] = 'D:/.cache/huggingface/hub'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from huggingface_hub import HfApi
api = HfApi()
items = list(api.list_repo_tree("cs2povarchive/cs2-demos", repo_type="dataset",
                                path_in_repo="iem_cologne_major_2026", recursive=True))
out = {"count": len(items), "paths": [i.path for i in items]}
Path("D:\\Projects\\CS2Archive\\scripts\\_hf_output.json").write_text(json.dumps(out, indent=2))
print(f"Wrote {len(items)} items to _hf_output.json")
