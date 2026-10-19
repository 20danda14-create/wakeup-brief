import requests
from bs4 import BeautifulSoup
import json

URL = "https://www.ego.gov.tr/duyurular"
DATA_FILE = "duyurular.json"

def fetch_ego_announcements():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    response = requests.get(URL, headers=headers)
    if response.status_code != 200:
        print("Sayfa yüklenemedi!")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    announcements = []

    # EGO sitesindeki duyuru başlıklarını ve linklerini buluyoruz
    items = soup.select('.news-list-item, .duyuru-item, tr')
    
    for item in items:
        link_tag = item.find('a')
        if link_tag and link_tag.get('href'):
            title = link_tag.text.strip()
            link = link_tag['href']
            if not link.startswith('http'):
                link = "https://www.ego.gov.tr" + link
            
            if title:
                announcements.append({
                    "title": title,
                    "link": link
                })

    return announcements

if __name__ == "__main__":
    new_data = fetch_ego_announcements()
    if new_data:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        print("Duyurular başarıyla kaydedildi.")
