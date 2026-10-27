#!/usr/bin/env python3
"""Wake Up Brief - sunucu tarafı veri toplayıcı (GitHub Actions'ta çalışır).

Çıktı: data.json  (index.html aynı origin'den okur; CORS/rss2json bağımlılığı yok)
 - ego:  EGO duyuruları (başlık, link, ISO tarih)
 - news: Google Haberler RSS (Türkiye + Dünya)
Bir kaynak başarısız olursa o kaynağın ÖNCEKİ verisi korunur (sayfa boşalmaz).
Tüm kaynaklar başarısızsa çıkış kodu 1 olur (workflow kırmızı olur, haberdar olursun).
"""
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
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
    ("Dünya", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=tr&gl=TR&ceid=TR:tr", 3),
]

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


def load_previous():
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def main():
    prev = load_previous()
    ok = {"ego": False, "news": False, "markets": False}

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
        "ok": ok,
    }
    # Gereksiz commit'i önle: içerik (updated hariç) değişmediyse dosyaya dokunma.
    strip = lambda d: {k: v for k, v in d.items() if k != "updated"}  # noqa: E731
    if os.path.exists(OUT) and strip(prev) == strip(data):
        print("Değişiklik yok.")
        return
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"data.json yazıldı: {len(ego)} EGO duyurusu, {len(news)} haber, piyasa={list(markets)}, ok={ok}")


if __name__ == "__main__":
    main()
