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
# SETTINGS (HARDCODED FOR TESTING)
# ============================================================
BOT_TOKEN = "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA"
CHAT_ID = "432826122"

GLITCH_DROP = 50.0
MIN_HISTORY = 2
MAX_PRODUCTS = 300
REQUEST_DELAY = 3.0
SCAN_INTERVAL_MINUTES = 60
PRICE_FILE = "amazon_sa_prices.csv"
ALERT_FILE = "amazon_sa_alerts.csv"
SELF_PING_INTERVAL = 600

# Amazon SA headers
AMAZON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("amazon_sa_bot")

# ============================================================
# FLASK
# ============================================================
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
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            logger.error(f"Telegram failed: {r.status_code} - {r.text[:200]}")
        return ok
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
                if len(prices) > 5000:
                    prices = prices.tail(5000)
        except Exception as e:
            logger.error(f"Load error: {e}")
            prices = pd.DataFrame(columns=PRICE_COLUMNS)
    else:
        prices = pd.DataFrame(columns=PRICE_COLUMNS)

    if os.path.exists(ALERT_FILE):
        try:
            alert_df = pd.read_csv(ALERT_FILE)
            sent_alerts = set(alert_df["alert_id"].astype(str).tolist())
        except Exception as e:
            logger.error(f"Alert load error: {e}")
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
# FETCH METHODS
# ============================================================
def fetch_direct(url, retries=2):
    """
    Direct fetch مع Amazon headers + proxy rotation
    """
    for attempt in range(retries + 1):
        try:
            logger.info(f"Direct fetch (attempt {attempt + 1}): {url}")
            
            # Rotate User-Agent
            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            ]
            headers = AMAZON_HEADERS.copy()
            headers["User-Agent"] = user_agents[attempt % len(user_agents)]
            
            resp = session.get(url, headers=headers, timeout=30)
            logger.info(f"Status: {resp.status_code} | Length: {len(resp.text)}")
            
            if resp.status_code == 200:
                # Check if blocked (captcha/robot check)
                if "captcha" in resp.text.lower() or "robot" in resp.text.lower():
                    logger.warning("Amazon blocked with captcha")
                    if attempt < retries:
                        time.sleep(5)
                        continue
                    return None
                return resp.text
            
            if resp.status_code == 503:  # Service unavailable
                logger.warning("Amazon 503 - bot detected")
                if attempt < retries:
                    time.sleep(10)
                    continue
            
            if attempt < retries:
                time.sleep(5)
                continue
                
        except Exception as e:
            logger.warning(f"Direct fetch error: {e}")
            if attempt < retries:
                time.sleep(3)
                continue
    return None

def fetch_amazon_html(url, retries=2):
    return fetch_direct(url, retries)

# ============================================================
# EXTRACT PRODUCTS FROM AMAZON HTML
# ============================================================
def extract_products_from_html(html, source_url):
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Method 1: Amazon search results (data-component-type)
    try:
        cards = soup.find_all("div", attrs={"data-component-type": "s-search-result"})
        logger.info(f"Amazon cards found: {len(cards)}")

        for card in cards:
            try:
                # Product name
                name = None
                for selector in [
                    "h2 a span",
                    "h2 span",
                    ".a-size-base-plus",
                    ".a-size-medium",
                    "[data-cy='title-recipe-title']",
                ]:
                    tag = card.select_one(selector)
                    if tag:
                        name = tag.get_text(strip=True)
                        if name and len(name) > 3:
                            break

                # Price
                price = None
                for selector in [
                    ".a-price .a-offscreen",
                    ".a-price-whole",
                    "[data-cy='price-recipe']",
                    ".a-price-range",
                ]:
                    tag = card.select_one(selector)
                    if tag:
                        price_text = tag.get_text(strip=True)
                        price = parse_price(price_text)
                        if price and price > 0:
                            break

                # URL
                link_tag = card.select_one("h2 a")
                url = "https://www.amazon.sa" + link_tag["href"] if link_tag and link_tag.get("href") else source_url
                
                # Image (optional)
                img_tag = card.select_one("img")
                image = img_tag["src"] if img_tag else None

                if name and price and price > 0:
                    products.append({
                        "product": str(name).strip()[:200],
                        "url": url.split("?")[0],  # Clean URL
                        "price": price,
                        "image": image
                    })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Card extraction error: {e}")

    # Method 2: Alternative selectors
    try:
        alt_cards = soup.find_all("div", class_=re.compile(r"s-result-item"))
        logger.info(f"Alt cards found: {len(alt_cards)}")

        for card in alt_cards:
            if card.get("data-component-type") == "s-search-result":
                continue
            
            try:
                name_tag = card.select_one("h2 span, .a-size-base-plus, .a-size-medium")
                price_tag = card.select_one(".a-price .a-offscreen, .a-price-whole")
                link_tag = card.select_one("h2 a")
                
                if name_tag and price_tag and link_tag:
                    name = name_tag.get_text(strip=True)
                    price = parse_price(price_tag.get_text(strip=True))
                    url = "https://www.amazon.sa" + link_tag["href"]
                    
                    if name and price and price > 0:
                        products.append({
                            "product": str(name).strip()[:200],
                            "url": url.split("?")[0],
                            "price": price,
                            "image": None
                        })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Alt extraction error: {e}")

    # Method 3: JSON-LD structured data
    try:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    items = data
                else:
                    items = [data]
                
                for item in items:
                    if item.get("@type") == "Product":
                        name = item.get("name")
                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        
                        price = None
                        if isinstance(offers, dict):
                            price = parse_price(offers.get("price"))
                        
                        url = item.get("url", "")
                        
                        if name and price and price > 0:
                            products.append({
                                "product": str(name).strip()[:200],
                                "url": url.split("?")[0] if url else source_url,
                                "price": price,
                                "image": item.get("image")
                            })
            except Exception:
                pass
    except Exception:
        pass

    # Deduplicate by URL
    unique = {}
    for p in products:
        key = p["url"]
        if key not in unique:
            unique[key] = p

    result = list(unique.values())
    logger.info(f"Total unique products: {len(result)}")
    return result

# ============================================================
# PRICE PARSER (supports SAR)
# ============================================================
def parse_price(value):
    if value is None:
        return None
    value = str(value)
    # Remove currency symbols and text
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
        elif value.count(".") > 1:
            value = value.replace(".", "")
        return float(value)
    except Exception:
        return None

# ============================================================
# PRODUCT ID
# ============================================================
def make_product_id(product):
    # Use Amazon ASIN if available
    asin_match = re.search(r"/dp/([A-Z0-9]{10})", product["url"])
    if asin_match:
        return asin_match.group(1)
    
    raw = product["product"] + "|" + product["url"]
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]

# ============================================================
# DISCOVER PRODUCTS
# ============================================================
DISCOVERY_URLS = [
    "https://www.amazon.sa/s?k=electronics",
    "https://www.amazon.sa/s?k=phones",
    "https://www.amazon.sa/s?k=laptops",
    "https://www.amazon.sa/s?k=headphones",
    "https://www.amazon.sa/s?k=smart+watch",
    "https://www.amazon.sa/s?k=tablet",
    "https://www.amazon.sa/s?k=camera",
    "https://www.amazon.sa/s?k=gaming",
]

def discover_products():
    discovered = {}
    logger.info("=" * 60)
    logger.info("AMAZON SA AUTO DISCOVERY")
    logger.info("=" * 60)

    for source_url in DISCOVERY_URLS:
        if len(discovered) >= MAX_PRODUCTS:
            break

        logger.info(f"Opening: {source_url}")
        html = fetch_amazon_html(source_url)

        if not html:
            logger.warning(f"Failed to fetch: {source_url}")
            continue

        found = extract_products_from_html(html, source_url)
        logger.info(f"Products found: {len(found)}")

        for product in found:
            if len(discovered) >= MAX_PRODUCTS:
                break
            product_id = make_product_id(product)
            product["product_id"] = product_id
            discovered[product_id] = product

        time.sleep(REQUEST_DELAY)

    return list(discovered.values())

# ============================================================
# ADD PRICE
# ============================================================
def add_price(product):
    global prices
    new_row = pd.DataFrame([{
        "product_id": product["product_id"],
        "product": product["product"],
        "url": product["url"],
        "price": float(product["price"]),
        "timestamp": datetime.now()
    }])
    prices = pd.concat([prices, new_row], ignore_index=True)

# ============================================================
# CALCULATE GLITCH
# ============================================================
def calculate_glitch(product_id):
    data = prices[prices["product_id"] == product_id].copy()
    data = data.sort_values("timestamp")

    if len(data) < (MIN_HISTORY + 1):
        return None

    old_prices = data["price"].iloc[:-1].astype(float)
    current_price = float(data["price"].iloc[-1])
    reference_price = float(old_prices.median())

    if reference_price <= 0:
        return None

    drop = ((reference_price - current_price) / reference_price) * 100

    if drop >= 90:
        score = 100
    elif drop >= 80:
        score = 95
    elif drop >= 70:
        score = 85
    elif drop >= 60:
        score = 75
    elif drop >= 50:
        score = 65
    else:
        score = 30

    return {
        "product_id": product_id,
        "product": data["product"].iloc[-1],
        "url": data["url"].iloc[-1],
        "current_price": current_price,
        "reference_price": reference_price,
        "drop": round(drop, 2),
        "score": score,
        "history_count": len(data)
    }

# ============================================================
# ALERT ID
# ============================================================
def make_alert_id(result):
    raw = result["product_id"] + "|" + str(result["current_price"]) + "|" + str(result["drop"])
    return hashlib.md5(raw.encode()).hexdigest()

# ============================================================
# MESSAGE (Arabic)
# ============================================================
def create_alert_message(result):
    return (
        "🚨 <b>AMAZON SAUDI GLITCH</b> 🚨\n\n"
        "🛍 المنتج:\n" + str(result["product"]) + "\n\n"
        "💰 السعر الحالي: " + str(result["current_price"]) + " ر.س\n"
        "📊 السعر المرجعي: " + str(round(result["reference_price"], 2)) + " ر.س\n"
        "🔻 نسبة الخصم: " + str(result["drop"]) + "%\n"
        "🔥 Glitch Score: " + str(result["score"]) + "/100\n\n"
        "📚 عدد القراءات: " + str(result["history_count"]) + "\n\n"
        "🔗 " + str(result["url"])
    )

# ============================================================
# GLITCH SCAN
# ============================================================
def scan_glitches():
    glitches = []
    logger.info("=" * 60)
    logger.info("GLITCH SCAN")
    logger.info("=" * 60)

    product_ids = prices["product_id"].drop_duplicates().tolist()
    logger.info(f"Checking {len(product_ids)} products...")

    for product_id in product_ids:
        result = calculate_glitch(product_id)
        if result is None:
            continue
        if result["drop"] <= GLITCH_DROP:
            continue
        if result["score"] < 65:
            continue

        alert_id = make_alert_id(result)
        if alert_id in sent_alerts:
            continue

        logger.info("=" * 40)
        logger.info("🚨 GLITCH FOUND!")
        logger.info(f"Product: {result['product'][:80]}")
        logger.info(f"Current: {result['current_price']} | Ref: {result['reference_price']} | Drop: {result['drop']}%")

        message = create_alert_message(result)
        if telegram_send(message):
            sent_alerts.add(alert_id)
            glitches.append(result)
            logger.info("✅ Alert sent!")

    return glitches

# ============================================================
# COMPLETE SCAN
# ============================================================
def run_scan():
    global prices
    logger.info("=" * 60)
    logger.info("AMAZON SAUDI PRICE SCAN")
    logger.info("=" * 60)

    telegram_send(
        "✅ <b>Amazon Saudi Glitch Hunter</b>\n"
        "Mode: Direct Fetch\n"
        f"Scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    products = discover_products()
    logger.info(f"Products discovered: {len(products)}")

    if not products:
        logger.warning("No products found.")
        telegram_send(
            "⚠️ No products found.\n"
            "Amazon may be blocking requests."
        )
        return []

    for product in products:
        add_price(product)

    save_database()
    logger.info(f"Prices collected: {len(products)}")
    logger.info(f"Total DB rows: {len(prices)}")

    glitches = scan_glitches()
    save_database()

    logger.info("=" * 60)
    logger.info("SCAN COMPLETED")
    logger.info(f"Prices: {len(products)} | Glitches: {len(glitches)}")
    logger.info("=" * 60)

    if not glitches:
        telegram_send(
            f"✅ Scan complete.\n"
            f"Products: {len(products)}\n"
            f"No glitches > {GLITCH_DROP}% found."
        )

    return glitches

# ============================================================
# BACKGROUND SCANNER
# ============================================================
last_scan_result = {"status": "idle", "last_run": None, "products": 0, "glitches": 0}

def background_scanner():
    global last_scan_result
    while True:
        try:
            logger.info(f"Starting scan (every {SCAN_INTERVAL_MINUTES} min)...")
            glitches = run_scan()
            last_scan_result = {
                "status": "success",
                "last_run": datetime.now().isoformat(),
                "products": len(prices),
                "glitches": len(glitches)
            }
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            last_scan_result = {
                "status": "error",
                "last_run": datetime.now().isoformat(),
                "error": str(e)
            }
            telegram_send(f"❌ Scan Error: {str(e)[:500]}")
            time.sleep(300)
            continue

        logger.info(f"Sleeping {SCAN_INTERVAL_MINUTES} minutes...")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

# ============================================================
# KEEP ALIVE
# ============================================================
def self_ping():
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL", "")
            if not url:
                url = "http://localhost:5000"
            resp = session.get(url.rstrip("/") + "/ping", timeout=10)
            logger.info(f"Self-ping: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
        time.sleep(SELF_PING_INTERVAL)

# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def home():
    return jsonify({
        "bot": "Amazon Saudi Glitch Hunter",
        "status": "running",
        "mode": "direct-fetch",
        "last_scan": last_scan_result,
        "scan_interval_minutes": SCAN_INTERVAL_MINUTES,
        "glitch_threshold": GLITCH_DROP,
        "total_products_in_db": len(prices["product_id"].unique()) if not prices.empty else 0
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/ping")
def ping():
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()})

@app.route("/scan")
def manual_scan():
    try:
        glitches = run_scan()
        return jsonify({"status": "success", "glitches_found": len(glitches), "glitches": glitches})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/test-telegram")
def test_telegram():
    ok = telegram_send("🧪 <b>Test message from Amazon Bot</b>\nToken works!")
    return jsonify({"telegram_sent": ok})

@app.route("/test-fetch")
def test_fetch():
    url = "https://www.amazon.sa/s?k=electronics"
    html = fetch_amazon_html(url)
    if html:
        products = extract_products_from_html(html, url)
        return jsonify({
            "success": True,
            "html_length": len(html),
            "products_found": len(products),
            "sample": products[:3] if products else []
        })
    return jsonify({"success": False, "error": "Failed to fetch"}), 500

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    threading.Thread(target=background_scanner, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
