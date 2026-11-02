#!/usr/bin/env python3
"""Wake Up Brief - sunucu tarafı veri toplayıcı (GitHub Actions'ta çalışır).

Çıktı: data.json  (index.html aynı origin'den okur; CORS/rss2json bağımlılığı yok)
 - ego:  EGO duyuruları (başlık, link, ISO tarih)
 - news: Google Haberler RSS (Türkiye + Dünya)
Bir kaynak başarısız olursa o kaynağın ÖNCEKİ verisi korunur (sayfa boşalmaz).
Tüm kaynaklar başarısızsa çıkış kodu 1 olur (workflow kırmızı olur, haberdar olursun).
"""
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data.json")
TZ = ZoneInfo("Europe/Istanbul")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

EGO_URL = "https://www.ego.gov.tr/duyurular"
NEWS_FEEDS = [
    ("Türkiye", "https://news.google.com/rss?hl=tr&gl=TR&ceid=TR:tr", 5),
]

# --- GDELT: günün dünyayı etkileyen başlıkları -------------------------------
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_LANG = "english"   # GDELT'te Türkçe kaynak için "turkish" yazılabilir (anahtar kelimeler İngilizce kalır)
WORLD_COUNT = 7
# Her konudan en önemli (HybridRel) haber seçilir => 7 farklı alan. Anahtar kelimeler >= 4 harf.
WORLD_TOPICS = [
    ("Çatışma", '(ceasefire OR missile OR airstrike OR invasion OR "armed conflict" OR offensive)'),
    ("Ekonomi", '("central bank" OR inflation OR "interest rates" OR recession OR "oil prices" OR tariffs)'),
    ("Diplomasi", '(summit OR sanctions OR election OR "United Nations" OR treaty OR "prime minister")'),
    ("İklim & Afet", '("climate change" OR earthquake OR hurricane OR flood OR wildfire OR typhoon)'),
    ("Sağlık", '(pandemic OR outbreak OR "World Health Organization" OR vaccine OR epidemic)'),
    ("Teknoloji", '("artificial intelligence" OR cyberattack OR semiconductor OR "data breach")'),
    ("İnsani & Enerji", '(refugees OR famine OR humanitarian OR "natural gas" OR "energy crisis")'),
]
GDELT_GAP = 5.5          # GDELT: en fazla 5 sn'de 1 istek
GDELT_MAX_PER_DOMAIN = 2
TITLE_JUNK = re.compile(r"^(just a moment|access denied|attention required|page not found|403|404|error|untitled)", re.I)

DATE_RE = re.compile(r"\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")
ID_RE = re.compile(r"/duyuru/(\d+)(?:/|$)")


def http_get(url, timeout=20, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "tr-TR,tr;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def parse_ego(html, base=EGO_URL, limit=8):
    """EGO duyuru listesi: <a href='/tr/duyuru/<id>/<slug>'>Başlık dd.mm.yyyy</a>."""
    soup = BeautifulSoup(html, "html.parser")
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        m = ID_RE.search(a["href"])
        if not m:
            continue  # menü/sayfalama linklerini (/duyurular, #pagination) ele
        did = int(m.group(1))
        if did in seen:
            continue
        seen.add(did)
        text = " ".join(a.get_text(" ", strip=True).split()) or (a.get("title") or "").strip()
        date_iso = ""
        dm = DATE_RE.search(text)
        if dm:
            d, mo, y = dm.groups()
            date_iso = f"{y}-{mo}-{d}"
            text = text[:dm.start()].strip()
        if len(text) < 5:
            continue
        items.append({"id": did, "title": text, "link": urljoin(base, a["href"]), "date": date_iso})
    items.sort(key=lambda x: x["id"], reverse=True)  # id artan = yeniden eskiye
    return items[:limit]


def parse_news(xml_text, tag, limit):
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].rstrip()
        elif not source and " - " in title:
            title, source = [p.strip() for p in title.rsplit(" - ", 1)]
        published = ""
        pub = it.findtext("pubDate")
        if pub:
            try:
                published = parsedate_to_datetime(pub).astimezone(TZ).isoformat(timespec="minutes")
            except Exception:  # noqa: BLE001
                pass
        if title and link.startswith("http"):
            out.append({"title": title, "link": link, "source": source, "tag": tag, "published": published})
        if len(out) >= limit:
            break
    return out


GF_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    # Avrupa/consent yönlendirmesini atlatmak için
    "Cookie": "CONSENT=YES+cb; SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
}
# anahtar: (Google Finance sembolü, makul değer aralığı)
GF_SYMBOLS = {
    "usd": ("USD-TRY", (1, 1000)),
    "eur": ("EUR-TRY", (1, 1000)),
    "bist": ("XU100:INDEXIST", (1000, 1_000_000)),
}
GF_PRICE_RES = [
    re.compile(r'data-last-price="([0-9][0-9.,]*)"'),
    re.compile(r'class="YMlKec fxKbKc"[^>]*>[^0-9<]*([0-9][0-9.,]*)<'),
]


def parse_google_finance(html):
    """Google Finance quote sayfasından son fiyatı (hl=en => nokta ondalık) çıkarır."""
    for rx in GF_PRICE_RES:
        m = rx.search(html)
        if m:
            return float(m.group(1).replace(",", ""))
    raise ValueError("fiyat bulunamadı (sayfa yapısı değişmiş olabilir)")


def http_get_headers(url, headers, timeout=20, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def fallback_price(key):
    """Google Finance çalışmazsa ücretsiz yedek kaynaklar."""
    if key in ("usd", "eur"):
        cur = key.upper()
        for url, getter in (
            (f"https://api.frankfurter.app/latest?from={cur}&to=TRY", lambda j: j["rates"]["TRY"]),
            (f"https://open.er-api.com/v6/latest/{cur}", lambda j: j["rates"]["TRY"]),
        ):
            try:
                return float(getter(json.loads(http_get(url, retries=1)))), url.split("/")[2]
            except Exception:  # noqa: BLE001
                continue
    if key == "bist":
        try:
            j = json.loads(http_get("https://query1.finance.yahoo.com/v8/finance/chart/XU100.IS?range=1d&interval=1d", retries=1))
            return float(j["chart"]["result"][0]["meta"]["regularMarketPrice"]), "Yahoo Finance"
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError("yedek kaynak da başarısız")


def fetch_markets(prev):
    """Her kur/endeks bağımsız: biri çökerse sadece o önceki değeriyle (eski zaman damgasıyla) kalır."""
    now = datetime.now(TZ).isoformat(timespec="minutes")
    out, fresh = {}, 0
    for key, (sym, (lo, hi)) in GF_SYMBOLS.items():
        entry = None
        try:
            html = http_get_headers(f"https://www.google.com/finance/quote/{sym}?hl=en", GF_HEADERS)
            v = parse_google_finance(html)
            if not lo <= v <= hi:
                raise ValueError(f"makul aralık dışı: {v}")
            entry = {"v": v, "src": "Google Finance", "t": now}
        except Exception as e:  # noqa: BLE001
            print(f"Google Finance {key} hatası: {e}", file=sys.stderr)
            try:
                v, src = fallback_price(key)
                if lo <= v <= hi:
                    entry = {"v": v, "src": src, "t": now}
            except Exception as e2:  # noqa: BLE001
                print(f"Yedek {key} hatası: {e2}", file=sys.stderr)
        if entry:
            fresh += 1
        else:
            entry = (prev or {}).get(key)  # eski değeri koru
        if entry:
            out[key] = entry
    return out, fresh > 0


def gdelt_search(query, timespan, retries=3):
    """GDELT DOC 2.0 ArtList -> makale listesi. Hata/oran sınırı için yeniden dener."""
    params = {
        "query": f"{query} sourcelang:{GDELT_LANG}",
        "mode": "artlist", "format": "json", "maxrecords": "20",
        "sort": "hybridrel", "timespan": timespan,
    }
    url = GDELT_URL + "?" + urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(7)
                continue
            raise
        if not body.strip():
            return []                      # eşleşme yok
        try:
            return json.loads(body).get("articles", []) or []
        except ValueError:
            if "limit requests" in body.lower() and attempt < retries - 1:
                time.sleep(7)
                continue
            raise RuntimeError("GDELT JSON dönmedi: " + body[:120].strip())
    return []


def clean_title(t):
    return " ".join(htmllib.unescape(t or "").split())


def title_key(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()[:60]


def parse_seen(s):
    """GDELT seendate '20261102T123000Z' -> yerel ISO."""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(TZ).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001
        return ""


def usable(a):
    t = clean_title(a.get("title"))
    return len(t) >= 25 and not TITLE_JUNK.match(t) and str(a.get("url", "")).startswith("http")


def select_world(pools, count=WORLD_COUNT):
    """pools: [(konu, [makale...])]. Konu başına sırayla birer tane (round-robin), tekrar ve aşırı alan adı yok."""
    seen_t, seen_u, per_dom, out = set(), set(), {}, []
    depth = 0
    while len(out) < count and any(len(arts) > depth for _, arts in pools):
        for topic, arts in pools:
            if len(out) >= count:
                break
            # bu turda bu konunun `depth`'inci uygun makalesi
            cands = [a for a in arts if usable(a)]
            if len(cands) <= depth:
                continue
            for a in cands[depth:]:
                title, url, dom = clean_title(a.get("title")), a["url"], (a.get("domain") or "").lower()
                k = title_key(title)
                if k in seen_t or url in seen_u or per_dom.get(dom, 0) >= GDELT_MAX_PER_DOMAIN:
                    continue
                seen_t.add(k); seen_u.add(url); per_dom[dom] = per_dom.get(dom, 0) + 1
                out.append({"title": title, "link": url, "source": dom, "topic": topic, "seen": parse_seen(a.get("seendate", ""))})
                break
        depth += 1
    return out


def world_window_minutes(now=None):
    """Bugünün başlangıcından (Istanbul) bu yana dakika; erken saatlerde boş kalmasın diye en az 6 saat."""
    now = now or datetime.now(TZ)
    since = int((now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60)
    return max(since, 360)


def fetch_world():
    """Bugünün 7 dünya başlığı. Dönüş: {'date','items'} ya da None (hiç başlık yoksa)."""
    minutes = world_window_minutes()
    pools = []
    for i, (topic, q) in enumerate(WORLD_TOPICS):
        if i:
            time.sleep(GDELT_GAP)
        try:
            pools.append((topic, gdelt_search(q, f"{minutes}min")))
        except Exception as e:  # noqa: BLE001
            print(f"GDELT hatası ({topic}): {e}", file=sys.stderr)
            pools.append((topic, []))
    items = select_world(pools)
    if len(items) < WORLD_COUNT:  # bugün yetmedi: boş kalan konuları son 24 saatle tamamla
        for idx, (topic, arts) in enumerate(pools):
            if any(usable(a) for a in arts):
                continue
            time.sleep(GDELT_GAP)
            try:
                pools[idx] = (topic, gdelt_search(WORLD_TOPICS[idx][1], "1440min"))
            except Exception as e:  # noqa: BLE001
                print(f"GDELT 24s hatası ({topic}): {e}", file=sys.stderr)
        items = select_world(pools)
    if not items:
        return None
    return {"date": datetime.now(TZ).strftime("%Y-%m-%d"), "items": items}


def load_previous():
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def main():
    prev = load_previous()
    ok = {"ego": False, "news": False, "markets": False, "world": False}

    ego = prev.get("ego", [])
    try:
        fresh = parse_ego(http_get(EGO_URL))
        if fresh:
            ego, ok["ego"] = fresh, True
        else:
            print("EGO: sayfa alındı ama duyuru bulunamadı (HTML yapısı değişmiş olabilir)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"EGO hatası: {e}", file=sys.stderr)

    news, news_errors = [], 0
    for tag, url, limit in NEWS_FEEDS:
        try:
            news += parse_news(http_get(url), tag, limit)
        except Exception as e:  # noqa: BLE001
            news_errors += 1
            print(f"Haber hatası ({tag}): {e}", file=sys.stderr)
    if news:
        ok["news"] = True
    else:
        news = prev.get("news", [])

    markets, ok["markets"] = fetch_markets(prev.get("markets"))

    world = prev.get("world") or {"date": "", "items": []}
    try:
        w = fetch_world()
        if w:
            world, ok["world"] = w, True
        else:
            print("GDELT: başlık bulunamadı; önceki dünya başlıkları korundu.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"GDELT hatası: {e}", file=sys.stderr)

    if not any(ok.values()):
        print("Hiçbir kaynak alınamadı; mevcut data.json korundu.", file=sys.stderr)
        if not os.path.exists(OUT):
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump({"updated": "", "ego": [], "news": [], "ok": ok}, f, ensure_ascii=False, indent=2)
        sys.exit(1)

    data = {
        "updated": datetime.now(TZ).isoformat(timespec="minutes"),
        "ego": ego,
        "news": news,
        "markets": markets,
        "world": world,
        "ok": ok,
    }
    # Gereksiz commit'i önle: içerik (updated hariç) değişmediyse dosyaya dokunma.
    strip = lambda d: {k: v for k, v in d.items() if k != "updated"}  # noqa: E731
    if os.path.exists(OUT) and strip(prev) == strip(data):
        print("Değişiklik yok.")
        return
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"data.json yazıldı: {len(ego)} EGO duyurusu, {len(news)} haber, piyasa={list(markets)}, dünya={len(world.get('items', []))}, ok={ok}")


if __name__ == "__main__":
    main()
