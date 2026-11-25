import base64
import binascii
import json
import os
import random
import re
import sys
import time
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://gamebounty.st"
API = f"{BASE}/api/v1/posts"
LIMIT = 100
OUT_FILE = "games_list.json"
CACHE_FILE = "_probe_cache.json"
CACHE_TTL = 86400
DELAY_LIST = (0.4, 1.0)
DELAY_DETAIL = (0.5, 1.2)
MAX_ATTEMPTS = 4
FORCE = os.environ.get("FORCE") == "1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DEAD_STRONG = ("file not found", "file was not found", "file does not exist",
               "file you are looking for", "file has been deleted",
               "file was deleted", "file has expired", "file expired",
               "file is no longer available", "file is not available",
               "link has expired", "404 not found")
DEAD_WEAK = ("deleted", "has been removed", "was removed", "no longer available",
             "expired", "page not found", "could not be found")
ALIVE_MARKERS = ("download", "filename", "file name", "file size", "click here")
XFS_HOSTS = ("datanodes.to", "fileq.net")
_pw = {"pw": None, "browser": None, "ctx": None}


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, */*",
                      "Referer": BASE + "/", "Accept-Language": "en-US,en;q=0.9"})
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"])))
    return s


def browser_get(url):
    from playwright.sync_api import sync_playwright
    if _pw["browser"] is None:
        _pw["pw"] = sync_playwright().start()
        _pw["browser"] = _pw["pw"].chromium.launch(
            headless=True, args=["--disable-gpu", "--no-sandbox"])
        _pw["ctx"] = _pw["browser"].new_context(
            user_agent=UA, extra_http_headers={"Accept": "application/json"})
    page = _pw["ctx"].new_page()
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if r is None or r.status != 200:
            raise RuntimeError(f"browser HTTP {r.status if r else 'none'}")
        return json.loads(page.evaluate("() => document.body.innerText"))
    finally:
        page.close()


def get_json(s, url):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                print(f"   ⚠️ HTTP {r.status_code}, browser fallback")
                return browser_get(url)
        except Exception as e:
            print(f"   ⚠️ {type(e).__name__}: {e} (attempt {attempt})")
        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt + random.random())
    raise RuntimeError(f"gave up: {url}")


def unwrap(url):
    if "/api/dl/" not in url:
        return url
    b64 = url.split("/api/dl/", 1)[1].split("/", 1)[-1]
    b64 += "=" * (-len(b64) % 4)
    try:
        return base64.urlsafe_b64decode(b64).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return url


def norm_sources(raw):
    s = (raw or "").strip().lower()
    out = []
    if "csf" in s or "clean steam" in s:
        out.append("Gamebounty CSF")
    if "sovereign" in s:
        out.append("Sovereign")
    if not out and s:
        out.append(re.sub(r"\s+", " ", raw.strip()))
    return " + ".join(out), "sovereign" in s


def _pixeldrain(url, host, s):
    if "pixeldrain.com" not in host and "pixeldrain.com" not in url:
        return None
    fid = url.rstrip("/").split("/")[-1].split("?")[0]
    if not fid:
        return None
    try:
        r = s.get(f"https://pixeldrain.com/api/file/{fid}/info", timeout=25)
        if r.status_code == 404:
            return "dead", "pixeldrain 404"
        if r.status_code != 200:
            return None
        j = r.json()
        return ("alive", "pixeldrain ok") if j.get("success") else \
               ("dead", f"pixeldrain '{j.get('value')}'")
    except Exception:
        return None


def _xfs(url, host, s):
    if not any(h in host for h in XFS_HOSTS):
        return None
    code = url.rstrip("/").split("/")[-1].split("?")[0]
    if not code:
        return None
    try:
        r = s.post(f"https://{host}/api/file/info", data={"key": code}, timeout=20)
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("status") == 200:
            return "alive", "file-info ok"
        msg = str(j.get("msg", "")).lower()
        if any(k in msg for k in ("not found", "deleted", "removed")):
            return "dead", f"file-info '{msg}'"
        return None
    except Exception:
        return None


def _html(url, s):
    fn = unquote(urlparse(url).path).split("/")[-1].lower()
    try:
        r = s.get(url, timeout=25, allow_redirects=True,
                  headers={"Range": "bytes=0-60000"})
    except Exception as e:
        return "unknown", f"fetch {type(e).__name__}"
    if r.status_code in (404, 410):
        return "dead", f"http {r.status_code}"
    if r.status_code >= 500:
        return "unknown", f"http {r.status_code}"
    ct = (r.headers.get("content-type") or "").lower()
    if "text/html" not in ct and "text/plain" not in ct:
        return "alive", "binary"
    text = r.content[:80000].decode("utf-8", "replace").lower()
    for m in DEAD_STRONG:
        if m in text:
            return "dead", f"page '{m}'"
    for m in DEAD_WEAK:
        if m in text:
            if fn and len(fn) > 4 and fn in text:
                return "alive", f"filename + '{m}'"
            return "dead", f"page '{m}'"
    if fn and len(fn) > 4 and fn in text:
        return "alive", "filename present"
    for m in ALIVE_MARKERS:
        if m in text:
            return "unknown", f"landing '{m}'"
    return "unknown", "no signal"


def assess(url, host, s, cache):
    hit = cache.get(url)
    if hit and time.time() - hit.get("ts", 0) < CACHE_TTL:
        return hit["state"]
    res = None
    for fn in (_pixeldrain, _xfs):
        res = fn(url, host, s)
        if res and res[0] != "unknown":
            break
        res = None
    if res is None:
        res = _html(url, s)
    cache[url] = {"state": res[0], "reason": res[1], "ts": time.time()}
    return res[0]


def mirrors_of(detail, s, cache):
    out = []
    for m in (detail.get("container") or {}).get("data", {}).get("mirrors") or []:
        host = m.get("name") or "unknown"
        for link in m.get("links") or []:
            w = link.get("url")
            if not w:
                continue
            u = unwrap(w)
            if assess(u, host, s, cache) != "dead":
                out.append({"url": u, "host": host})
    return out


def fetch_list(s):
    games, seen, page, total = [], set(), 1, None
    while True:
        print(f"🔍 page {page}" + (f"/{-(-total // LIMIT)}" if total else ""))
        p = get_json(s, f"{API}?page={page}&limit={LIMIT}&sort=newest")
        total = total or p.get("total", 0)
        batch = p.get("data") or []
        if not batch:
            break
        for g in batch:
            if g.get("slug") and g["slug"] not in seen:
                seen.add(g["slug"])
                games.append({"slug": g["slug"],
                              "title": (g.get("title") or "").strip()})
        if total and len(games) >= total:
            break
        page += 1
        time.sleep(random.uniform(*DELAY_LIST))
    return games


def main():
    s = session()
    print("🚀 GameBounty daily")

    prev = {}
    if os.path.exists(OUT_FILE):
        try:
            for r in json.load(open(OUT_FILE, encoding="utf-8")):
                if r.get("slug"):
                    prev[r["slug"]] = r
        except Exception:
            prev = {}
    print(f"📄 {len(prev)} previously known")

    try:
        games = fetch_list(s)
    except Exception as e:
        print(f"❌ list failed: {e}")
        sys.exit(1)
    print(f"✅ {len(games)} games listed")

    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            cache = {}

    out, reused, fetched, failed = [], 0, 0, 0
    for i, g in enumerate(games, 1):
        old = prev.get(g["slug"])
        if old and old.get("mirrors"):
            out.append(old)
            reused += 1
            continue
        try:
            d = get_json(s, f"{API}/{g['slug']}").get("data", {})
            src, sov = norm_sources(d.get("sources"))
            out.append({"title": g["title"],
                        "url": f"{BASE}/{g['slug']}-free-pc-download",
                        "slug": g["slug"], "sources": src,
                        "is_sovereign": sov,
                        "mirrors": mirrors_of(d, s, cache)})
            fetched += 1
            print(f"   [{i}/{len(games)}] {g['slug']}")
        except Exception as e:
            failed += 1
            print(f"   ❌ {g['slug']}: {type(e).__name__}")
        if fetched and fetched % 20 == 0:
            json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"))
        time.sleep(random.uniform(*DELAY_DETAIL))

    print(f"📊 {reused} reused, {fetched} fetched, {failed} failed")

    if prev and len(out) < len(prev) * 0.9 and not FORCE:
        print(f"🛑 canary: {len(out)} vs {len(prev)} (>10% drop)")
        json.dump(out, open(OUT_FILE, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"))
        sys.exit(2)

    json.dump(out, open(OUT_FILE, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"))
    print(f"💾 saved {len(out)}")


if __name__ == "__main__":
    main()
