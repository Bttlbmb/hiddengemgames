#!/usr/bin/env python3
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
import textwrap

# --- settings ---
LOCAL_TZ = ZoneInfo("Europe/Berlin")   # change if you prefer a different display timezone
POST_DIR = Path("content/posts")
POST_DIR.mkdir(parents=True, exist_ok=True)

# --- time stamps ---
now_utc = dt.datetime.now(dt.timezone.utc)
now_local = now_utc.astimezone(LOCAL_TZ)

# Filename unique per hour (prevents duplicates on reruns)
slug_ts = now_local.strftime("%Y-%m-%d-%H00")
fname = POST_DIR / f"{slug_ts}-hourly.md"

if fname.exists():
    print(f"[info] Post already exists for this hour: {fname}")
else:
    # Pelican parses "Date:"; include offset so it displays correctly
    date_meta = now_local.isoformat(timespec="minutes")
    human_time = now_local.strftime("%Y-%m-%d %H:%M %Z")

    body = textwrap.dedent(f"""\
    Title: Hourly Update — {human_time}
    Date: {date_meta}
    Category: Updates
    Tags: hourly, auto
    Slug: {slug_ts}

    This is an automated hourly post generated at **{human_time}**.
    Replace this text later with your gems picker output.
    """)

    fname.write_text(body, encoding="utf-8")
    print(f"[ok] Wrote {fname}")
