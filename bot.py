import os
import sys
import time
import json
import re
import hashlib
import threading
import logging
from datetime import datetime
from urllib.parse import urljoin, quote

import requests
import pandas as pd
from flask import Flask, jsonify
from bs4 import BeautifulSoup

# ============================================================
# SETTINGS & CONFIGURATION
# ============================================================
BOT_TOKEN = "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA"
CHAT_ID = "432826122"

# 🔑 ScraperAPI Key
SCRAPER_API_KEY = "f56b509d66fa5a0f2b234473858004b7"

# إعدادات الفحص والتنبيه
MIN_DISCOUNT_PERCENT = 25.0  # الحد الأدنى لنسبة الخصم المقبولة (25%)
MIN_HISTORY = 1             # عدد مرات تسجيل السعر السابقة للتأكد من الخصم
MAX_PRODUCTS = 300
REQUEST_DELAY = 2.0
SCAN_INTERVAL_MINUTES = 60
PRICE_FILE = "amazon_sa_prices.csv"
ALERT_FILE = "amazon_sa_alerts.csv"
SELF_PING_INTERVAL = 600

# ============================================================
# AMAZON SA BESTSELLERS URLS
# ============================================================
DISCOVERY_URLS = [
    "https://www.amazon.sa/gp/bestsellers/electronics",
    "https://www.amazon.sa/gp/bestsellers/mobile-phones",
    "https://www.amazon.sa/gp/bestsellers/computers",
    "https://www.amazon.sa/gp/bestsellers/kitchen",
    "https://www.amazon.sa/gp/bestsellers/beauty",
    "https://www.amazon.sa/gp/bestsellers/supermarket",
    "https://www.amazon.sa/gp/movers-and-shakers/electronics",
    "https://www.amazon.sa/gp/movers-and-shakers/mobile-phones"
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

# ============================================================
# TELEGRAM HELPER
# ============================================================
def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set!")
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20
        )
        return r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ============================================================
# CLEAN URL BUILDER
# ============================================================
def get_clean_url(url):
    """استخراج رابط المنتج المباشر بالنظام القياسي المباشر لـ ASIN"""
    asin_match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", url)
    if asin_match:
        asin = asin_match.group(2)
        return f"https://www.amazon.sa/dp/{asin}"
    return url.split("?")[0]

# ============================================================
# DATABASE MANAGEMENT
# ============================================================
PRICE_COLUMNS = ["product_id", "product", "url", "price", "timestamp"]

def load_database():
    global prices, sent_alerts
    if os.path.exists(PRICE_FILE):
        try:
            prices = pd.read_csv(PRICE_FILE)
            if not prices.empty:
                prices["timestamp"] = pd.to_datetime(prices["timestamp"], errors="coerce")
        except Exception:
            prices = pd.DataFrame(columns=PRICE_COLUMNS)
    else:
        prices = pd.DataFrame(columns=PRICE_COLUMNS)

    if os.path.exists(ALERT_FILE):
        try:
            alert_df = pd.read_csv(ALERT_FILE)
            sent_alerts = set(alert_df["alert_id"].astype(str).tolist())
        except Exception:
            sent_alerts = set()
    else:
        sent_alerts = set()

def save_database():
    try:
        prices.to_csv(PRICE_FILE, index=False)
        pd.DataFrame({"alert_id": list(sent_alerts)}).to_csv(ALERT_FILE, index=False)
    except Exception as e:
        logger.error(f"Save error: {e}")

load_database()

# ============================================================
# FETCH VIA SCRAPERAPI & PARSE
# ============================================================
def parse_price(value):
    if value is None:
        return None
    value = str(value)
    value = re.sub(r"(SAR|ر\.س|ريال|AED|USD|\$|€|£)", "", value, flags=re.I)
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

def fetch_direct(url, retries=2):
    """جلب الصفحة من خلال ScraperAPI لتجاوز الحظر كلياً"""
    payload = {
        'api_key': SCRAPER_API_KEY,
        'url': url,
        'country_code': 'sa',
        'render': 'false'
    }
    
    for attempt in range(retries + 1):
        try:
            logger.info(f"Fetching via ScraperAPI (Attempt {attempt + 1}): {url}")
            resp = session.get('http://api.scraperapi.com', params=payload, timeout=60)
            
            if resp.status_code == 200 and len(resp.text) > 5000:
                logger.info(f"Successfully fetched | Length: {len(resp.text)}")
                return resp.text
            
            logger.warning(f"ScraperAPI status: {resp.status_code}")
            time.sleep(3)
        except Exception as e:
            logger.warning(f"Fetch error: {e}")
            time.sleep(3)
            
    return None

def extract_bestsellers_from_html(html):
    soup = BeautifulSoup(html, "lxml")
    products = []

    # البحث عن حاويات المنتجات بجميع التنسيقات الممكنة في أمازون
    cards = soup.select(
        "div[id^='post-'], "
        "div[class*='zg-grid-general-faceout'], "
        "div[class*='p13n-sc-unselected-item'], "
        "div.zg-carousel-general-faceout, "
        "div[data-component-type='s-search-result'], "
        "div.p13n-sc-shoveler div[class*='a-cardui']"
    )

    if not cards:
        cards = soup.select("div.a-section.p13n-asin, div[data-asin]")

    logger.info(f"HTML Cards found: {len(cards)}")

    for card in cards:
        try:
            # 1. استخراج الاسم
            name = None
            for selector in [
                "span.zg-text-js-truncate",
                "div._cDE1C_truncate_3qMTh",
                "a.a-link-normal span",
                "h2 span",
                "div[class*='p13n-sc-css-line-clamp']",
                ".a-size-base-plus",
                "span.a-size-medium"
            ]:
                tag = card.select_one(selector)
                if tag and len(tag.get_text(strip=True)) > 3:
                    name = tag.get_text(strip=True)
                    break

            # 2. استخراج السعر
            price = None
            for selector in [
                "span._cDE1C_p13n-sc-price_3m33M",
                "span.a-price span.a-offscreen",
                "span.a-price-whole",
                "span.p13n-sc-price",
                "span.a-color-price"
            ]:
                tag = card.select_one(selector)
                if tag:
                    price = parse_price(tag.get_text(strip=True))
                    if price and price > 0:
                        break

            # 3. استخراج الرابط والـ ASIN
            raw_url = None
            link_tag = card.select_one("a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/']")
            if not link_tag:
                link_tag = card.select_one("a.a-link-normal")

            if link_tag and link_tag.get("href"):
                href = link_tag["href"]
                raw_url = "https://www.amazon.sa" + href if href.startswith("/") else href

            if name and price and price > 0 and raw_url:
                clean_url = get_clean_url(raw_url)
                
                asin_match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", raw_url)
                if asin_match:
                    product_id = asin_match.group(2)
                else:
                    product_id = card.get("data-asin") or hashlib.md5(raw_url.encode()).hexdigest()[:16]

                products.append({
                    "product_id": product_id,
                    "product": str(name).strip()[:180],
                    "url": clean_url,
                    "price": price
                })
        except Exception:
            continue

    unique_in_page = {}
    for p in products:
        unique_in_page[p["product_id"]] = p

    return list(unique_in_page.values())

# ============================================================
# PROCESSING & GLITCH SCAN
# ============================================================
def process_and_check_deals(discovered_products):
    global prices
    alerts_to_send = []

    for item in discovered_products:
        pid = item["product_id"]
        current_price = item["price"]

        new_row = pd.DataFrame([{
            "product_id": pid,
            "product": item["product"],
            "url": item["url"],
            "price": current_price,
            "timestamp": datetime.now()
        }])
        prices = pd.concat([prices, new_row], ignore_index=True)

        prod_history = prices[prices["product_id"] == pid].sort_values("timestamp")
        
        if len(prod_history) >= (MIN_HISTORY + 1):
            old_prices = prod_history["price"].iloc[:-1].astype(float)
            ref_price = float(old_prices.median())

            if ref_price > current_price:
                discount = ((ref_price - current_price) / ref_price) * 100
                
                if discount >= MIN_DISCOUNT_PERCENT:
                    alert_id = f"{pid}_{current_price}_{round(discount)}"
                    if alert_id not in sent_alerts:
                        alerts_to_send.append({
                            "alert_id": alert_id,
                            "product": item["product"],
                            "current_price": current_price,
                            "ref_price": ref_price,
                            "discount": round(discount, 1),
                            "url": item["url"]
                        })

    return alerts_to_send

def run_scan():
    logger.info("=" * 60)
    logger.info("STARTING AMAZON SA BESTSELLERS SCAN")
    logger.info("=" * 60)

    all_discovered = []

    for url in DISCOVERY_URLS:
        html = fetch_direct(url)
        if html:
            items = extract_bestsellers_from_html(html)
            all_discovered.extend(items)
            logger.info(f"Extracted {len(items)} items from: {url}")
        time.sleep(REQUEST_DELAY)

    if not all_discovered:
        logger.warning("No products found across all URLs.")
        telegram_send("⚠️ <b>تنبيه البوت:</b> لم يتم العثور على منتجات. يرجى التأكد من مفتاح ScraperAPI.")
        return 0

    unique_products = list({p["product_id"]: p for p in all_discovered}.values())
    logger.info(f"Unique products extracted: {len(unique_products)}")

    deals = process_and_check_deals(unique_products)
    save_database()

    for deal in deals:
        msg = (
            "🔥 <b>صيدة جديدة من الأكثر مبيعاً (Best Seller)!</b> 🔥\n\n"
            f"🛍 <b>المنتج:</b> {deal['product']}\n"
            f"💰 <b>السعر الحالي:</b> {deal['current_price']} ر.س\n"
            f"📈 <b>السعر السابق:</b> {deal['ref_price']} ر.س\n"
            f"💥 <b>نسبة الخصم:</b> {deal['discount']}%\n\n"
            f"🔗 <b>رابط الشراء:</b>\n{deal['url']}"
        )
        if telegram_send(msg):
            sent_alerts.add(deal["alert_id"])
            save_database()
            time.sleep(1)

    logger.info(f"Scan finished. New deals sent: {len(deals)}")
    return len(deals)

# ============================================================
# BACKGROUND SCHEDULER & KEEP-ALIVE
# ============================================================
def background_scanner():
    while True:
        try:
            run_scan()
        except Exception as e:
            logger.error(f"Error in background scan: {e}")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

def self_ping():
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
            session.get(url.rstrip("/") + "/ping", timeout=10)
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
        time.sleep(SELF_PING_INTERVAL)

# ============================================================
# FLASK ENDPOINTS
# ============================================================
@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Amazon SA Best Sellers Hunter",
        "tracked_products": len(prices["product_id"].unique()) if not prices.empty else 0
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()})

@app.route("/scan")
def manual_scan():
    deals_count = run_scan()
    return jsonify({"status": "success", "deals_sent": deals_count})

@app.route("/test-fetch")
def test_fetch():
    url = DISCOVERY_URLS[0]
    html = fetch_direct(url)
    if html:
        products = extract_bestsellers_from_html(html)
        return jsonify({
            "success": True,
            "html_length": len(html),
            "products_found": len(products),
            "sample": products[:2] if products else []
        })
    return jsonify({"success": False, "error": "Fetch failed"}), 500

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    threading.Thread(target=background_scanner, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
