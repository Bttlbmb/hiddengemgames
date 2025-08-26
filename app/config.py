import os
import time
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

# -------- Paths
ROOT            = Path(".")
CONTENT_DIR     = ROOT / "content"
POST_DIR        = CONTENT_DIR / "posts"
DATA_DIR        = CONTENT_DIR / "data"
SUM_CACHE_DIR   = DATA_DIR / "summaries"
POOL_PATH       = DATA_DIR / "candidate_pool.json"
POOL_META_PATH  = DATA_DIR / "pool_meta.json"
APPSTATS_DIR    = DATA_DIR / "appstats"
SEEN_PATH       = DATA_DIR / "seen.json"
APPLIST_CACHE   = DATA_DIR / "applist.json"

for p in (POST_DIR, DATA_DIR, SUM_CACHE_DIR, APPSTATS_DIR):
    p.mkdir(parents=True, exist_ok=True)

LOCAL_TZ = ZoneInfo("Europe/Berlin")

# -------- Harvest cadence & pool sizing
APPLIST_TTL_SECS  = 60 * 60 * 24 * 7   # 7 days
POOL_TTL_SECS     = 60 * 60 * 24 * 7   # 7 days
POOL_MIN_SIZE     = int(os.getenv("POOL_MIN_SIZE", "80"))
HARVEST_MAX_PROBE = int(os.getenv("HARVEST_MAX_PROBE", "180"))
HARVEST_FORCE     = os.getenv("HARVEST_FORCE") == "1"

# -------- Hidden-gem hard gates
MIN_REVIEWS   = 50
MAX_REVIEWS   = 2000
MIN_POS_RATIO = 0.85  # lifetime positivity if recent unavailable

# -------- Diversity (placeholder hooks)
NO_REPEAT_GENRE_DAYS     = 5
NO_REPEAT_PUBLISHER_DAYS = 14

# -------- Cloudflare Workers AI
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN  = os.getenv("CF_API_TOKEN")
CF_MODEL      = "@cf/meta/llama-3-8b-instruct"

# -------- Steam networking knobs
USER_AGENT     = "HiddenGemGamesBot/1.0 (contact: bot@example.invalid)"
RETRY_BACKOFF  = 0.8
RATE_LIMIT_S   = 0.75

# -------- Offline / no-fetch toggle
def _pelican_flag_default_true() -> bool:
    pelicanconf = Path("pelicanconf.py")
    try:
        if pelicanconf.exists():
            spec = importlib.util.spec_from_file_location("pelicanconf", str(pelicanconf.resolve()))
            mod = importlib.util.module_from_spec(spec)  # type: ignore
            spec.loader.exec_module(mod)  # type: ignore
            return bool(getattr(mod, "HGG_FETCH_ON_BUILD", True))
    except Exception:
        pass
    return True

def should_fetch() -> bool:
    val = os.getenv("HGG_FETCH", "").strip().lower()
    if val in {"0","false","no","off"}: return False
    if val in {"1","true","yes","on"}:  return True
    return _pelican_flag_default_true()

# -------- Time helpers
def now_local():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).astimezone(LOCAL_TZ)

def is_stale(mtime: float, ttl_secs: int) -> bool:
    return (time.time() - mtime) > ttl_secs
