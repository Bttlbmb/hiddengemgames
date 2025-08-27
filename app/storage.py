# app/storage.py
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone

# Base data dir used by the project
DATA_DIR = Path("content/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Back-compat aliases for old code that expects these names
CANDIDATE_POOL_PATH = POOL_PATH
CANDIDATE_POOL_META_PATH = POOL_META_PATH

# Default file locations
POOL_PATH       = DATA_DIR / "candidate_pool.json"
POOL_META_PATH  = DATA_DIR / "pool_meta.json"
APPLIST_PATH    = DATA_DIR / "applist.json"
APPSTATS_DIR    = DATA_DIR / "appstats"
SUMMARIES_DIR   = DATA_DIR / "summaries"

APPSTATS_DIR.mkdir(parents=True, exist_ok=True)
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)


# --------------------
# Generic JSON helpers
# --------------------
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as f:
        tmp = Path(f.name)
        f.write(text)
    tmp.replace(path)


def save_json(path: Path, obj: Any) -> None:
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2))


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# -------------
# Pool handling
# -------------
def save_candidate_pool(pool: dict) -> None:
    """
    Persist the candidate pool and lightweight metadata.
    """
    now = datetime.now(timezone.utc).isoformat()
    # store pool
    save_json(POOL_PATH, pool)
    # store metadata (last refresh timestamp + counts)
    meta = {
        "last_refreshed": now,
        "size": len(pool.get("candidates", [])) if isinstance(pool, dict) else None,
    }
    save_json(POOL_META_PATH, meta)


def load_candidate_pool(default: Optional[dict] = None) -> Optional[dict]:
    return load_json(POOL_PATH, default=default)


# -----------------
# App list / stats
# -----------------
def save_applist(applist: list[dict]) -> None:
    save_json(APPLIST_PATH, {"apps": applist})


def load_applist() -> list[dict]:
    data = load_json(APPLIST_PATH, default={"apps": []})
    return data.get("apps", [])


def appstats_path(appid: int | str) -> Path:
    return APPSTATS_DIR / f"{appid}.json"


def load_appstats(appid: int | str, default: Any = None) -> Any:
    return load_json(appstats_path(appid), default=default)


def save_appstats(appid: int | str, stats: Any) -> None:
    save_json(appstats_path(appid), stats)


# -----------------
# Summaries (AI)
# -----------------
def summaries_path(appid: int | str) -> Path:
    return SUMMARIES_DIR / f"{appid}.json"


def load_summary(appid: int | str, default: Any = None) -> Any:
    return load_json(summaries_path(appid), default=default)


def save_summary(appid: int | str, data: Any) -> None:
    save_json(summaries_path(appid), data)


# -----------------
# Small TTL helper
# -----------------
def is_fresh(path: Path, ttl_seconds: int) -> bool:
    """
    Return True if 'path' exists and its mtime is within ttl_seconds.
    """
    try:
        if not path.exists():
            return False
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        return age <= ttl_seconds
    except Exception:
        return False
