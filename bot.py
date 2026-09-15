import os
import sys
import time
import re
import hashlib
import threading
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8603881184:AAH_x-NK8zDqRnt2kRlp2Ti6GbBFAiDzuuo")
CHAT_ID = os.environ.get("CHAT_ID", "432826122")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "fb7742b2e62f3699d5059eea890268dd")

MIN_DISCOUNT_PERCENT = 70.0  
CHUNK_SIZE = 15  # 🎯 نطاق البحث في المرة الواحدة (15 صفحة)

is_scanning = False
scan_lock = threading.Lock()
sent_alerts = set()

# بداية ونهاية نطاق البحث الحالي
start_page = 1
end_page = 15

BASE_NOON_URLS = [
    "https://minutes.noon.com/saudi-ar/",
    "https://www.noon.com/saudi-ar/bestsellers/",
    "https://www.noon.com/saudi-ar/mega-deals/",
    "https://www.noon.com/saudi-ar/electronics-and-mobile/bestseller",
    "https://www.noon.com/saudi-ar/home-and-kitchen/bestseller",
    "https://www.noon.com/saudi-ar/beauty-and-health/bestseller"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("noon_chunked")
app = Flask(__name__)
session = requests.Session()

def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID: return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Telegram Error: {e}")
        return False

def parse_price(val):
    if not val: return None
    cleaned = re.sub(r"[^\d\.]", "", str(val).replace(",", ""))
    try: return float(cleaned)
    except: return None

def fetch_fast(url):
    payload = {'api_key': SCRAPER_API_KEY, 'url': url, 'country_code': 'sa'}
    try:
        resp = session.get('http://api.scraperapi.com', params=payload, timeout=12)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def parse_and_send_deals(html):
    if not html: return 0
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div[class*='productContainer'], span[class*='productBlock'], div.sc-2023ee08-0, div[class*='minutesCard'], div[class*='grid'] > div")
    
    found_in_page = 0
    for card in cards:
        try:
            title_tag = card.select_one("div[data-qa='product-name'], [class*='title'], [class*='name']")
            curr_price_tag = card.select_one("strong[class*='amount'], span[class*='currency'] + strong, [class*='price']")
            old_price_tag = card.select_one("span[class*='oldPrice'], span[class*='strike']")
            link_tag = card.select_one("a")

            if title_tag and curr_price_tag and old_price_tag and link_tag:
                title = title_tag.get_text(strip=True)[:120]
                price = parse_price(curr_price_tag.get_text())
                old_price = parse_price(old_price_tag.get_text())
                href = link_tag.get("href", "")

                if price and old_price and old_price > price:
                    discount = ((old_price - price) / old_price) * 100
                    
                    if discount >= MIN_DISCOUNT_PERCENT:
                        full_url = href if "minutes.noon.com" in href else f"https://www.noon.com{href}"
                        sku_match = re.search(r"/([A-Z0-9]+)/p", full_url)
                        pid = sku_match.group(1) if sku_match else hashlib.md5(full_url.encode()).hexdigest()[:10]
                        
                        alert_key = f"{pid}_{price}"
                        if alert_key not in sent_alerts:
                            sent_alerts.add(alert_key)
                            found_in_page += 1
                            
                            badge = "⚡️ <b>[نون مينتس]</b>\n" if "minutes" in full_url else "🔥 <b>[تخفيضات نون]</b>\n"
                            msg = (
                                f"💛 <b>صيدة جديدة (خصم {round(discount)}%)!</b> 💛\n"
                                f"{badge}"
                                f"🛍 <b>المنتج:</b> {title}\n"
                                f"💰 <b>السعر الآن:</b> {price} ر.س\n"
                                f"📈 <b>السعر قبل:</b> {old_price} ر.س\n\n"
                                f"🔗 <b>رابط الشراء المباشر:</b>\n{full_url}"
                            )
                            telegram_send(msg)
                            time.sleep(0.3)
        except Exception:
            continue
    return found_in_page

def run_fast_scan():
    global is_scanning, start_page, end_page
    if is_scanning: return

    with scan_lock: is_scanning = True
    telegram_send(f"⚡️ <b>بدأ فحص نون للشريحة من صفحة [{start_page}] إلى [{end_page}].. جاري البحث!</b>")

    # 🚀 إنتاج 15 صفحة لكل قسم
    target_urls = []
    for p in range(start_page, end_page + 1):
        for u in BASE_NOON_URLS:
            if "minutes.noon.com" in u:
                if p == start_page: target_urls.append(u)
            else:
                delimiter = "&" if "?" in u else "?"
                target_urls.append(f"{u}{delimiter}page={p}")

    # فحص موازي باستخدام 10 مسارات
    with ThreadPoolExecutor(max_workers=10) as executor:
        html_results = list(executor.map(fetch_fast, target_urls))

    total_sent = 0
    for html in html_results:
        total_sent += parse_and_send_deals(html)

    telegram_send(f"✅ <b>انتهى فحص الصفحات من [{start_page}] إلى [{end_page}]!</b>\nتم إرسال <b>{total_sent}</b> صيدة.\n💡 الفحص القادم سيبدأ من الصفحة [{end_page + 1}] إلى [{end_page + CHUNK_SIZE}].")
    
    # 🔄 تحديث الصفحة للدفعة القادمة (16-30، ثم 31-45، وهكذا لغاية 60)
    start_page = end_page + 1
    end_page = start_page + CHUNK_SIZE - 1
    
    if start_page > 60:
        start_page = 1
        end_page = 15

    is_scanning = False

def telegram_listener():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = session.get(url, params={"offset": last_update_id + 1, "timeout": 20}, timeout=25)
            if resp.status_code == 200 and resp.json().get("ok"):
                for update in resp.json().get("result", []):
                    last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip()

                    if text in ["/scan", "/scan@"]:
                        if is_scanning:
                            telegram_send("⏳ <b>جاري الفحص حالياً.. يرجى الانتظار.</b>")
                        else:
                            threading.Thread(target=run_fast_scan, daemon=True).start()
        except Exception:
            pass
        time.sleep(2)

@app.route("/")
def home():
    return jsonify({"status": "noon_15chunk_online", "next_start_page": start_page, "next_end_page": end_page})

if __name__ == "__main__":
    telegram_send("🚀 <b>تم تشغيل بوت نون (نظام الـ 15 صفحة)! أرسلي /scan للبدء.</b>")
    threading.Thread(target=telegram_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
