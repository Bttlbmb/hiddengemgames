import math
import time
from typing import Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter, Retry
from . import config as C
from . import storage as S

# polite session with retries + backoff
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": C.USER_AGENT})
retries = Retry(
    total=5, connect=3, read=3,
    backoff_factor=C.RETRY_BACKOFF,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=["GET","HEAD"]
)
SESSION.mount("https://", HTTPAdapter(max_retries=retries))

_last_call = 0.0
def _rate_limit():
    global _last_call
    wait = _last_call + C.RATE_LIMIT_S - time.monotonic()
    if wait > 0: time.sleep(wait)
    _last_call = time.monotonic()

def get_applist() -> List[Dict]:
    # network try
    try:
        _rate_limit()
        r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
        r.raise_for_status()
        apps = (r.json().get("applist", {}) or {}).get("apps", [])
        if apps:
            S.write_applist(apps)
            return apps
    except Exception as e:
        print(f"[warn] GetAppList failed: {e}")

    # cache fallback (even if stale)
    apps = S.read_applist()
    if apps:
        mt = S.applist_mtime()
        age = int(time.time() - mt) if mt else -1
        print(f"[info] Using cached app list (age ~{age}s)")
        return apps
    return []

def get_appdetails(appid: int) -> Optional[Dict]:
    _rate_limit()
    r = SESSION.get("https://store.steampowered.com/api/appdetails",
                    params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    item = j.get(str(appid)) or {}
    return item.get("data") if item.get("success") else None

def get_review_summary(appid: int) -> Dict:
    _rate_limit()
    r = SESSION.get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all",
                "filter": "summary", "num_per_page": 1},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("query_summary", {}) or {}

def fetch_review_texts(appid: int, num=20) -> List[str]:
    out: List[str] = []
    cursor = "*"
    while len(out) < num:
        _rate_limit()
        r = SESSION.get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={
                "json": 1, "language": "english", "purchase_type": "all",
                "filter": "recent", "num_per_page": 20, "cursor": cursor,
            },
            timeout=30,
        )
        if r.status_code != 200: break
        j = r.json()
        reviews = j.get("reviews") or []
        if not reviews: break
        for rv in reviews:
            txt = (rv.get("review") or "").strip()
            if txt: out.append(txt)
            if len(out) >= num: break
        cursor = j.get("cursor")
        if not cursor: break
    return out[:num]
