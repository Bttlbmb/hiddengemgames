#!/usr/bin/env python3
import os, datetime, pathlib

TODAY = datetime.date.today().isoformat()
content_dir = pathlib.Path("content/posts")
content_dir.mkdir(parents=True, exist_ok=True)

md_path = content_dir / f"{TODAY}-daily.md"
if md_path.exists():
    print("Post already exists for today, skipping.")
else:
    md = f"""Title: Today’s Hidden Gems (MVP)
Date: {TODAY}
Category: Daily

This is the minimal MVP post generated automatically.

- **Game Gem (placeholder):** Try something small and cozy you’ve never heard of.
- **Book Gem (placeholder):** Pick a short backlist novel that’s < 300 pages.
"""
    md_path.write_text(md, encoding="utf-8")
    print(f"Wrote {md_path}")
