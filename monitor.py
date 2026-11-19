#!/usr/bin/env python3
"""Claude referral (guest pass) monitor.

Polls public sources for fresh https://claude.ai/referral/<code> links, filters out
fakes / spam / stale posts, de-duplicates, and sends alerts to Telegram.

Usage:
    python monitor.py --check              # poll every enabled source once, print results, send nothing
    python monitor.py --test-telegram      # send a test message
    python monitor.py                      # single pass
    python monitor.py --duration 240 --interval 60   # loop for 4 min, poll each minute (GitHub Actions)
    python monitor.py --daemon             # run forever (home PC / VPS / Termux)

All configuration is via environment variables, see .env.example.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import requests

log = logging.getLogger("refmon")

UA = "Mozilla/5.0 (compatible; claude-referral-monitor/1.0; personal use)"

# --------------------------------------------------------------------------- #
# Extraction + plausibility
# --------------------------------------------------------------------------- #

# The host must be exactly claude.ai (optionally www.). Look-alikes such as
# claude.ai.evil.com, claude-ai.com, evilclaude.ai, claude.ai@evil.com never match.
REFERRAL_RE = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.)?claude\.ai/referral/([A-Za-z0-9_-]{6,64})(?![A-Za-z0-9_-]*\.[a-z])",
    re.I,
)
# A code followed by ".something" (e.g. claude.ai/referral/abc.evil.com) is not a code.
# The negative lookahead above handles that; sentence-ending dots ("...XyZ.") are fine
# because the lookahead requires a letter after the dot.

TAG_RE = re.compile(r"<[^>]+>")
SPAM_RE = re.compile(
    r"\b(sell(ing)?|sold|buy(ing)?|for sale|price|paid|payment|paypal|cash ?app|usdt|crypto|"
    r"whatsapp|bitcoin|naira|escrow|cheap(er)?|discount code)\b|[$€£₦]\s?\d",
    re.I,
)
CONTEXT_RE = re.compile(
    r"guest pass|free (week|trial|pro)|7[- ]day|claude pro|referral|invite|pass(es)?\b|claude code",
    re.I,
)
PLACEHOLDER_RE = re.compile(r"(your|example|placeholder|xxxx|yyyy|code|link|here|test|demo|sample)", re.I)


def clean_html(s: str | None) -> str:
    return html.unescape(TAG_RE.sub(" ", s or ""))


def extract_codes(*texts: str) -> list[str]:
    """Return unique referral codes (order preserved) found in the given strings."""
    seen: list[str] = []
    for t in texts:
        if not t:
            continue
        t = unquote(html.unescape(t))
        for m in REFERRAL_RE.finditer(t):
            code = m.group(1)
            if code not in seen:
                seen.append(code)
    return seen


def is_plausible(code: str) -> bool:
    """Heuristic: real codes look random (e.g. wsKK8ipOeA, Me-pKK-zcA, qX2Lm5nF-w).

    Rejects placeholders ("YOUR-CODE", "xxxxxxxxxx", "example"), plain words, and
    single-class strings. Heuristic only -- it cannot prove a code is real.
    """
    if not 8 <= len(code) <= 24:
        return False
    if PLACEHOLDER_RE.search(code):
        return False
    if re.search(r"(.)\1{4,}", code):  # aaaaa, 00000
        return False
    classes = sum(
        bool(re.search(p, code)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")
    )
    return classes >= 2


def parse_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Config / data
# --------------------------------------------------------------------------- #

def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in (env(name, default) or "").split(",") if x.strip()]


@dataclass
class Cfg:
    tg_token: str | None = field(default_factory=lambda: env("TELEGRAM_BOT_TOKEN"))
    tg_chat: str | None = field(default_factory=lambda: env("TELEGRAM_CHAT_ID"))
    dry_run: bool = field(default_factory=lambda: env("DRY_RUN", "0") == "1")
    state_path: str = field(default_factory=lambda: env("STATE_PATH", "state/seen.json"))
    max_age_h: float = field(default_factory=lambda: float(env("MAX_AGE_HOURS", "48")))
    max_codes_per_author: int = field(default_factory=lambda: int(env("MAX_CODES_PER_AUTHOR", "3")))
    min_score: int = field(default_factory=lambda: int(env("MIN_SCORE", "2")))
    validate: str = field(default_factory=lambda: env("VALIDATE", "label"))  # label | drop | off
    blocked_authors: list[str] = field(default_factory=lambda: [a.lower() for a in env_list("BLOCKED_AUTHORS")])
    bsky_handle: str | None = field(default_factory=lambda: env("BSKY_HANDLE"))
    bsky_password: str | None = field(default_factory=lambda: env("BSKY_APP_PASSWORD"))
    reddit_subs: str = field(default_factory=lambda: env("REDDIT_SUBS", "ClaudeAI+Anthropic+ClaudeCode"))
    reddit_ua: str = field(default_factory=lambda: env("REDDIT_USER_AGENT", "script:claude-referral-monitor:v1.0 (personal use)"))
    reddit_id: str | None = field(default_factory=lambda: env("REDDIT_CLIENT_ID"))
    reddit_secret: str | None = field(default_factory=lambda: env("REDDIT_CLIENT_SECRET"))
    mastodon_instances: list[str] = field(default_factory=lambda: env_list("MASTODON_INSTANCES", "mastodon.social"))
    mastodon_tags: list[str] = field(default_factory=lambda: env_list("MASTODON_TAGS", "claudeai,claudecode,anthropic,guestpass"))
    x_bearer: str | None = field(default_factory=lambda: env("X_BEARER_TOKEN"))
    x_query: str = field(default_factory=lambda: env("X_QUERY", 'url:"claude.ai/referral" -is:retweet'))
    x_max_reads_per_day: int = field(default_factory=lambda: int(env("X_MAX_READS_PER_DAY", "100")))
    twitterapi_key: str | None = field(default_factory=lambda: env("TWITTERAPI_IO_KEY"))
    threads_token: str | None = field(default_factory=lambda: env("THREADS_ACCESS_TOKEN"))
    exa_key: str | None = field(default_factory=lambda: env("EXA_API_KEY"))
    exa_type: str = field(default_factory=lambda: env("EXA_TYPE", "lite"))
    extra_rss: list[str] = field(default_factory=lambda: env_list("EXTRA_RSS_URLS"))
    cache: dict = field(default_factory=dict)


@dataclass
class Post:
    source: str
    pid: str
    url: str
    author: str
    text: str            # human readable, used for spam / context scoring
    blob: str            # raw text incl. hrefs / JSON, used for link extraction
    created: datetime | None


# --------------------------------------------------------------------------- #
# Sources. Each returns list[Post]; raise on failure.
# --------------------------------------------------------------------------- #

def _get(s: requests.Session, url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", 20)
    r = s.get(url, **kw)
    r.raise_for_status()
    return r


def fetch_bluesky(s: requests.Session, cfg: Cfg) -> list[Post]:
    """Bluesky: search now requires a (free) logged-in session. Use an APP PASSWORD."""
    c = cfg.cache.setdefault("bsky", {})
    if not c.get("jwt") or time.time() - c.get("t", 0) > 90 * 60:
        r = s.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": cfg.bsky_handle, "password": cfg.bsky_password},
            timeout=20,
        )
        r.raise_for_status()
        c.update(jwt=r.json()["accessJwt"], t=time.time())
    hdr = {"Authorization": f"Bearer {c['jwt']}"}
    out: list[Post] = []
    queries = [
        {"q": "claude.ai/referral", "sort": "latest", "limit": 50},
        {"q": "guest pass", "domain": "claude.ai", "sort": "latest", "limit": 50},
    ]
    for params in queries:
        r = s.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts", params=params, headers=hdr, timeout=20)
        if r.status_code == 401:
            c.clear()
            r.raise_for_status()
        r.raise_for_status()
        for p in r.json().get("posts", []):
            rec = p.get("record", {})
            handle = p.get("author", {}).get("handle", "")
            rkey = p.get("uri", "").rsplit("/", 1)[-1]
            out.append(Post(
                "bluesky", p.get("uri", ""), f"https://bsky.app/profile/{handle}/post/{rkey}", handle,
                rec.get("text", ""), json.dumps(p), parse_dt(rec.get("createdAt") or p.get("indexedAt")),
            ))
    return out


def _reddit_token(s: requests.Session, cfg: Cfg) -> str:
    c = cfg.cache.setdefault("reddit", {})
    if c.get("tok") and time.time() - c.get("t", 0) < 3000:
        return c["tok"]
    r = s.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cfg.reddit_id, cfg.reddit_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": cfg.reddit_ua},
        timeout=20,
    )
    r.raise_for_status()
    c.update(tok=r.json()["access_token"], t=time.time())
    return c["tok"]


def _reddit_get(s, cfg, path: str, params: dict) -> requests.Response:
    params = {**params, "raw_json": 1}
    if cfg.reddit_id and cfg.reddit_secret:
        hdr = {"Authorization": f"bearer {_reddit_token(s, cfg)}", "User-Agent": cfg.reddit_ua}
        return _get(s, "https://oauth.reddit.com" + path, params=params, headers=hdr)
    return _get(s, "https://www.reddit.com" + path + ".json", params=params, headers={"User-Agent": cfg.reddit_ua})


def fetch_reddit(s: requests.Session, cfg: Cfg) -> list[Post]:
    """Newest comments + posts in the watched subs, plus a site-wide search.

    Comments matter most: people drop passes in the comments of 'guest pass' threads.
    NOTE: Reddit often blocks datacenter IPs (GitHub Actions) on the unauthenticated
    endpoints. Run `--check` to see whether it works from where you host this.
    """
    out: list[Post] = []
    jobs = [
        (f"/r/{cfg.reddit_subs}/comments", {"limit": 100}),
        (f"/r/{cfg.reddit_subs}/new", {"limit": 100}),
        ("/search", {"q": '"claude.ai/referral"', "sort": "new", "t": "day", "limit": 50}),
    ]
    for path, params in jobs:
        data = _reddit_get(s, cfg, path, params).json()
        for ch in data.get("data", {}).get("children", []):
            d, kind = ch.get("data", {}), ch.get("kind")
            author = d.get("author", "")
            if author in ("AutoModerator", "[deleted]"):
                continue
            if kind == "t1":
                text = d.get("body", "")
            else:
                text = f"{d.get('title', '')}\n{d.get('selftext', '')}"
            blob = f"{text} {d.get('url_overridden_by_dest', '')} {d.get('url', '')}"
            out.append(Post(
                "reddit", d.get("name", ""), "https://www.reddit.com" + d.get("permalink", ""),
                author, text, blob, parse_dt(d.get("created_utc")),
            ))
    return out


def fetch_hn(s: requests.Session, cfg: Cfg) -> list[Post]:
    out: list[Post] = []
    for q in ('"claude.ai/referral"', "claude guest pass"):
        r = _get(s, "https://hn.algolia.com/api/v1/search_by_date",
                 params={"query": q, "tags": "(story,comment)", "hitsPerPage": 50})
        for h in r.json().get("hits", []):
            raw = " ".join(str(h.get(k) or "") for k in ("comment_text", "story_text", "title", "url"))
            oid = h.get("objectID", "")
            out.append(Post(
                "hn", oid, f"https://news.ycombinator.com/item?id={oid}", h.get("author", ""),
                clean_html(raw), html.unescape(raw), parse_dt(h.get("created_at_i") or h.get("created_at")),
            ))
    return out


def fetch_mastodon(s: requests.Session, cfg: Cfg) -> list[Post]:
    out: list[Post] = []
    for inst in cfg.mastodon_instances:
        for tag in cfg.mastodon_tags:
            r = _get(s, f"https://{inst}/api/v1/timelines/tag/{tag}", params={"limit": 40})
            for st in r.json():
                content = st.get("content", "")
                out.append(Post(
                    "mastodon", f"{inst}:{st.get('id')}", st.get("url", ""),
                    (st.get("account") or {}).get("acct", ""), clean_html(content), html.unescape(content),
                    parse_dt(st.get("created_at")),
                ))
    return out


def fetch_x_official(s: requests.Session, cfg: Cfg, state: dict) -> list[Post]:
    """Official X API v2 recent search (pay-per-use, no free tier as of Feb 2026).

    Cost control: we only ask for tweets newer than the last one we saw (since_id), so
    you are billed per *new* matching post, and a daily read cap is enforced.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    xr = state.setdefault("xreads", {"date": today, "n": 0})
    if xr.get("date") != today:
        xr.update(date=today, n=0)
    if xr["n"] >= cfg.x_max_reads_per_day:
        log.info("X daily read cap reached (%s)", cfg.x_max_reads_per_day)
        return []
    params = {
        "query": cfg.x_query, "max_results": 10,
        "tweet.fields": "created_at,entities,author_id", "expansions": "author_id", "user.fields": "username",
    }
    since_id = state.setdefault("cursors", {}).get("x")
    if since_id:
        params["since_id"] = since_id
    else:
        params["start_time"] = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = _get(s, "https://api.x.com/2/tweets/search/recent", params=params,
             headers={"Authorization": f"Bearer {cfg.x_bearer}"})
    j = r.json()
    users = {u["id"]: u["username"] for u in j.get("includes", {}).get("users", [])}
    out: list[Post] = []
    for t in j.get("data", []) or []:
        uname = users.get(t.get("author_id"), "")
        out.append(Post(
            "x", t["id"], f"https://x.com/{uname or 'i'}/status/{t['id']}", uname,
            t.get("text", ""), json.dumps(t), parse_dt(t.get("created_at")),
        ))
    n = len(j.get("data", []) or [])
    if n:
        xr["n"] += n
        state["cursors"]["x"] = j.get("meta", {}).get("newest_id") or max(t["id"] for t in j["data"])
    return out


def fetch_x_twitterapi_io(s: requests.Session, cfg: Cfg, state: dict) -> list[Post]:
    """EXPERIMENTAL third-party X search (twitterapi.io). Parsing is defensive: we regex the
    whole tweet JSON, so schema differences don't break link extraction."""
    r = _get(s, "https://api.twitterapi.io/twitter/tweet/advanced_search",
             params={"query": "claude.ai/referral", "queryType": "Latest"},
             headers={"X-API-Key": cfg.twitterapi_key})
    out: list[Post] = []
    for t in r.json().get("tweets", []) or []:
        author = (t.get("author") or {}).get("userName", "")
        out.append(Post(
            "x", str(t.get("id")), t.get("url") or f"https://x.com/{author}/status/{t.get('id')}", author,
            t.get("text", ""), json.dumps(t), parse_dt(t.get("createdAt")),
        ))
    return out


def fetch_threads(s: requests.Session, cfg: Cfg) -> list[Post]:
    """Official Threads keyword search (free). Without Meta's approval for the
    `threads_keyword_search` permission it only searches the token owner's own posts."""
    out: list[Post] = []
    for q in ("claude.ai/referral", "claude guest pass"):
        r = _get(s, "https://graph.threads.net/v1.0/keyword_search", params={
            "q": q, "search_type": "RECENT", "limit": 50,
            "fields": "id,text,permalink,timestamp,username", "access_token": cfg.threads_token,
        })
        for t in r.json().get("data", []):
            out.append(Post("threads", t.get("id", ""), t.get("permalink", ""), t.get("username", ""),
                            t.get("text", ""), json.dumps(t), parse_dt(t.get("timestamp"))))
    return out


def fetch_exa(s: requests.Session, cfg: Cfg) -> list[Post]:
    """Optional web-wide semantic search (Exa). Free tier: $10 credit/month. Page-text
    retrieval may be billed on top of the search price -- check your dashboard on day 1."""
    start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = s.post("https://api.exa.ai/search", headers={"x-api-key": cfg.exa_key}, timeout=30, json={
        "query": "claude.ai/referral Claude guest pass free week link", "type": cfg.exa_type,
        "numResults": 10, "startPublishedDate": start, "contents": {"text": {"maxCharacters": 3000}},
    })
    r.raise_for_status()
    out: list[Post] = []
    for x in r.json().get("results", []):
        host = urlparse(x.get("url", "")).hostname or ""
        out.append(Post("web", x.get("id") or x.get("url", ""), x.get("url", ""), host,
                        x.get("text", "") or x.get("title", ""), json.dumps(x), parse_dt(x.get("publishedDate"))))
    return out


def fetch_rss(s: requests.Session, cfg: Cfg) -> list[Post]:
    """Any RSS/Atom feed, e.g. a Google Alert for "claude.ai/referral" delivered as RSS."""
    out: list[Post] = []
    for url in cfg.extra_rss:
        r = _get(s, url, headers={"User-Agent": UA})
        root = ET.fromstring(r.content)
        for el in list(root.iter("{http://www.w3.org/2005/Atom}entry")) + list(root.iter("item")):
            raw = ET.tostring(el, encoding="unicode")
            link = ""
            for ch in el:
                tag = ch.tag.split("}")[-1]
                if tag == "link":
                    link = ch.attrib.get("href") or (ch.text or "")
            when = next((ch.text for ch in el if ch.tag.split("}")[-1] in ("published", "updated", "pubDate")), None)
            out.append(Post("rss", link, link, urlparse(url).hostname or "rss", clean_html(raw), raw, parse_dt(when)))
    return out


# name -> (enabled(cfg) , fn, min seconds between polls)
SOURCES = {
    "bluesky":  (lambda c: bool(c.bsky_handle and c.bsky_password), fetch_bluesky, 60),
    "reddit":   (lambda c: True, fetch_reddit, 60),
    "hn":       (lambda c: True, fetch_hn, 120),
    "mastodon": (lambda c: True, fetch_mastodon, 120),
    "x":        (lambda c: bool(c.x_bearer), fetch_x_official, 120),
    "x_3p":     (lambda c: bool(c.twitterapi_key and not c.x_bearer), fetch_x_twitterapi_io, 180),
    "threads":  (lambda c: bool(c.threads_token), fetch_threads, 120),
    "exa":      (lambda c: bool(c.exa_key), fetch_exa, 1500),   # ~58 searches/day => fits the free $10/mo
    "rss":      (lambda c: bool(c.extra_rss), fetch_rss, 300),
}
NEEDS_STATE = {"x", "x_3p"}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k in ("seen", "authors", "cursors", "health"):
        st.setdefault(k, {})
    return st


def save_state(path: str, st: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def prune(state: dict, now: datetime) -> None:
    cutoff = now - timedelta(days=14)
    state["seen"] = {c: v for c, v in state["seen"].items() if (parse_dt(v.get("t")) or now) > cutoff}
    day = now - timedelta(hours=24)
    for k in list(state["authors"]):
        state["authors"][k] = [e for e in state["authors"][k] if (parse_dt(e[1]) or now) > day]
        if not state["authors"][k]:
            del state["authors"][k]


# --------------------------------------------------------------------------- #
# Scoring / validation / Telegram
# --------------------------------------------------------------------------- #

def score_post(post: Post, code: str, now: datetime) -> tuple[int, list[str]]:
    s, why = 0, []
    if post.created:
        age_h = (now - post.created).total_seconds() / 3600
        if age_h < 1: s += 3; why.append("<1h old")
        elif age_h < 6: s += 2; why.append("<6h old")
        elif age_h < 24: s += 1; why.append("<24h old")
    if CONTEXT_RE.search(post.text):
        s += 2; why.append("guest-pass context")
    if len(code) == 10:
        s += 1; why.append("10-char code")
    if SPAM_RE.search(post.text):
        s -= 4; why.append("selling/spam words")
    return s, why


def check_link(s: requests.Session, code: str) -> str:
    """Best-effort. Returns 'ok' | 'dead' | 'unknown'. claude.ai may bot-block us, and a
    200 does not prove the pass is unused -- treat as a hint only."""
    try:
        r = s.get(f"https://claude.ai/referral/{code}", allow_redirects=False, timeout=8,
                  headers={"User-Agent": UA})
    except requests.RequestException:
        return "unknown"
    if r.status_code in (404, 410):
        return "dead"
    if r.status_code in (200, 301, 302, 303, 307, 308):
        return "ok"
    return "unknown"


def age_str(created: datetime | None, now: datetime) -> str:
    if not created:
        return "unknown"
    m = int((now - created).total_seconds() // 60)
    return f"{m} min ago" if m < 90 else f"{m // 60} h ago"


SRC_LABEL = {"x": "X", "x_3p": "X", "bluesky": "Bluesky", "reddit": "Reddit", "hn": "Hacker News",
             "mastodon": "Mastodon", "threads": "Threads", "web": "Web", "rss": "RSS"}


def build_message(post: Post, code: str, now: datetime, link_state: str, score: int) -> str:
    esc = html.escape
    chk = {"ok": "✅ page loads (not proof it's unused)", "dead": "❌ looks dead (404)",
           "unknown": "⚠️ couldn't confirm automatically. Open it and try", "off": "— not checked"}[link_state]
    conf = "High" if score >= 5 else "Medium" if score >= 3 else "Low"
    return (
        "🚨 <b>NEW CLAUDE REFERRAL</b>\n"
        f"🔗 https://claude.ai/referral/{esc(code)}\n"
        f"📍 {esc(SRC_LABEL.get(post.source, post.source))} · @{esc(post.author or '?')}\n"
        f"⚡ Posted: {esc(age_str(post.created, now))}\n"
        f"🧪 Link check: {chk}\n"
        f"🎯 Confidence: {conf} ({score})\n"
        "Open quickly: passes are single-use and get claimed fast.\n"
        f"Original post: {esc(post.url)}"
    )


def tg_send(cfg: Cfg, text: str, button_url: str | None = None) -> bool:
    if cfg.dry_run or not (cfg.tg_token and cfg.tg_chat):
        print("--- [DRY RUN / no Telegram config] ---\n" + text + "\n")
        return True
    payload = {"chat_id": cfg.tg_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if button_url:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "🚀 Open referral now", "url": button_url}]]})
    try:
        r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage", data=payload, timeout=20)
        if r.status_code != 200:
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", type(e).__name__)  # never log the URL (contains the token)
        return False


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #

def process_posts(posts: list[Post], cfg: Cfg, state: dict, s: requests.Session, now: datetime) -> int:
    """Filter -> dedupe -> score -> alert. Returns number of alerts sent."""
    seen = state["seen"]

    def mark(code: str, st: str, src: str) -> None:
        seen[code] = {"t": now.isoformat(timespec="seconds"), "s": src, "st": st}

    cands: dict[str, Post] = {}
    for p in sorted(posts, key=lambda x: x.created or now):
        if p.author and p.author.lower() in cfg.blocked_authors:
            continue
        if p.created and (now - p.created) > timedelta(hours=cfg.max_age_h):
            continue
        if p.created and p.created > now + timedelta(minutes=10):
            continue  # clock-skewed / fabricated timestamp
        for code in extract_codes(p.blob, p.text):
            if code in seen or code in cands:
                continue
            if not is_plausible(code):
                mark(code, "implausible", p.source)
                continue
            cands[code] = p

    # Spam-farm guard: a real referrer has at most 3 passes. One author posting more than
    # that many distinct codes in 24h is a list / aggregator / farm, so suppress all of them.
    per_author: dict[str, set[str]] = defaultdict(set)
    for code, p in cands.items():
        if p.author:
            per_author[f"{p.source}:{p.author.lower()}"].add(code)
    suppressed: set[str] = set()
    for key, codes in per_author.items():
        prior = {e[0] for e in state["authors"].get(key, [])}
        if len(prior | codes) > cfg.max_codes_per_author:
            suppressed |= codes

    sent = 0
    for code, p in cands.items():
        if code in suppressed:
            mark(code, "suppressed:bulk-poster", p.source)
            continue
        if SPAM_RE.search(p.text):  # someone selling a pass is not giving one away
            mark(code, "dropped:spam", p.source)
            continue
        score, _ = score_post(p, code, now)
        if score < cfg.min_score:
            mark(code, f"dropped:score{score}", p.source)
            continue
        link_state = "off" if cfg.validate == "off" else check_link(s, code)
        if link_state == "dead" and cfg.validate == "drop":
            mark(code, "dropped:dead", p.source)
            continue
        url = f"https://claude.ai/referral/{code}"  # rebuilt on the exact host, never the raw URL
        if tg_send(cfg, build_message(p, code, now, link_state, score), url):
            mark(code, "alerted", p.source)
            if p.author:
                key = f"{p.source}:{p.author.lower()}"
                state["authors"].setdefault(key, []).append([code, now.isoformat(timespec="seconds")])
            sent += 1
    return sent


def cycle(cfg: Cfg, state: dict, s: requests.Session, last_poll: dict, now: datetime | None = None,
          only: list[str] | None = None, verbose: bool = False) -> int:
    now = now or datetime.now(timezone.utc)
    ts = time.time()
    posts: list[Post] = []
    for name, (enabled, fn, every) in SOURCES.items():
        if only and name not in only:
            continue
        if not enabled(cfg):
            continue
        if not verbose:
            if name == "exa":
                bucket = int(ts // every)
                if ts % every > 330 or last_poll.get(name) == bucket:
                    continue
                last_poll[name] = bucket
            elif ts - last_poll.get(name, 0) < every - 1:
                continue
            else:
                last_poll[name] = ts
        h = state["health"].setdefault(name, {"fails": 0, "alerted": False})
        try:
            got = fn(s, cfg, state) if name in NEEDS_STATE else fn(s, cfg)
            posts += got
            if verbose:
                hits = sum(1 for p in got if extract_codes(p.blob, p.text))
                print(f"[{name}] OK: {len(got)} posts fetched, {hits} contain a referral-looking link")
            if h["fails"]:
                h.update(fails=0, alerted=False)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("source %s failed: %s", name, msg)
            if verbose:
                print(f"[{name}] FAILED: {msg}")
            h["fails"] = min(h["fails"] + 1, 6)
            if h["fails"] >= 6 and not h["alerted"]:
                h["alerted"] = True
                tg_send(cfg, f"⚠️ Referral monitor: source <b>{html.escape(name)}</b> keeps failing "
                             f"({html.escape(msg)}). Other sources are still running.")
    if verbose:
        return 0
    return process_posts(posts, cfg, state, s, now)


def heartbeat(cfg: Cfg, state: dict, now: datetime) -> None:
    """Weekly 'still alive' ping. It also changes the state file, which makes the workflow
    commit, which stops GitHub disabling the schedule after 60 days of repo inactivity."""
    last = parse_dt(state.get("hb"))
    if last and now - last < timedelta(days=7):
        return
    active = [n for n, (en, _, _) in SOURCES.items() if en(cfg)]
    bad = [n for n, h in state["health"].items() if h.get("fails", 0) >= 6]
    alerts = sum(1 for v in state["seen"].values() if v.get("st") == "alerted")
    if last is not None:  # skip the very first run
        tg_send(cfg, "🟢 Referral monitor is alive.\nSources: " + ", ".join(active) +
                (f"\n⚠️ Failing: {', '.join(bad)}" if bad else "") + f"\nAlerts in last 14 days: {alerts}")
    state["hb"] = now.isoformat(timespec="seconds")


def run(cfg: Cfg, duration: int, interval: int) -> None:
    state = load_state(cfg.state_path)
    before = json.dumps(state, sort_keys=True)
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    last_poll: dict = {}
    end = time.time() + duration
    while True:
        now = datetime.now(timezone.utc)
        prune(state, now)
        heartbeat(cfg, state, now)
        n = cycle(cfg, state, s, last_poll, now)
        if n:
            log.info("sent %d alert(s)", n)
        if json.dumps(state, sort_keys=True) != before:
            save_state(cfg.state_path, state)
            before = json.dumps(state, sort_keys=True)
        if time.time() + interval > end:
            break
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="poll each enabled source once and print results; sends nothing")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--duration", type=int, default=0, help="keep polling for N seconds (0 = one pass)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polling cycles")
    ap.add_argument("--daemon", action="store_true", help="run forever")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Cfg()

    if a.test_telegram:
        ok = tg_send(cfg, "✅ Referral monitor test message. Telegram is wired up correctly.")
        print("sent" if ok else "FAILED (check token / chat id; did you press Start on the bot?)")
        return 0 if ok else 1
    if a.check:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        print("Enabled sources:", ", ".join(n for n, (en, _, _) in SOURCES.items() if en(cfg)))
        cycle(cfg, load_state(cfg.state_path), s, {}, verbose=True)
        return 0
    run(cfg, 10**9 if a.daemon else a.duration, a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
