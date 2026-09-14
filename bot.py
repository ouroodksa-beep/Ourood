import os
import sys
import time
import re
import hashlib
import threading
import logging
from datetime import datetime

import requests
import pandas as pd
from flask import Flask, jsonify
from bs4 import BeautifulSoup

# ============================================================
# SETTINGS & CONFIGURATION
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
CHAT_ID = os.environ.get("CHAT_ID", "432826122")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "fb7742b2e62f3699d5059eea890268dd")

# 🎯 نسبة الخصم المطلوب الوصول إليها بالضبط (70%)
MIN_DISCOUNT_PERCENT = 70.0  
TARGET_DEALS_COUNT = 20  # الحد الأدنى المباشر للنتائج قبل الإرسال (من 20 لـ 25 صيدة)

PRICE_FILE = "amazon_sa_prices.csv"
ALERT_FILE = "amazon_sa_alerts.csv"

is_scanning = False
scan_lock = threading.Lock()

# 🎯 قائمة موسعة من أقسام أمازون لضمان العثور على 20+ صيدة
DISCOVERY_URLS = [
    "https://www.amazon.sa/gp/goldbox", # عروض اليوم والتخفيضات
    "https://www.amazon.sa/gp/bestsellers/electronics",
    "https://www.amazon.sa/gp/bestsellers/mobile-phones",
    "https://www.amazon.sa/gp/bestsellers/computers",
    "https://www.amazon.sa/gp/bestsellers/kitchen",
    "https://www.amazon.sa/gp/bestsellers/beauty",
    "https://www.amazon.sa/gp/bestsellers/supermarket",
    "https://www.amazon.sa/gp/bestsellers/fashion",
    "https://www.amazon.sa/gp/bestsellers/toys"
]

# ============================================================
# LOGGING & APP SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("amazon_sa_bot")

app = Flask(__name__)
session = requests.Session()

def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=15
        )
        return r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def parse_price(value):
    if value is None:
        return None
    value = re.sub(r"(SAR|ر\.س|ريال|AED|USD|\$|€|£)", "", str(value), flags=re.I)
    value = re.sub(r"[^\d,\.]", "", value)
    if not value:
        return None
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

def fetch_direct(target_url):
    payload = {
        'api_key': SCRAPER_API_KEY,
        'url': target_url,
        'country_code': 'sa',
        'device_type': 'desktop'
    }
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}
    try:
        resp = session.get('http://api.scraperapi.com', params=payload, headers=headers, timeout=30)
        if resp.status_code == 200 and len(resp.text) > 5000:
            return resp.text
    except Exception as e:
        logger.warning(f"Fetch error: {e}")
    return None

def extract_products(html):
    soup = BeautifulSoup(html, "lxml")
    products = []

    cards = soup.select(
        "div[id^='post-'], "
        "div[class*='zg-grid-general-faceout'], "
        "div[class*='p13n-sc-unselected-item'], "
        "div[data-component-type='s-search-result'], "
        "div.p13n-grid-content, "
        "div.grid-item"
    )

    for card in cards:
        try:
            name_tag = card.select_one("div._cDE1C_truncate_3qMTh, span.zg-text-js-truncate, a.a-link-normal span, h2 span")
            name = name_tag.get_text(strip=True) if name_tag else None

            price_tag = card.select_one("span._cDE1C_p13n-sc-price_3m33M, span.a-price span.a-offscreen, span.p13n-sc-price")
            price = parse_price(price_tag.get_text(strip=True)) if price_tag else None

            old_price_tag = card.select_one("span.a-text-price span.a-offscreen, span.a-color-secondary.a-text-strike")
            old_price = parse_price(old_price_tag.get_text(strip=True)) if old_price_tag else None

            link_tag = card.select_one("a.a-link-normal[href*='/dp/'], a.a-link-normal")
            raw_url = link_tag.get("href") if link_tag else None

            if name and price and old_price and raw_url and old_price > price:
                clean_url = "https://www.amazon.sa" + raw_url if raw_url.startswith("/") else raw_url.split("?")[0]
                asin_match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", clean_url)
                product_id = asin_match.group(2) if asin_match else hashlib.md5(clean_url.encode()).hexdigest()[:16]

                products.append({
                    "product_id": product_id,
                    "product": name[:150],
                    "url": clean_url,
                    "price": price,
                    "old_price": old_price
                })
        except Exception:
            continue

    return products

# ============================================================
# DEEP SCAN UNTIL 20+ DEALS ARE FOUND
# ============================================================
def run_scan():
    global is_scanning
    if is_scanning:
        return 0

    with scan_lock:
        is_scanning = True

    try:
        logger.info("Starting Amazon SA scan targeting 20-25 deals with 70%+ discount...")
        collected_deals = []
        scanned_urls = set()

        # الاستمرار في الفحص والتدوير في الأقسام حتى تجميع 20 صيدة على الأقل
        for url in DISCOVERY_URLS:
            if len(collected_deals) >= 25:
                break
                
            if url in scanned_urls:
                continue

            scanned_urls.add(url)
            html = fetch_direct(url)
            if html:
                items = extract_products(html)
                for item in items:
                    discount = ((item["old_price"] - item["price"]) / item["old_price"]) * 100
                    
                    if discount >= MIN_DISCOUNT_PERCENT:
                        # منع تكرار نفس المنتج في القائمة
                        if not any(d["product_id"] == item["product_id"] for d in collected_deals):
                            item["discount"] = round(discount, 1)
                            collected_deals.append(item)

                            if len(collected_deals) >= 25:
                                break
            time.sleep(1)

        # 🛑 لا يتم إرسال أي نتائج للتليجرام إلا إذا كانت الصيدات 20 صيدة فأكثر
        if len(collected_deals) >= TARGET_DEALS_COUNT:
            telegram_send(f"🔥 <b>تم العثور على {len(collected_deals)} صيدة خيالية بنسبة خصم 70%+ من أمازون!</b>\nجاري إرسالها الآن...")
            
            for deal in collected_deals:
                msg = (
                    "💥 <b>صيدة أمازون (خصم 70%+)!</b> 💥\n\n"
                    f"🛍 <b>المنتج:</b> {deal['product']}\n"
                    f"💰 <b>السعر الحالي:</b> {deal['price']} ر.س\n"
                    f"📈 <b>السعر الأصلي:</b> {deal['old_price']} ر.س\n"
                    f"🔥 <b>نسبة الخصم:</b> {deal['discount']}%\n\n"
                    f"🔗 <b>رابط الشراء:</b>\n{deal['url']}"
                )
                telegram_send(msg)
                time.sleep(1)
        else:
            logger.info(f"Only found {len(collected_deals)} deals with 70% discount. Waiting for next cycle to reach 20+.")

        return len(collected_deals)
    finally:
        is_scanning = False

# ============================================================
# TELEGRAM LISTENER
# ============================================================
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
                            telegram_send("⏳ <b>جاري البحث عن 20-25 صيدة بخصم 70%+... يرجى الانتظار.</b>")
                        else:
                            telegram_send("⚡️ <b>بدأ البحث العميق.. لن يتم الإرسال إلا عند تجميع 20 صيدة على الأقل!</b>")
                            threading.Thread(target=run_scan, daemon=True).start()
        except Exception:
            pass
        time.sleep(2)

@app.route("/")
def home():
    return jsonify({"status": "online", "target_discount": "70%", "min_deals_threshold": TARGET_DEALS_COUNT})

@app.route("/scan")
def manual_scan():
    if not is_scanning:
        threading.Thread(target=run_scan, daemon=True).start()
        return jsonify({"status": "started"})
    return jsonify({"status": "busy"})

if __name__ == "__main__":
    telegram_send("🚀 <b>تم تشغيل بوت أمازون (خصم 70%+)! جاهز للبحث عن 20+ صيدة.</b>")
    threading.Thread(target=telegram_listener, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
