"""
CS2Archive — Player Account Manager

Persistent storage for player accounts linking Faceit and Steam profiles.
Uses JSON file storage following the same pattern as download_history.json.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from models import PlayerAccount

ACCOUNTS_FILE = Path("player_accounts.json")


def _load_accounts() -> list[dict]:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_accounts(records: list[dict]) -> None:
    ACCOUNTS_FILE.write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )


def extract_faceit_nickname(url: str) -> Optional[str]:
    m = re.search(r"faceit\.com/(?:\w+/)?players/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


def extract_steam_id(steam_url: str) -> str:
    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", steam_url)
    if m:
        return m.group(1)
    m = re.search(r"steamcommunity\.com/id/([^/?#]+)", steam_url)
    if m:
        vanity = m.group(1)
        try:
            with httpx.Client(follow_redirects=True, timeout=15) as client:
                resp = client.get(f"https://steamcommunity.com/id/{vanity}")
                final = str(resp.url)
                m2 = re.search(r"/profiles/(\d{17})", final)
                if m2:
                    return m2.group(1)
        except Exception:
            pass
    return ""


def add_account(
    nickname: str,
    faceit_url: str = "",
    steam_url: str = "",
) -> PlayerAccount:
    records = _load_accounts()
    now = datetime.now()
    faceit_nickname = extract_faceit_nickname(faceit_url) if faceit_url else ""
    steam_id = extract_steam_id(steam_url) if steam_url else ""

    existing = next((r for r in records if r["nickname"] == nickname), None)
    if existing:
        existing["faceit_url"] = faceit_url
        existing["faceit_nickname"] = faceit_nickname
        existing["steam_url"] = steam_url
        existing["steam_id"] = steam_id
        existing["updated_at"] = now
    else:
        records.append({
            "nickname": nickname,
            "faceit_url": faceit_url,
            "faceit_nickname": faceit_nickname,
            "steam_url": steam_url,
            "steam_id": steam_id,
            "created_at": now,
            "updated_at": now,
        })

    _save_accounts(records)
    return PlayerAccount(**records[-1] if not existing else existing)


def remove_account(nickname: str) -> bool:
    records = _load_accounts()
    new_records = [r for r in records if r["nickname"] != nickname]
    if len(new_records) == len(records):
        return False
    _save_accounts(new_records)
    return True


def list_accounts() -> list[PlayerAccount]:
    records = _load_accounts()
    return [PlayerAccount(**r) for r in records]


def get_account(nickname: str) -> Optional[PlayerAccount]:
    records = _load_accounts()
    for r in records:
        if r["nickname"] == nickname:
            return PlayerAccount(**r)
    return None
