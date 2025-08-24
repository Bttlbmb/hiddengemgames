#!/usr/bin/env python3
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from random import SystemRandom
rng = SystemRandom()  # better randomness, no seeding

LOCAL_TZ = ZoneInfo("Europe/Berlin")  # change as you like
POST_DIR = Path("content/posts")
DATA_DIR = Path("content/data")
SEEN_PATH = DATA_DIR / "seen.json"
POST_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

def clean_text(s: str, max_len=240):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s

def get_applist():
    r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
    r.raise_for_status()
    return r.json().get("applist", {}).get("apps", [])

def get_appdetails(appid: int):
    r = SESSION.get("https://store.steampowered.com/api/appdetails", params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    item = j.get(str(appid)) or {}
    return item.get("data") if item.get("success") else None

def get_review_summary(appid: int):
    r = SESSION.get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all", "filter": "summary", "num_per_page": 1},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("query_summary", {}) or {}

def load_seen(max_keep=500):
    if SEEN_PATH.exists():
        try:
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            ids = data.get("seen_appids", [])
            return ids[-max_keep:]
        except Exception:
            return []
    return []

def save_seen(seen):
    SEEN_PATH.write_text(json.dumps({"seen_appids": seen[-500:]}, indent=2), encoding="utf-8")

def pick_game(apps, seen_set, tries=200):
    # Try up to N times to find a released GAME not in seen_set.
    for _ in range(tries):
        app = rng.choice(apps)
        appid = app.get("appid")
        if not appid or appid in seen_set:
            continue
        data = get_appdetails(appid)
        if not data or data.get("type") != "game":
            continue
        rd = data.get("release_date") or {}
        if rd.get("coming_soon"):
            continue
        if not data.get("name") or not data.get("header_image"):
            continue
        return appid, data
    return None, None

def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")
    post_path = POST_DIR / f"{slug_ts}-auto.md"

    try:
