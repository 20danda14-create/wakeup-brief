import urllib.request
import json
import re
from bs4 import BeautifulSoup

URL = "https://www.ego.gov.tr/duyurular"

# Kendimizi gerçek bir Chrome tarayıcısı gibi tanıtıyoruz (CORS / Cloudflare engelini aşmak için)
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

req = urllib.request.Request(URL, headers=headers)

try:
    html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    
    duyurular = []
    # EGO Duyurular sayfasındaki link ve başlık yapısını okuyoruz
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if '/duyuru/' in href or 'duyuru' in href.lower():
            full_url = href if href.startswith('http') else f"https://www.ego.gov.tr{href}"
            if text and len(text) > 10 and not any(d['title'] == text for d in duyurular):
                duyurular.append({
                    "title": text,
                    "link": full_url,
                    "date": ""
                })
    
    # En güncel 10 duyuruyu kaydediyoruz
    with open('duyurular.json', 'w', encoding='utf-8') as f:
        json.dump(duyurular[:10], f, ensure_ascii=False, indent=2)
        
    print(f"Başarıyla {len(duyurular[:10])} duyuru çekildi.")

except Exception as e:
    print(f"EGO Veri Çekme Hatası: {e}")
    # Hata durumunda workflow'un çöküp e-posta atmasını engellemek için boş/mevcut JSON bırakıyoruz
    try:
        with open('duyurular.json', 'r', encoding='utf-8') as f:
            pass
    except:
        with open('duyurular.json', 'w', encoding='utf-8') as f:
            json.dump([], f)
