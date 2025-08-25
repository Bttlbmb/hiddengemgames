#!/usr/bin/env python3
import requests

def fetch_review_texts(appid: int, num=40):
    """
    Light fetch of recent English reviews. We keep this small for cost/speed.
    """
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1, "language": "english",
        "purchase_type": "all", "filter": "recent",
        "num_per_page": min(max(num, 5), 100)
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json() or {}
    out = []
    for rv in (data.get("reviews") or []):
        txt = (rv.get("review") or "").strip()
        if txt:
            out.append(txt)
    return out
