import os
import re
import json
import logging
import requests
import cloudscraper
import sqlite3
import time
import random
import hashlib
import threading
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Bot
from fake_useragent import UserAgent
from flask import Flask

# ========== الإعدادات العامة وتسجيل اللوج ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "432826122")
PORT = int(os.environ.get("PORT", 8080))

bot = Bot(token=TELEGRAM_BOT_TOKEN)
ua = UserAgent()

# ========== Flask App للتشغيل المستمر على Render ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Amazon Auto Deals Bot is Running!", 200

@app.route('/health')
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}, 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ========== إدارة قاعدة البيانات (سجل الأسعار والصفقات) ==========
DB_FILE = "deals_history.db"

def init_db():
    """إنشاء قاعدة البيانات لتتبع الأسعار والمنتجات المرسلة"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # جدول المنتجات المرسلة منعاً للتكرار
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_products (
            asin TEXT PRIMARY KEY,
            title TEXT,
            sent_at TIMESTAMP
        )
    ''')
    
    # جدول سجّل الأسعار لتحديد انخفاض السعر المفاجئ ومتوسط السعر
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_history (
            asin TEXT,
            price REAL,
            recorded_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

def is_already_sent(asin):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_products WHERE asin = ?", (asin,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_as_sent(asin, title):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO sent_products VALUES (?, ?, ?)", 
                   (asin, title, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def record_price_and_check_drop(asin, current_price):
    """تسجيل السعر والتحقق مما إذا كان السعر الحالي ينخفض بأكثر من 65% عن متوسط السعر السابق"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT price FROM price_history WHERE asin = ?", (asin,))
    prices = [row[0] for row in cursor.fetchall()]
    
    # إضافة السعر الحالي للسجل
    cursor.execute("INSERT INTO price_history VALUES (?, ?, ?)", 
                   (asin, current_price, datetime.now().isoformat()))
    conn.commit()
    
    if not prices:
        conn.close()
        return False, 0
    
    avg_price = sum(prices) / len(prices)
    conn.close()
    
    if avg_price > 0:
        price_drop_percent = ((avg_price - current_price) / avg_price) * 100
        # إذا كان الانخفاض عن المتوسط أكثر من 65% (مثال: من 4000 إلى 1400)
        if price_drop_percent >= 65:
            return True, round(price_drop_percent, 1)
            
    return False, 0

# ========== قائمة الأقسام (الأكثر مبيعا + Amazon Yalla) ==========
CATEGORIES = [
    # رابط Yalla / Everyday Deals المطلوب
    ("https://www.amazon.sa/-/en/tez/browse/?_encoding=UTF8&qcbrand=sAuWWBROaG&ref_=mwb_sn_logo_yalla", "🚀 Yalla Everyday Deals"),
    
    # أقسام الأكثر مبيعاً (Best Sellers)
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/kitchen", "🍳 Kitchen Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/home", "🏠 Home Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/mobile", "📱 Mobile Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/perfumes", "🌸 Perfumes Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/appliances", "⚡ Appliances Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/computers", "💻 Computers Best Seller"),
    ("https://www.amazon.sa/gp/bestsellers/watches", "⌚ Watches Best Seller"),
    
    # أقسام الخصومات والتصفية
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox Deals"),
    ("https://www.amazon.sa/outlet", "🎁 Outlet Clearance"),
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals")
]

# ========== أدوات الاستخراج والتحليل ==========
def create_session():
    session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=10
    )
    session.headers.update({
        'User-Agent': ua.random,
        'Accept-Language': 'ar-SA,ar;q=0.9,en-US;q=0.8',
        'Referer': 'https://www.amazon.sa/',
    })
    return session

def extract_asin(link):
    if not link:
        return None
    patterns = [r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})', r'product/([A-Z0-9]{10})']
    for p in patterns:
        match = re.search(p, link, re.I)
        if match:
            return match.group(1).upper()
    return None

def parse_item(item, category_name):
    try:
        # استخراج السعر الحالي
        price = None
        for sel in ['.a-price-whole', '.a-price .a-offscreen', '.a-price']:
            el = item.select_one(sel)
            if el:
                txt = el.text.replace(',', '').replace('ريال', '').replace('SAR', '').strip()
                match = re.search(r'[\d,]+\.?\d*', txt)
                if match:
                    price = float(match.group().replace(',', ''))
                    break
        
        if not price or price <= 0:
            return None

        # استخراج السعر القديم ونسبة الخصم
        old_price = 0
        discount = 0
        
        old_el = item.find('span', class_='a-text-price')
        if old_el:
            txt = old_el.get_text()
            match = re.search(r'[\d,]+\.?\d*', txt.replace(',', ''))
            if match:
                old_price = float(match.group())
                if old_price > price:
                    discount = int(((old_price - price) / old_price) * 100)

        # استخراج نسبة الخصم من الشارات إن وجدت
        if discount == 0:
            badge = item.find(string=re.compile(r'(\d+)%'))
            if badge:
                match = re.search(r'(\d+)', str(badge))
                if match:
                    discount = int(match.group())
                    if price > 0 and discount < 100:
                        old_price = price / (1 - discount/100)

        # استخراج العنوان والرابط
        title = "منتج مميز"
        for sel in ['h2 a span', 'h2 span', '.a-size-base-plus', '.p13n-sc-truncated', '.a-size-medium']:
            el = item.select_one(sel)
            if el:
                title = el.text.strip()
                break

        link = ""
        a = item.find('a', href=True)
        if a:
            href = a['href']
            if href.startswith('/'):
                link = f"https://www.amazon.sa{href}"
            elif 'amazon.sa' in href:
                link = href

        asin = extract_asin(link)
        if not asin:
            return None

        # استخراج الصورة
        img = ""
        el_img = item.select_one('img.s-image, img[src]')
        if el_img:
            img = el_img.get('src', '') or el_img.get('data-src', '')

        return {
            'asin': asin,
            'title': title,
            'price': price,
            'old_price': round(old_price, 2),
            'discount': discount,
            'link': link,
            'image': img,
            'category': category_name
        }
    except Exception as e:
        return None

# ========== المحرك الأساسي للمسح التلقائي ==========
def auto_scanner_loop():
    logger.info("🚀 Auto-scanner loop started...")
    session = create_session()

    while True:
        for url, cat_name in CATEGORIES:
            try:
                logger.info(f"🔍 Scanning category: {cat_name}")
                response = session.get(url, timeout=20)
                
                if response.status_code != 200:
                    time.sleep(3)
                    continue

                soup = BeautifulSoup(response.text, 'html.parser')
                
                # جمع العناصر
                items = []
                items.extend(soup.find_all('div', {'data-component-type': 's-search-result'}))
                items.extend(soup.find_all('li', class_='zg-item-immersion'))
                items.extend(soup.find_all('div', class_='p13n-sc-uncoverable-faceout'))
                items.extend(soup.find_all('div', {'data-testid': 'deal-card'}))

                for item in items:
                    deal = parse_item(item, cat_name)
                    if not deal:
                        continue

                    asin = deal['asin']

                    # التحقق مما إذا تم إرسال المنتج سابقاً
                    if is_already_sent(asin):
                        continue

                    # فحص انخفاض السعر التاريخي من قاعدة البيانات
                    is_price_drop, drop_percent = record_price_and_check_drop(asin, deal['price'])

                    # الشرط الرئيسي لإرسال العرض:
                    # 1. نسبة الخصم المباشرة 80% أو أكثر
                    # 2. أَوْ انخفاض مفاجئ وممتاز في السعر التاريخي للقطعة (أكثر من 65%)
                    should_send = (deal['discount'] >= 80) or is_price_drop

                    if should_send:
                        send_telegram_deal(deal, is_price_drop, drop_percent)
                        mark_as_sent(asin, deal['title'])
                        time.sleep(2) # مهلة بسيطة لتجنب السبام

                time.sleep(random.uniform(2, 5))

            except Exception as e:
                logger.error(f"Error scanning {cat_name}: {e}")
                time.sleep(5)

        logger.info("🔄 Finished one full scan cycle. Retrying in 60 seconds...")
        time.sleep(60)

def send_telegram_deal(deal, is_price_drop=False, drop_percent=0):
    """إرسال الصفقة فوراً إلى قناتك أو حسابك في تلجرام"""
    try:
        tag = "🔥 خصم خارق (فوق 80%)" if deal['discount'] >= 80 else f"📉 انخفاض سعر تاريخي ({drop_percent}%)"
        
        old_price_str = f"🏷️ *قبل:* {deal['old_price']:.2f} ريال\n" if deal['old_price'] > 0 else ""
        savings = (deal['old_price'] - deal['price']) if deal['old_price'] > deal['price'] else 0
        savings_str = f"💵 *التوفير:* {savings:.2f} ريال\n" if savings > 0 else ""

        caption = f"""
{tag}

📦 *{deal['title'][:120]}*

💵 *السعر الحالي:* {deal['price']:.2f} ريال
{old_price_str}{savings_str}📉 *نسبة الخصم:* {deal['discount']}%
📍 *القسم:* {deal['category']}

🔗 [اضغط هنا للشراء من أمازون]({deal['link']})
        """

        if deal['image'] and deal['image'].startswith('http'):
            bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=deal['image'], caption=caption, parse_mode='Markdown')
        else:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode='Markdown')

        logger.info(f"✅ Deal sent to Telegram: {deal['title'][:30]}")
    except Exception as e:
        logger.error(f"Failed to send deal to Telegram: {e}")

# ========== التشغيل الرئيسي ==========
if __name__ == "__main__":
    init_db()

    # تشغيل سيرفر Flask في Thread منفصل للحفاظ على نشاط البوت على Render
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # تشغيل محرك المسح التلقائي المباشر
    auto_scanner_loop()
