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
# SETTINGS
# ============================================================
BOT_TOKEN = "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA"
CHAT_ID = "432826122"

# تعديل الحد الأدنى للخصم (25% بدلاً من 50-90% لقتنص العروض الحقيقية على الأكثر مبيعاً)
MIN_DISCOUNT_PERCENT = 25.0 
MIN_HISTORY = 1          # عدد المرات التي يجب أن نسجل فيها السعر قبل اعتبار التخفيض حقيقي
MIN_REVIEWS_COUNT = 50   # الحد الأدنى لعدد التقييمات لضمان أن المنتج مطلوب
MAX_PRODUCTS = 300
REQUEST_DELAY = 3.0
SCAN_INTERVAL_MINUTES = 60
PRICE_FILE = "amazon_sa_prices.csv"
ALERT_FILE = "amazon_sa_alerts.csv"
SELF_PING_INTERVAL = 600

# ============================================================
# AMAZON SA BESTSELLERS URLS (روابط الأكثر مبيعاً والأعلى حركة)
# ============================================================
DISCOVERY_URLS = [
    "https://www.amazon.sa/gp/bestsellers/electronics",
    "https://www.amazon.sa/gp/bestsellers/mobile-phones",
    "https://www.amazon.sa/gp/bestsellers/computers",
    "https://www.amazon.sa/gp/bestsellers/kitchen",
    "https://www.amazon.sa/gp/bestsellers/beauty",
    "https://www.amazon.sa/gp/bestsellers/supermarket",
    "https://www.amazon.sa/gp/movers-and-shakers/electronics", # الأكثر زيادة في المبيعات حالياً
    "https://www.amazon.sa/gp/movers-and-shakers/mobile-phones"
]

AMAZON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("amazon_sa_bestseller_bot")

app = Flask(__name__)
session = requests.Session()

# ============================================================
# TELEGRAM
# ============================================================
def telegram_send(message):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set!")
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=20
        )
        return r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ============================================================
# DATABASE
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
# FETCH & PARSE
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

def fetch_direct(url):
    try:
        resp = session.get(url, headers=AMAZON_HEADERS, timeout=30)
        if resp.status_code == 200 and "captcha" not in resp.text.lower():
            return resp.text
    except Exception as e:
        logger.warning(f"Fetch error: {e}")
    return None

def extract_bestsellers_from_html(html, source_url):
    soup = BeautifulSoup(html, "lxml")
    products = []

    # استخراج منتجات صفحات Best Sellers القياسية
    cards = soup.select(".zg-grid-general-faceout, #zg-right-col .p13n-sc-unselected-item, div[id^='post-']")
    if not cards:
        cards = soup.find_all("div", attrs={"data-component-type": "s-search-result"})

    for card in cards:
        try:
            # الاسم
            name_tag = card.select_one("span.zg-text-js-truncate, ._cDE1C_truncate_3qMTh, h2 span, .a-size-base-plus")
            name = name_tag.get_text(strip=True) if name_tag else None

            # السعر
            price_tag = card.select_one("._cDE1C_p13n-sc-price_3m33M, .a-price .a-offscreen, .a-price-whole")
            price = parse_price(price_tag.get_text(strip=True)) if price_tag else None

            # الرابط
            link_tag = card.select_one("a.a-link-normal")
            url = "https://www.amazon.sa" + link_tag["href"] if link_tag and link_tag.get("href") else None

            # التقييمات (لمعرفة قوة الإقبال)
            rating_tag = card.select_one(".a-icon-row a, .a-size-small")
            reviews_text = rating_tag.get_text(strip=True) if rating_tag else "0"
            reviews_count = int(re.sub(r"\D", "", reviews_text)) if re.sub(r"\D", "", reviews_text) else 0

            if name and price and price > 0 and url:
                # تنظيف الرابط للحصول على ASIN فقط
                clean_url = url.split("?")[0].split("/ref=")[0]
                asin_match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", clean_url)
                product_id = asin_match.group(2) if asin_match else hashlib.md5(clean_url.encode()).hexdigest()[:16]

                products.append({
                    "product_id": product_id,
                    "product": name[:180],
                    "url": clean_url,
                    "price": price,
                    "reviews": reviews_count
                })
        except Exception:
            continue

    return products

# ============================================================
# SCAN & CALCULATE TOPTIER DEALS
# ============================================================
def process_and_check_deals(discovered_products):
    global prices
    alerts_to_send = []

    for item in discovered_products:
        pid = item["product_id"]
        current_price = item["price"]

        # 1. إدخال السعر الجديد لقاعدة البيانات
        new_row = pd.DataFrame([{
            "product_id": pid,
            "product": item["product"],
            "url": item["url"],
            "price": current_price,
            "timestamp": datetime.now()
        }])
        prices = pd.concat([prices, new_row], ignore_index=True)

        # 2. تحليل التاريخ السعري للمنتج
        prod_history = prices[prices["product_id"] == pid].sort_values("timestamp")
        
        if len(prod_history) >= (MIN_HISTORY + 1):
            old_prices = prod_history["price"].iloc[:-1].astype(float)
            ref_price = float(old_prices.median()) # السعر الاعتيادي السابق

            if ref_price > current_price:
                discount = ((ref_price - current_price) / ref_price) * 100
                
                # شرط العرض: خصم أكبر من المحددة + المنتج من الأكثر مبيعاً
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
    logger.info("🔥 بدء فحص منتجات الأكثر مبيعاً (Best Sellers)...")
    all_discovered = []

    for url in DISCOVERY_URLS:
        html = fetch_direct(url)
        if html:
            items = extract_bestsellers_from_html(html, url)
            all_discovered.extend(items)
            logger.info(f"تم سحب {len(items)} منتج من: {url}")
        time.sleep(REQUEST_DELAY)

    # إزالة التكرار
    unique_products = {p["product_id"]: p for p in all_discovered}.values()
    logger.info(f"إجمالي المنتجات المكتشفة فريدة: {len(unique_products)}")

    # معالجة الأسعار وتحديد العروض القوية
    deals = process_and_check_deals(unique_products)
    save_database()

    # إرسال التنبيهات
    for deal in deals:
        msg = (
            "🔥 <b>صيدة جديدة من الأكثر مبيعاً (Best Seller)!</b> 🔥\n\n"
            f"🛍 <b>المنتج:</b> {deal['product']}\n"
            f"💰 <b>السعر الحالي:</b> {deal['current_price']} ر.س\n"
            f"📈 <b>السعر السابق:</b> {deal['ref_price']} ر.س\n"
            f"💥 <b>نسبة الخصم:</b> {deal['discount']}%\n\n"
            f"🔗 <b>رابط الشراء:</b> {deal['url']}"
        )
        if telegram_send(msg):
            sent_alerts.add(deal["alert_id"])
            save_database()
            time.sleep(1)

    logger.info(f"تم الانتهاء! عدد العروض المكتشفة والمرسلة: {len(deals)}")
    return len(deals)

# ============================================================
# BACKGROUND TASK & FLASK
# ============================================================
def background_scanner():
    while True:
        try:
            run_scan()
        except Exception as e:
            logger.error(f"Scan failed: {e}")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

@app.route("/")
def home():
    return jsonify({
        "status": "active",
        "target": "Amazon SA Best Sellers & Movers",
        "min_discount": f"{MIN_DISCOUNT_PERCENT}%",
        "tracked_products": len(prices["product_id"].unique()) if not prices.empty else 0
    })

@app.route("/scan")
def manual_scan():
    count = run_scan()
    return jsonify({"status": "success", "deals_found": count})

if __name__ == "__main__":
    threading.Thread(target=background_scanner, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
