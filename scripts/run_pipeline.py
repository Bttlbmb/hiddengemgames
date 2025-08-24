#!/usr/bin/env python3
from pathlib import Path
import datetime as dt
from zoneinfo import ZoneInfo
import os, textwrap, requests, random, re

LOCAL_TZ = ZoneInfo("Europe/Berlin")  # change if you want
POST_DIR = Path("content/posts")
POST_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

def clean(s, max_len=240):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s

def get_applist():
    r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
    r.raise_for_status()
    return r.json().get("applist", {}).get("apps", [])

def appdetails(appid: int):
    r = SESSION.get("https://store.steampowered.com/api/appdetails", params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return (j.get(str(appid)) or {}).get("data")

def review_summary(appid: int):
    r = SESSION.get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all", "filter": "summary", "num_per_page": 1},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("query_summary", {}) or {}

def pick_random_game(apps, tries=40):
    for _ in range(tries):
        a = random.choice(apps)
        appid = a.get("appid")
        if not appid:
            continue
        d = appdetails(appid)
        if not d or d.get("type") != "game":
            continue
        if (d.get("release_date") or {}).get("coming_soon"):
            continue
        if not d.get("name") or not d.get("header_image"):
            continue
        return appid, d
    return None, None

def main():
    # Unique timestamp (second precision) so every run writes a new file
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")
    fname = POST_DIR / f"{slug_ts}-auto.md"

    # Stable-ish randomness per hour for selection, but filename is unique per run
    random.seed(int(now_utc.strftime("%Y%m%d%H")))

    try:
        apps = get_applist()
    except Exception as e:
        apps = []
        print(f"[warn] GetAppList failed: {e}")

    appid, data = pick_random_game(apps) if apps else (None, None)

    if not appid:
        body = textwrap.dedent(f"""\
        Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
        Date: {now_local.strftime('%Y-%m-%d %H:%M')}
        Category: Games
        Tags: auto
        Slug: fallback-{slug_ts}

        Could not fetch Steam data this run. Will try again next hour.
        """)
        fname.write_text(body, encoding="utf-8")
        print(f"[ok] wrote fallback {fname}")
        return

    summ = {}
    try:
        summ = review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")

    name = data.get("name", f"App {appid}")
    short = clean(data.get("short_description", ""))
    header = data.get("header_image", "")
    link = f"https://store.steampowered.com/app/{appid}/"
    release = (data.get("release_date") or {}).get("date", "—")
    genres = ", ".join([g.get("description") for g in (data.get("genres") or [])][:5]) or "—"
    is_free = data.get("is_free", False)
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    desc = summ.get("review_score_desc")
    total = summ.get("total_reviews")
    review_line = f"Reviews: **{desc}** ({total:,} total)" if (desc and total) else (f"Reviews: **{desc}**" if desc else "Reviews: —")

    body = textwrap.dedent(f"""\
    Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}
    Date: {now_local.strftime('%Y-%m-%d %H:%M')}
    Category: Games
    Tags: auto, steam
    Slug: game-{slug_ts}

    ![{name}]({header})

    **[{name}]({link})**

    {short}

    - {review_line}
    - Release: **{release}**
    - Genres: **{genres}**
    - Price: **{price_str}**
    - Steam AppID: `{appid}`

    *Auto-generated; game chosen randomly each run.*
    """)

    fname.write_text(body, encoding="utf-8")
    print(f"[ok] wrote {fname} for {appid} — {name!r}")

if __name__ == "__main__":
    main()
