import json
from pathlib import Path
from typing import Any, Optional, List, Dict
from . import config as C

# ---- generic JSON helpers
def read_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def write_json(path: Path, obj: Any):
    try:
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    except Exception:
        pass

# ---- applist cache
def read_applist():
    return read_json(C.APPLIST_CACHE, default=[])

def write_applist(apps):
    write_json(C.APPLIST_CACHE, apps)

def applist_mtime() -> Optional[float]:
    return C.APPLIST_CACHE.stat().st_mtime if C.APPLIST_CACHE.exists() else None

# ---- candidate pool
def read_pool() -> List[Dict]:
    return read_json(C.POOL_PATH, default=[]) or []

def write_pool(pool: List[Dict]):
    write_json(C.POOL_PATH, pool)

def read_pool_meta() -> Dict:
    return read_json(C.POOL_META_PATH, default={}) or {}

def write_pool_meta(meta: Dict):
    write_json(C.POOL_META_PATH, meta)

# ---- appstats (per-app cached details+summary)
def read_appstats(appid: int) -> Optional[Dict]:
    p = C.APPSTATS_DIR / f"{appid}.json"
    return read_json(p, default=None)

def write_appstats(appid: int, data: Dict):
    p = C.APPSTATS_DIR / f"{appid}.json"
    write_json(p, data)

# ---- seen history
def read_seen(max_keep=500) -> List[int]:
    data = read_json(C.SEEN_PATH, default={"seen_appids": []}) or {}
    return (data.get("seen_appids") or [])[-max_keep:]

def write_seen(ids: List[int]):
    write_json(C.SEEN_PATH, {"seen_appids": ids[-500:]})

# ---- summary text cache
def read_summary(appid: int, kind: str) -> Optional[str]:
    p = C.SUM_CACHE_DIR / f"{appid}_{kind}.txt"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return None
    return None

def write_summary(appid: int, kind: str, text: str):
    p = C.SUM_CACHE_DIR / f"{appid}_{kind}.txt"
    try:
        p.write_text(text, encoding="utf-8")
    except Exception:
        pass
