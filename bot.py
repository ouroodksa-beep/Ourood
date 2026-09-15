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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
CHAT_ID = os.environ.get("CHAT_ID", "432826122")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "fb7742b2e62f3699d5059eea890268dd")

MIN_DISCOUNT_PERCENT = 70.0  
TARGET_DEALS_COUNT = 30
CHUNK_SIZE = 15  

is_scanning = False
scan_lock = threading.Lock()
sent_alerts = set()

# بداية ونهاية نطاق البحث الحالي لأمازون
amazon_start_page = 1
amazon_end_page = 15

BASE_AMAZON_URLS = [
    "https://www.amazon.sa/gp/goldbox",
    "https://www.amazon.sa/gp/bestsellers/electronics",
    "https://www.amazon.sa/gp/bestsellers/mobile-phones",
    "https://www.amazon.sa/gp/bestsellers/kitchen",
    "https://www.amazon.sa/gp/bestsellers/beauty",
    "https://www.amazon.sa/gp/bestsellers/fashion"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("amazon_render_target")
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

def parse_price(value):
    if value is None: return None
    value = re.sub(r"(SAR|ر\.س|ريال|AED|USD|\$|€|£)", "", str(value), flags=re.I)
    value = re.sub(r"[^\d,\.]", "", value)
    if not value: return None
    try:
        if "," in value and "." in value:
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif "," in value:
            parts = value.split(",")
            if len(parts[-1]) == 2:
                value = value.replace(",", ".")
            else:
                value = value.replace(",", "")
        return float(value)
    except Exception:
        return None

def fetch_fast_render(target_url):
    # 🎯 تفعيل render=true لجلب صفحات أمازون الديناميكية بالكامل مثل كود نون
    payload = {
        'api_key': SCRAPER_API_KEY, 
        'url': target_url, 
        'country_code': 'sa', 
        'device_type': 'desktop',
        'render': 'true'
    }
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}
    try:
        resp = session.get('http://api.scraperapi.com', params=payload, headers=headers, timeout=30)
        if resp.status_code == 200 and len(resp.text) > 10000:
            return resp.text
    except Exception as e:
        logger.error(f"Amazon Fetch Error: {e}")
    return None

def parse_and_send_amazon_deals(html):
    if not html: return 0
    soup = BeautifulSoup(html, "lxml")
    
    cards = soup.select(
        "div[id^='post-'], "
        "div[class*='zg-grid-general-faceout'], "
        "div[class*='p13n-sc-unselected-item'], "
        "div[data-component-type='s-search-result'], "
        "div.p13n-grid-content, "
        "div.grid-item, "
        "div[data-asin]:not([data-asin=''])"
    )

    found_in_page = 0
    for card in cards:
        try:
            name_tag = card.select_one("div._cDE1C_truncate_3qMTh, span.zg-text-js-truncate, a.a-link-normal span, h2 span, span.a-size-base-plus, span.a-size-medium")
            price_tag = card.select_one("span._cDE1C_p13n-sc-price_3m33M, span.a-price span.a-offscreen, span.p13n-sc-price")
            old_price_tag = card.select_one("span.a-text-price span.a-offscreen, span.a-color-secondary.a-text-strike")
            link_tag = card.select_one("a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/'], a.a-link-normal")

            if name_tag and price_tag and old_price_tag and link_tag:
                name = name_tag.get_text(strip=True)[:120]
                price = parse_price(price_tag.get_text(strip=True))
                old_price = parse_price(old_price_tag.get_text(strip=True))
                raw_url = link_tag.get("href", "")

                if price and old_price and old_price > price:
                    discount = ((old_price - price) / old_price) * 100

                    if discount >= MIN_DISCOUNT_PERCENT:
                        clean_url = "https://www.amazon.sa" + raw_url if raw_url.startswith("/") else raw_url.split("?")[0]
                        asin_match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", clean_url)
                        pid = asin_match.group(2) if asin_match else hashlib.md5(clean_url.encode()).hexdigest()[:10]

                        alert_key = f"{pid}_{price}"
                        if alert_key not in sent_alerts:
                            sent_alerts.add(alert_key)
                            found_in_page += 1

                            msg = (
                                f"💥 <b>صيدة أمازون جديدة (خصم {round(discount)}%)!</b> 💥\n\n"
                                f"🛍 <b>المنتج:</b> {name}\n"
                                f"💰 <b>السعر الآن:</b> {price} ر.س\n"
                                f"📈 <b>السعر الأصلي:</b> {old_price} ر.س\n\n"
                                f"🔗 <b>رابط الشراء المباشر:</b>\n{clean_url}"
                            )
                            telegram_send(msg)
                            time.sleep(0.3)
        except Exception:
            continue
    return found_in_page

def run_until_target_found():
    global is_scanning, amazon_start_page, amazon_end_page
    if is_scanning: return

    with scan_lock: is_scanning = True
    telegram_send(f"🚀 <b>بدأ فحص أمازون الحقيقي (مع تفعيل JS Rendering)...</b>\nجاري البحث عن 30 صيدة بخصم 70%.")

    total_deals_found = 0
    rounds_count = 0

    while total_deals_found < TARGET_DEALS_COUNT:
        rounds_count += 1
        telegram_send(f"🔍 <b>جولة أمازون {rounds_count}:</b> جاري كشط الصفحات من [{amazon_start_page}] إلى [{amazon_end_page}]...")

        target_urls = []
        for p in range(amazon_start_page, amazon_end_page + 1):
            for u in BASE_AMAZON_URLS:
                delimiter = "&" if "?" in u else "?"
                target_urls.append(f"{u}{delimiter}pg={p}")

        # تقليل Threads إلى 3 لضمان استقرار ScraperAPI مع الـ Render
        with ThreadPoolExecutor(max_workers=3) as executor:
            html_results = list(executor.map(fetch_fast_render, target_urls))

        round_found = 0
        valid_pages_count = 0
        for html in html_results:
            if html:
                valid_pages_count += 1
                round_found += parse_and_send_amazon_deals(html)

        total_deals_found += round_found
        telegram_send(f"📊 <b>نتيجة جولة أمازون {rounds_count}:</b> تم جلب {valid_pages_count} صفحة بنجاح، ووجدنا {round_found} صيدة. (الإجمالي: {total_deals_found}/{TARGET_DEALS_COUNT})")

        amazon_start_page = amazon_end_page + 1
        amazon_end_page = amazon_start_page + CHUNK_SIZE - 1

        if amazon_start_page > 60:
            amazon_start_page = 1
            amazon_end_page = 15

        if total_deals_found < TARGET_DEALS_COUNT:
            time.sleep(3)

    telegram_send(f"🎉 <b>تم الوصول للهدف!</b> تم إرسال {total_deals_found} صيدة من أمازون.")
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
                            telegram_send("⏳ <b>جاري البحث حالياً.. يرجى الانتظار.</b>")
                        else:
                            threading.Thread(target=run_until_target_found, daemon=True).start()
        except Exception:
            pass
        time.sleep(2)

@app.route("/")
def home():
    return jsonify({"status": "amazon_render_active", "start": amazon_start_page, "end": amazon_end_page})

if __name__ == "__main__":
    telegram_send("🚀 <b>تم تحديث بوت أمازون لنظام الـ Rendering وجمع الـ 30 صيدة! أرسلي /scan للتجربة.</b>")
    threading.Thread(target=telegram_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
