#!/usr/bin/env python3
"""Upload CS2 demos to HuggingFace dataset using git-lfs."""
import os
import subprocess
from pathlib import Path
import shutil

HF_REPO = "https://huggingface.co/datasets/cs2povarchive/cs2-demos"
HF_REPO_NAME = "cs2povarchive/cs2-demos"
LOCAL_REPO = Path("cs2-demos-hf")
DEMOS_ROOT = Path("demos/hltv")
TARGET = "iem_cologne_major_2026"
EXCLUDE = {"g2-vs-spirit-iem-cologne-major"}

def run(cmd, check=True):
    print(f"RUN: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if check and result.returncode != 0:
        print(f"FAIL: {result.stderr}")
        raise RuntimeError(result.stderr)
    return result

def main():
    # Clone repo
    if LOCAL_REPO.exists():
        print(f"Removing existing {LOCAL_REPO}")
        shutil.rmtree(LOCAL_REPO)
    
    run(f"git lfs install")
    run(f"git clone {HF_REPO} {LOCAL_REPO}")
    
    # Copy demo folders
    for folder in DEMOS_ROOT.iterdir():
        if not folder.is_dir():
            continue
        if folder.name in EXCLUDE:
            print(f"SKIP: {folder.name} (in use)")
            continue
        
        target_dir = LOCAL_REPO / TARGET / folder.name
        print(f"COPY: {folder.name} -> {target_dir}")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, target_dir, dirs_exist_ok=True)
    
    # Commit and push
    os.chdir(LOCAL_REPO)
    run("git add -A")
    run(f'git commit -m "Add IEM Cologne Major 2026 demos"', check=False)
    run("git push")

if __name__ == "__main__":
    main()