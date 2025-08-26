import re
from collections import Counter
from typing import List, Optional
import math
import requests
from . import config as C
from . import storage as S

# -------- small text utils
def _clean(s: str, max_len=650) -> str:
    import re as _re
    s = _re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len].rstrip()

MARKETING_PAT = re.compile(
    r"\b(amazing|incredible|awesome|ultimate|epic|jaw[- ]?dropping|must[- ]?play|"
    r"groundbreaking|revolutionary|stunning|breathtaking)\b", re.I
)
def _demarket(s: str) -> str:
    s = re.sub(MARKETING_PAT, "", s or "")
    s = re.sub(r"[!]{2,}", "!", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -–—")

# -------- local extractive fallback
_STOP = {"the","a","an","and","or","but","if","then","so","to","of","in","on","at","for","from","with","by","as",
         "is","are","was","were","be","been","being","it","its","this","that","these","those","you","your","i","we",
         "they","he","she","him","her","them","our","us","me","my","mine","yours","their","theirs","his","hers",
         "not","no","yes","very","really","just","also","too","still","again","more","most","much","many","few",
         "can","could","should","would","will","wont","won't","cant","can't","dont","don't","did","didn't","does",
         "doesn't","do","have","has","had","having","make","made","get","got","like","lot","lots","thing","things",
         "game","games","play","played","playing","player","players","steam","time","times","one","two","three","bit","little"}
_POS = {"enjoy","love","fun","great","excellent","polished","smooth","addictive","awesome","amazing","satisfying",
        "beautiful","charming","relaxing","clever","smart","unique","solid","well-made","well designed","responsive","tight"}
_FEAT = {"puzzle","story","narrative","soundtrack","music","art","graphics","pixel","combat","mechanics","controls",
         "co-op","coop","multiplayer","exploration","level design","progression","boss","mode","roguelike","deck",
         "cards","strategy","platformer","metroidvania","physics","builder","craft","quest","dialogue","voice acting"}

def _sentences(text: str):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if 6 <= len(p.strip()) <= 220]

def _tokens(text: str):
    import re as _re
    return [t for t in _re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 2]

def _score_sentences(sentences):
    docs = [set(_tokens(s)) for s in sentences]
    tf = Counter([tok for s in sentences for tok in _tokens(s)])
    df = Counter([tok for d in docs for tok in d])
    N = max(1, len(sentences))
    idf = {t: math.log((N + 1) / (1 + df[t])) + 1.0 for t in df}
    scores = []
    for s in sentences:
        toks = _tokens(s)
        score = sum(tf[t] * idf.get(t, 0.0) for t in toks)
        low = s.lower()
        if any(w in low for w in _POS):  score *= 1.15
        if any(w in low for w in _FEAT): score *= 1.10
        scores.append(score)
    return scores

def _reviews_to_paragraph(reviews: List[str], want=3) -> Optional[str]:
    sents = []
    for rv in reviews:
        sents.extend(_sentences(rv))
    if not sents: return None
    scores = _score_sentences(sents)
    ranked = [s for _, s in sorted(zip(scores, sents), key=lambda x: x[0], reverse=True)]
    band = set(ranked[: max(5, want * 2)])
    top = []
    for s in sents:
        if s in band and s not in top:
            top.append(s)
        if len(top) >= want: break
    return _clean(" ".join(top[:want]), 650)

# -------- Cloudflare call
def _cf_generate(prompt: str, max_tokens=360) -> str:
    if not (C.CF_ACCOUNT_ID and C.CF_API_TOKEN and prompt.strip()):
        return ""
    url = f"https://api.cloudflare.com/client/v4/accounts/{C.CF_ACCOUNT_ID}/ai/run/{C.CF_MODEL}"
    headers = {"Authorization": f"Bearer {C.CF_API_TOKEN}"}
    payload = {
        "messages": [
            {"role": "system", "content": "Return only the requested text. No JSON, no markdown, no preface."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code in (429, 503): return ""
        r.raise_for_status()
        j = r.json()
        return (j.get("result") or {}).get("response", "") or ""
    except Exception:
        return ""

# -------- public API (with caching)
def make_overview(appid: int, name: str, short_desc: str, reviews: List[str]) -> str:
    kind = "overview"
    cached = S.read_summary(appid, kind)
    if cached: return cached
    base = _demarket(short_desc or "")
    sample = "\n\n".join(reviews[:15]) if reviews else ""
    txt = ""
    if C.CF_ACCOUNT_ID and C.CF_API_TOKEN and (base or sample):
        prompt = ("Write a neutral, non-marketing overview of this PC game in 2–4 sentences. "
                  "Avoid hype; describe premise, mechanics, and tone succinctly.\n"
                  f"STEAM SHORT DESCRIPTION:\n{base}\n\nREVIEWS SAMPLE:\n{sample}")
        raw = _cf_generate(prompt, max_tokens=420)
        if raw: txt = _clean(raw, 650)
    if not txt:
        parts = []
        sents = _sentences(base)
        if sents: parts.append(" ".join(sents[:2]))
        add = _reviews_to_paragraph(reviews, want=2) if reviews else None
        if add: parts.append(add)
        txt = _clean(" ".join(parts) or "A concise, neutral overview is unavailable for this title.", 650)
    S.write_summary(appid, kind, txt)
    return txt

def make_likes(appid: int, name: str, reviews: List[str]) -> str:
    kind = "likes"
    cached = S.read_summary(appid, kind)
    if cached: return cached
    sample = "\n\n".join(reviews[:20]) if reviews else ""
    txt = ""
    if C.CF_ACCOUNT_ID and C.CF_API_TOKEN and sample:
        prompt = (f"Summarize what is praised about the game {name} in 2–3 sentences. "
                  "Use a direct, factual tone (e.g. 'Combat feels satisfying', 'Levels are well designed'). "
                  "Do not hedge with 'players say' or 'some think'.\n"
                  f"REVIEWS SAMPLE:\n{sample}")
        raw = _cf_generate(prompt, max_tokens=360)
        if raw: txt = _clean(raw, 550)
    if not txt:
        txt = _reviews_to_paragraph(reviews, want=3) or "Praised aspects not available."
    S.write_summary(appid, kind, txt)
    return txt

def make_dislikes(appid: int, name: str, reviews: List[str]) -> str:
    kind = "dislikes"
    cached = S.read_summary(appid, kind)
    if cached: return cached
    sample = "\n\n".join(reviews[:20]) if reviews else ""
    txt = ""
    if C.CF_ACCOUNT_ID and C.CF_API_TOKEN and sample:
        prompt = (f"Summarize what is criticized about the game {name} in 2–3 sentences. "
                  "Use a direct, factual tone (e.g. 'Controls are clunky', 'Performance is unstable'). "
                  "Do not hedge with 'players say' or 'some think'.\n"
                  f"REVIEWS SAMPLE:\n{sample}")
        raw = _cf_generate(prompt, max_tokens=360)
        if raw: txt = _clean(raw, 550)
    if not txt:
        txt = _reviews_to_paragraph(reviews, want=3) or "Criticized aspects not available."
    S.write_summary(appid, kind, txt)
    return txt

def make_hidden_gem(appid: int, name: str, reviews: List[str],
                    review_desc: str | None, total_reviews: int | None) -> str:
    kind = "hidden_gem"
    cached = S.read_summary(appid, kind)
    if cached: return cached
    txt = ""
    if C.CF_ACCOUNT_ID and C.CF_API_TOKEN and reviews:
        sig = []
        if total_reviews is not None: sig.append(f"total_reviews={total_reviews}")
        if review_desc: sig.append(f"review_score={review_desc}")
        prompt = (f"Explain in 1–2 sentences why {name} qualifies as a hidden gem. "
                  "Highlight uniqueness, craft, or depth despite limited attention. "
                  "Use a direct, matter-of-fact tone without hype.\n"
                  f"Signals: {', '.join(sig) or 'n/a'}\n"
                  f"REVIEWS SAMPLE:\n" + "\n\n".join(reviews[:20]))
        raw = _cf_generate(prompt, max_tokens=220)
        if raw: txt = _clean(raw, 320)
    if not txt:
        # heuristic
        kw = re.compile(r"\b(hidden gem|underrated|overlooked|sleeper|surprised me|better than it looks)\b", re.I)
        hits = []
        for rv in (reviews or []):
            if kw.search(rv or ""):
                sents = _sentences(rv)
                if sents: hits.append(sents[0])
                if len(hits) >= 2: break
        parts = []
        if total_reviews is not None and review_desc and total_reviews < 500 and "Positive" in review_desc:
            parts.append(f"Strong {review_desc.lower()} reception despite a relatively small player base.")
        if hits:
            parts.append(_clean(" ".join(hits[:1]), 200))
        if not parts and review_desc:
            parts.append(f"Its {review_desc.lower()} reviews suggest quality that hasn’t reached a wide audience.")
        txt = _clean(" ".join(parts) or "Well-regarded qualities with limited visibility make it easy to miss.", 320)
    S.write_summary(appid, kind, txt)
    return txt
