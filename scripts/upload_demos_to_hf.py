"""Upload all .dem files to HuggingFace dataset in parallel with progress and resume."""
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from huggingface_hub import HfApi

REPO_ID = "cs2povarchive/cs2-demos"
TARGET = "iem_cologne_major_2026"
DEMOS_DIR = Path("demos/hltv")
STATE_FILE = Path("scripts/.upload_hf_state.json")
MAX_WORKERS = 15


def _load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("uploaded", []))
        except Exception:
            return set()
    return set()


def _save_state(uploaded: set[str]):
    STATE_FILE.write_text(json.dumps({"uploaded": sorted(uploaded)}, indent=2))


def _get_remote_files(api: HfApi) -> set[str]:
    """Fetch already-uploaded file paths from the HF repo."""
    try:
        items = api.list_repo_tree(REPO_ID, repo_type="dataset", path_in_repo=TARGET, recursive=True)
        return {item.path for item in items if item.path.endswith(".dem")}
    except Exception as e:
        print(f"  Could not list remote files (will rely on local state): {e}")
        return set()


def _hf_path(dem_path: Path, match_name: str) -> str:
    return f"{TARGET}/{match_name}/{dem_path.name}"


def upload_one(api: HfApi, dem_path: Path, match_name: str) -> tuple[bool, str, float]:
    hf = _hf_path(dem_path, match_name)
    size_gb = dem_path.stat().st_size / 1024**3
    t0 = time.time()
    try:
        api.upload_file(
            path_or_fileobj=str(dem_path.resolve()),
            path_in_repo=hf,
            repo_id=REPO_ID,
            repo_type="dataset",
        )
        elapsed = time.time() - t0
        speed = size_gb / elapsed * 8
        return True, f"{dem_path.name} ({size_gb:.2f} GB @ {speed:.1f} Gbps)", elapsed
    except Exception as e:
        elapsed = time.time() - t0
        return False, f"{dem_path.name}: {e}", elapsed


def main():
    api = HfApi()
    local_state = _load_state()
    remote_files = _get_remote_files(api)

    cleaned = 0
    pending: list[tuple[Path, str]] = []
    skipped = 0

    for match_dir in sorted(d for d in DEMOS_DIR.iterdir() if d.is_dir()):
        for dem_path in sorted(match_dir.glob("*.dem")):
            hf = _hf_path(dem_path, match_dir.name)
            key = str(dem_path.resolve())
            if key in local_state or hf in remote_files:
                dem_path.unlink()
                cleaned += 1
                skipped += 1
                continue
            pending.append((dem_path, match_dir.name))

        if not list(match_dir.glob("*.dem")) and not list(match_dir.glob("*.rar")):
            try:
                match_dir.rmdir()
            except OSError:
                pass

    if cleaned:
        print(f"  Cleaned {cleaned} already-uploaded files from disk.")

    if not pending:
        print(f"All {skipped} files already uploaded. Nothing to do.")
        return 0

    total = len(pending)
    print(f"{skipped} already skipped/cleaned, {total} remaining — uploading with {MAX_WORKERS} parallel workers...\n")

    done = 0
    failed: list[str] = []
    uploaded: set[str] = local_state.copy()
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(upload_one, api, p, m): p for p, m in pending}

        for fut in as_completed(fut_map):
            ok, msg, _ = fut.result()
            dem_path = fut_map[fut]
            done += 1
            elapsed_total = time.time() - t_start
            if ok:
                key = str(dem_path.resolve())
                uploaded.add(key)
                _save_state(uploaded)
                dem_path.unlink(missing_ok=True)
                print(f"  [{done}/{total}] OK  — {msg}  (total {elapsed_total:.0f}s)")
            else:
                print(f"  [{done}/{total}] FAIL — {msg}")
                failed.append(str(fut_map[fut]))

    t_total = time.time() - t_start
    print(f"\nDone in {t_total:.0f}s. Uploaded {total - len(failed)}/{total} files.")
    if failed:
        print(f"Failed ({len(failed)}):")
        for f in failed:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())