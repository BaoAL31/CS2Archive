#!/usr/bin/env python3
"""Upload CS2 demos to HuggingFace dataset."""
import os
import subprocess
from pathlib import Path
from huggingface_hub import HfApi, login

HF_REPO = "cs2povarchive/cs2-demos"
DEMOS_ROOT = Path("demos/hltv")
EXCLUDE_FOLDERS = {"g2-vs-spirit-iem-cologne-major"}  # Currently in use
TARGET_DIR = "iem_cologne_major_2026"

def main():
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("Set HF_TOKEN env var")
        return
    
    api = login(token=token)
    
    folders = sorted([f for f in DEMOS_ROOT.iterdir() if f.is_dir()])
    print(f"Found {len(folders)} folders")
    
    for folder in folders:
        if folder.name in EXCLUDE_FOLDERS:
            print(f"SKIP: {folder.name} (in use)")
            continue
        
        print(f"UPLOAD: {folder.name}")
        # Use huggingface_hub to upload folder
        api.upload_folder(
            folder_path=str(folder),
            repo_id=HF_REPO,
            path_in_repo=f"{TARGET_DIR}/{folder.name}",
            commit_message=f"Add {folder.name} demos"
        )
    print("Done")

if __name__ == "__main__":
    main()