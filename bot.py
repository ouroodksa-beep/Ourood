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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8769441239:AAFUuBQxj-9q-xhYFGEW6yNWT2xWzvAA")
CHAT_ID = os.environ.get("CHAT_ID", "432826122")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "fb7742b2e62f3699d5059eea890268dd")

# 📌 الالتزام التام بنسبة 70% بدون أي تغيير
MIN_DISCOUNT_PERCENT = 70.0  
TARGET_DEALS_COUNT = 30
CHUNK_SIZE = 5  

is_scanning = False
scan_lock = threading.Lock()
sent_alerts = set()

amazon_start_page = 1
amazon_end_page = 5

# روابط أمازون السعودية المخصصة للتصفيات والخصومات الفائقة (70% فأكثر)
BASE_AMAZON_URLS = [
    "https://www.amazon.sa/s?i=electronics&rh=p_8%3A70-&fs=true",
    "https://www.amazon.sa/s?i=mobile-phones&rh=p_8%3A70-&fs=true",
    "https://www.amazon.sa/s?i=kitchen&rh=p_8%3A70-&fs=true",
    "https://www.amazon.sa/s?i=beauty&rh=p_8%3A70-&fs=true",
    "https://www.amazon.sa/s?i=fashion&rh=p_8%3A70-&fs=true",
    "https://www.amazon.sa/s?i=appliances&rh=p_8%3A70-&fs=true"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("amazon_strict_70")
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

def fetch_anti_bot(target_url):
    payload = {
        'api_key': SCRAPER_API_KEY, 
        'url': target_url, 
        'country_code': 'sa',
        'keep_headers': 'true'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept-Language': 'ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    try:
        resp = session.get('http://api.scraperapi.com', params=payload, headers=headers, timeout=25)
        if resp.status_code == 200 and len(resp.text) > 8000:
            return resp.text
    except Exception as e:
        logger.error(f"Fetch Error: {e}")
    return None

def parse_and_send_amazon_deals(html):
    if not html: return 0, 0
    soup = BeautifulSoup(html, "lxml")
    
    cards = soup.select("div[data-component-type='s-search-result']")
    total_cards = len(cards)
    deals_sent = 0

    for card in cards:
        try:
            name_tag = card.select_one("h2 a span, span.a-size-base-plus, span.a-size-medium")
            link_tag = card.select_one("h2 a.a-link-normal, a.a-link-normal[href*='/dp/']")

            if not (name_tag and link_tag):
                continue

            name = name_tag.get_text(strip=True)[:100]
            raw_url = link_tag.get("href", "")

            # 1. استخراج كل عناصر الأسعار الممكنة داخل الكارت
            all_price_texts = [p.get_text(strip=True) for p in card.select("span.a-offscreen")]
            
            price = None
            old_price = None
            
            parsed_prices = [parse_price(p) for p in all_price_texts if parse_price(p) is not None]
            parsed_prices = sorted(list(set(parsed_prices)))  # ترتيب الأرقام تصاعدياً

            if len(parsed_prices) >= 2:
                price = parsed_prices[0]
                old_price = parsed_prices[-1]
            elif len(parsed_prices) == 1:
                price = parsed_prices[0]

            # 2. حساب نسبة الخصم الفعلية
            discount = 0.0
            if price and old_price and old_price > price:
                discount = ((old_price - price) / old_price) * 100

            # 3. التحقق الاحتياطي من وجود شارة الخصم الصريحة (مثل: خصم 70% أو 75%-)
            if discount < MIN_DISCOUNT_PERCENT:
                card_text = card.get_text()
                match_disc = re.search(r"(خصم\s*(\d+)%|(\d+)%\s*off|-(\d+)%)", card_text, re.I)
                if match_disc:
                    found_disc = float(next(g for g in match_disc.groups() if g and g.isdigit()))
                    if found_disc >= MIN_DISCOUNT_PERCENT:
                        discount = found_disc
                        if price and not old_price:
                            old_price = round(price / (1 - (discount / 100)), 2)

            # 🎯 تفعيل الشرط الصارم: 70% أو أكثر فقط
            if discount >= MIN_DISCOUNT_PERCENT and price:
                clean_url = "https://www.amazon.sa" + raw_url if raw_url.startswith("/") else raw_url.split("?")[0]
                asin = card.get("data-asin", "") or hashlib.md5(clean_url.encode()).hexdigest()[:10]

                alert_key = f"{asin}_{price}"
                if alert_key not in sent_alerts:
                    sent_alerts.add(alert_key)
                    deals_sent += 1

                    msg = (
                        f"💥 <b>صيدة أمازون قوية (خصم {round(discount)}%)!</b> 💥\n\n"
                        f"🛍 <b>المنتج:</b> {name}\n"
                        f"💰 <b>السعر بعد الخصم:</b> {price} ر.س\n"
                        f"📈 <b>السعر قبل الخصم:</b> {old_price if old_price else 'غير محدد'} ر.س\n\n"
                        f"🔗 <b>رابط الشراء المباشر:</b>\n{clean_url}"
                    )
                    telegram_send(msg)
                    time.sleep(0.3)
        except Exception:
            continue
            
    return total_cards, deals_sent

def run_until_target_found():
    global is_scanning, amazon_start_page, amazon_end_page
    if is_scanning: return

    with scan_lock: is_scanning = True
    telegram_send("🚀 <b>بدأ فحص أمازون المتقدم (بالالتزام الكامل بنسبة خصم 70%+)...</b>")

    total_deals_found = 0
    rounds_count = 0

    while total_deals_found < TARGET_DEALS_COUNT:
        rounds_count += 1

        target_urls = []
        for p in range(amazon_start_page, amazon_end_page + 1):
            for u in BASE_AMAZON_URLS:
                delimiter = "&" if "?" in u else "?"
                target_urls.append(f"{u}{delimiter}page={p}")

        with ThreadPoolExecutor(max_workers=2) as executor:
            html_results = list(executor.map(fetch_anti_bot, target_urls))

        round_found = 0
        total_cards_scanned = 0
        valid_pages_count = 0

        for html in html_results:
            if html:
                valid_pages_count += 1
                cards_cnt, deals_cnt = parse_and_send_amazon_deals(html)
                total_cards_scanned += cards_cnt
                round_found += deals_cnt

        total_deals_found += round_found
        
        telegram_send(
            f"📊 <b>تقرير الجولة {rounds_count}:</b>\n"
            f"• الصفحات المجلوبة: {valid_pages_count} من {len(target_urls)}\n"
            f"• إجمالي المنتجات المفحوصة: {total_cards_scanned}\n"
            f"• الصيدات المطابقة لشرط (70%+): {round_found}\n"
            f"• الإجمالي الكلي حتى الآن: {total_deals_found}/{TARGET_DEALS_COUNT}"
        )

        amazon_start_page = amazon_end_page + 1
        amazon_end_page = amazon_start_page + CHUNK_SIZE - 1

        if amazon_start_page > 30:
            amazon_start_page = 1
            amazon_end_page = 5

        if total_deals_found < TARGET_DEALS_COUNT:
            time.sleep(2)

    telegram_send(f"🎉 <b>تم الوصول للهدف!</b> تم إرسال {total_deals_found} صيدة بخصم 70%+.")
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
    return jsonify({"status": "amazon_strict_70_active", "start": amazon_start_page, "end": amazon_end_page})

if __name__ == "__main__":
    telegram_send("🚀 <b>تم تشغيل البوت المخصص للخصومات الفائقة (70%+)! أرسلي /scan للبدء.</b>")
    threading.Thread(target=telegram_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
