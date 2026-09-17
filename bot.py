import os
import re
import json
import sqlite3
import logging
import requests
import cloudscraper
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from fake_useragent import UserAgent
import time
import random
import hashlib
import threading
from flask import Flask

# ========== Logging Setup ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== Configurations ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "432826122")
PORT = int(os.environ.get("PORT", 8080))

# تعديل الشروط لتسهيل العثور على العروض وإرسالها
SUPER_DISCOUNT_THRESHOLD = 50.0  # إرسال فوراً إذا كان الخصم 50% أو أكثر
AVERAGE_DISCOUNT_THRESHOLD = 40.0 # إرسال إذا نزل السعر 40% عن متوسط السعر المسجل

# ========== Flask Server ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Noon Deals Monitor Bot is running!", 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ========== SQLite Database setup ==========
DB_NAME = "noon_tracker.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            category TEXT,
            current_price REAL,
            original_price REAL,
            avg_price REAL,
            price_count INTEGER,
            last_updated DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_log (
            sku TEXT PRIMARY KEY,
            sent_price REAL,
            sent_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def update_product_and_check(deal):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    sku = deal['sku']
    price = deal['price']
    orig_price = deal['old_price']
    
    cursor.execute("SELECT current_price, avg_price, price_count FROM products WHERE sku = ?", (sku,))
    row = cursor.fetchone()
    
    should_send = False
    reason = ""
    
    if row is None:
        avg_price = price
        price_count = 1
        cursor.execute('''
            INSERT INTO products (sku, title, category, current_price, original_price, avg_price, price_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sku, deal['title'], deal['category'], price, orig_price, avg_price, price_count, datetime.now()))
        
        if deal['discount'] >= SUPER_DISCOUNT_THRESHOLD:
            should_send = True
            reason = f"🔥 خصم مميز مباشر ({deal['discount']}% )"
            
    else:
        old_avg = row[1]
        count = row[2]
        
        new_count = count + 1
        new_avg = ((old_avg * count) + price) / new_count
        
        cursor.execute('''
            UPDATE products 
            SET current_price = ?, original_price = ?, avg_price = ?, price_count = ?, last_updated = ?
            WHERE sku = ?
        ''', (price, orig_price, new_avg, new_count, datetime.now(), sku))
        
        drop_from_avg = 0
        if new_avg > 0:
            drop_from_avg = int(((new_avg - price) / new_avg) * 100)
            
        if deal['discount'] >= SUPER_DISCOUNT_THRESHOLD:
            should_send = True
            reason = f"🔥 خصم متجاوز {SUPER_DISCOUNT_THRESHOLD}% ({deal['discount']}%)"
        elif drop_from_avg >= AVERAGE_DISCOUNT_THRESHOLD:
            should_send = True
            reason = f"📉 انخفاض ممتاز عن المتوسط بنسبة {drop_from_avg}% (المتوسط: {new_avg:.1f} ريال)"

    if should_send:
        cursor.execute("SELECT sent_price FROM sent_log WHERE sku = ?", (sku,))
        sent = cursor.fetchone()
        if sent and abs(sent[0] - price) < 1.0:
            should_send = False
        else:
            cursor.execute("INSERT OR REPLACE INTO sent_log (sku, sent_price, sent_at) VALUES (?, ?, ?)",
                           (sku, price, datetime.now()))

    conn.commit()
    conn.close()
    
    return should_send, reason

# ========== Scraper Setup ==========
ua = UserAgent()

CATEGORIES_NOON = [
    ("https://www.noon.com/saudi-en/electronics-and-mobile/", "📱 Beauty Best Seller"),
    ("https://www.noon.com/saudi-en/home-and-kitchen/", "🍳 Kitchen Best Seller"),
    ("https://www.noon.com/saudi-en/home-and-kitchen/", "🏠 Home Best Seller"),
    ("https://www.noon.com/saudi-en/electronics-and-mobile/", "📱 Mobile Best Seller"),
    ("https://www.noon.com/saudi-en/beauty-and-fragrances/", "🌸 Perfumes Best Seller"),
    ("https://www.noon.com/saudi-en/electronics-and-mobile/", "⚡ Appliances Best Seller"),
    ("https://www.noon.com/saudi-en/electronics-and-mobile/", "💻 Computers Best Seller"),
    ("https://www.noon.com/saudi-en/fashion/", "⌚ Watches Best Seller")
]

def fetch_noon_category(session, base_url, category_name):
    deals = []
    url = f"{base_url}?sort%5Bby%5D=is_bestseller&sort%5Bdir%5D=desc&limit=50"
    
    try:
        headers = {
            'User-Agent': ua.random,
            'Accept-Language': 'ar-SA,ar;q=0.9,en-US;q=0.8',
            'Referer': 'https://www.noon.com/saudi-ar/',
        }
        r = session.get(url, headers=headers, timeout=20)
        
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            product_cards = soup.find_all('span', class_=re.compile(r'productContainer|wrapper'))
            
            for card in product_cards:
                try:
                    link_tag = card.find('a', href=True)
                    if not link_tag:
                        continue
                    
                    href = link_tag['href']
                    sku_match = re.search(r'/N\d+A/', href) or re.search(r'/([A-Z0-9]+)/p', href)
                    sku = sku_match.group(1) if sku_match else hashlib.md5(href.encode()).hexdigest()[:10]
                    
                    link = f"https://www.noon.com{href}" if href.startswith('/') else href
                    
                    title_tag = card.find('div', {'data-qa': 'product_name'}) or card.find('strong')
                    title = title_tag.text.strip() if title_tag else "منتج نون"
                    
                    price_tag = card.find('strong', class_=re.compile(r'amount'))
                    if not price_tag:
                        continue
                    price = float(re.sub(r'[^\d.]', '', price_tag.text.strip()))
                    
                    old_price_tag = card.find('span', class_=re.compile(r'oldPrice|old_price'))
                    old_price = price
                    if old_price_tag:
                        try:
                            old_price = float(re.sub(r'[^\d.]', '', old_price_tag.text.strip()))
                        except:
                            old_price = price

                    discount = 0
                    if old_price > price:
                        discount = int(((old_price - price) / old_price) * 100)

                    img_tag = card.find('img')
                    img = img_tag['src'] if img_tag and 'src' in img_tag.attrs else ""

                    deals.append({
                        'sku': sku,
                        'title': title,
                        'price': price,
                        'old_price': old_price,
                        'discount': discount,
                        'link': link,
                        'image': img,
                        'category': category_name
                    })
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"Error fetching Noon category {category_name}: {e}")
        
    return deals

def send_telegram_alert(bot, deal, reason):
    savings = deal['old_price'] - deal['price']
    msg = f"""
🚨 *تنبيه صيدة جديدة من نون (Noon)!*

📌 *السبب:* {reason}

📦 *المنتج:* {deal['title'][:120]}

💵 *السعر الحالي:* {deal['price']:.2f} ريال
🏷️ *السعر الأصلي:* {deal['old_price']:.2f} ريال
💥 *التوفير:* {savings:.2f} ريال ({deal['discount']}% خصم)

📍 *القسم:* {deal['category']}

🔗 [اضغط هنا للرابط والشراء المباشر]({deal['link']})
    """
    try:
        if deal['image'] and deal['image'].startswith('http'):
            bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=deal['image'], caption=msg, parse_mode='Markdown')
        else:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send alert to Telegram: {e}")

# ========== Automated Background Scanner Loop ==========
def auto_monitor_loop(bot):
    logger.info("⚡ Auto Monitor loop started...")
    session = cloudscraper.create_scraper()
    
    while True:
        try:
            for base_url, category_name in CATEGORIES_NOON:
                logger.info(f"Scanning category: 💄 {category_name}")
                deals = fetch_noon_category(session, base_url, category_name)
                
                for deal in deals:
                    should_send, reason = update_product_and_check(deal)
                    
                    if should_send:
                        logger.info(f"🎯 Alert triggered for {deal['title']}")
                        send_telegram_alert(bot, deal, reason)
                        time.sleep(2)
                
                time.sleep(random.uniform(3, 6))
                
            logger.info("Finished one full scan cycle. Retrying in 60 seconds...")
            time.sleep(60)
                
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")
            time.sleep(60)

# ========== Main Application ==========
def main():
    init_db()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    bot = updater.bot
    
    monitor_thread = threading.Thread(target=auto_monitor_loop, args=(bot,), daemon=True)
    monitor_thread.start()
    
    logger.info("🤖 Bot initialized and monitoring active!")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
