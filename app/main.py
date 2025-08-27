# app/main.py
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

from app import steam, storage

# Optional AI module (Cloudflare / HF / etc.). Safe to be missing.
try:
    from app import ai  # type: ignore
except Exception:  # nosec - optional
    ai = None


# -----------------------------
# Rendering helpers
# -----------------------------

def _mk_review_line(data: dict) -> str:
    """
    Try to form a short review line from what we have in appdetails.
    (Steam doesn't always expose review summary in appdetails; we keep it cheap.)
    """
    # If you later store a review summary in your candidate pool or a separate cache,
    # feel free to use that here instead.
    meta = data.get("metacritic")
    if meta and isinstance(meta, dict) and "score" in meta:
        return f"Reviews: **Metacritic {meta.get('score')}**"
    return "Reviews: —"


def _write_post_from_appdetails(appid: int, data: dict, *, now_utc: dt.datetime | None = None) -> None:
    """
    Render a Pelican post from a single appdetails payload.
    Uses optional AI summaries if app/ai.py is available; otherwise, falls back.
    """
    # --- timestamps & slugs
    LOCAL_TZ = getattr(storage, "LOCAL_TZ", ZoneInfo("Europe/Berlin"))
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")

    # --- core fields
    name = data.get("name", f"App {appid}")
    short = (data.get("short_description") or "").strip()
    header = data.get("header_image") or ""
    link = f"https://store.steampowered.com/app/{appid}/"

    rd = data.get("release_date") or {}
    release = rd.get("date", "—")

    genres = ", ".join([g.get("description") for g in (data.get("genres") or [])]) or "—"

    is_free = bool(data.get("is_free", False))
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    review_line = _mk_review_line(data)

    # --- optional AI bits
    overview_text = ""
    gem_reason = ""
    likes_text = ""
    dislikes_text = ""

    if ai:
        try:
            overview_text = ai.summarize_overview(data) or ""
        except Exception:
            overview_text = ""
        try:
            gem_reason = ai.summarize_gem_reason(data) or ""
        except Exception:
            gem_reason = ""
        try:
            likes_text = ai.summarize_likes(data) or ""
        except Exception:
            likes_text = ""
        try:
            dislikes_text = ai.summarize_dislikes(data) or ""
        except Exception:
            dislikes_text = ""

    # --- fallbacks if AI is off/failed
    if not overview_text:
        overview_text = short or "No overview available."
    if not gem_reason:
        gem_reason = (
            "Overlooked by the mainstream, but it stands out for its mechanics, style, or niche appeal."
        )

    likes_block = f"\n\n### What players like\n\n{likes_text}\n" if likes_text else ""
    dislikes_block = f"\n\n### What players don’t like\n\n{dislikes_text}\n" if dislikes_text else ""

    # --- build markdown
    md = f"""Title: {name}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: game-{slug_ts}
Cover: {header}

# {name}

![{name}]({header})

{overview_text}

- {review_line}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`

### Why it’s a hidden gem

{gem_reason}{likes_block}{dislikes_block}

*Auto-generated; daily pick from a cached candidate pool refreshed weekly.*
"""

    # --- write
    storage.POST_DIR.mkdir(parents=True, exist_ok=True)
    post_path = storage.POST_DIR / f"{slug_ts}-auto.md"
    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")


# -----------------------------
# Commands
# -----------------------------

def run_harvest(
    *,
    min_reviews: int,
    block_nsfw: bool,
    max_apps_to_check: int | None,
    batch_size: int,
    wait_s: float,
) -> None:
    """
    Refresh the cached candidate pool by sampling appids and building a high-signal set.
    This function delegates rate-limiting and request pacing to app.steam.
    """
    print(
        f"[harvest] start | min_reviews={min_reviews} block_nsfw={block_nsfw} "
        f"max_apps_to_check={max_apps_to_check} batch_size={batch_size} wait_s={wait_s}"
    )
    apps = steam.get_applist()
    if not apps:
        raise RuntimeError("Could not fetch the Steam applist.")

    pool = steam.build_candidate_pool(
        apps,
        min_reviews=min_reviews,
        block_nsfw=block_nsfw,
        sample_size=max_apps_to_check,
        batch_size=batch_size,
        wait_s=wait_s,
    )
    storage.save_candidate_pool(pool)
    print(f"[harvest] candidate pool size={len(pool)} saved to {storage.CANDIDATE_POOL_PATH}")


def run_daily() -> None:
    """
    Daily: pick ONE id from the cached pool, fetch details once, write the post.
    Keeps Steam traffic extremely low.
    """
    pool = storage.load_candidate_pool(default={})
    if not pool:
        raise RuntimeError("No candidate pool found. Run with --harvest first.")

    # Avoid short-term repeats
    seen_path = storage.DATA_DIR / "seen_daily.json"
    try:
        seen_json = json.loads(seen_path.read_text(encoding="utf-8"))
        seen = set(seen_json.get("ids", []))
    except Exception:
        seen = set()

    # pick one appid (note: returns INT)
    appid = steam.pick_from_pool(pool, exclude=seen, use_weights=True)

    # fetch details exactly once (retry with one backup pick on failure)
    data = steam.get_appdetails(appid)
    if not data:
        print(f"[daily] first pick failed to fetch details (appid={appid}), trying a backup…")
        appid2 = steam.pick_from_pool(pool, seen=seen | {appid}, use_weights=True)
        data = steam.get_appdetails(appid2)
        if not data:
            raise RuntimeError("Could not fetch appdetails for the picked ids.")
        appid = appid2

    _write_post_from_appdetails(appid, data)

    # update short-term seen window
    try:
        recent = list(seen)
        recent.append(appid)
        recent = recent[-50:]
        seen_path.write_text(json.dumps({"ids": recent}, indent=2), encoding="utf-8")
    except Exception:
        pass


# -----------------------------
# CLI
# -----------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hiddengemgames",
        description="Hidden Gem Games – harvest candidate pool and generate daily post.",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--harvest", action="store_true", help="Refresh the weekly candidate pool.")
    g.add_argument("--daily", action="store_true", help="Generate today’s post from cached pool.")

    parser.add_argument("--min-reviews", type=int, default=80, help="Minimum reviews to consider.")
    parser.add_argument(
        "--allow-nsfw", action="store_true", help="Allow NSFW / 18+ content in the pool."
    )
    parser.add_argument(
        "--max-apps", type=int, default=1500, help="Max number of random apps to check in this run."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="How many appdetails to attempt per batch before pausing.",
    )
    parser.add_argument(
        "--wait-s",
        type=float,
        default=2.0,
        help="Seconds to wait between batches (helps avoid 429s).",
    )

    args = parser.parse_args(argv)

    if args.harvest:
        run_harvest(
            min_reviews=args.min_reviews,
            block_nsfw=not args.allow_nsfw,
            max_apps_to_check=args.max_apps,
            batch_size=args.batch_size,
            wait_s=args.wait_s,
        )
    elif args.daily:
        run_daily()
    else:
        parser.error("Choose either --harvest or --daily")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[aborted]")
        sys.exit(130)
