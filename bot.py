import os
import re
import sqlite3
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.ext import Updater
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
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "fb7742b2e62f3699d5059eea890268dd")
PORT = int(os.environ.get("PORT", 8080))

SUPER_DISCOUNT_THRESHOLD = 70.0  # إرسال لو الخصم 50% أو أكثر
AVERAGE_DISCOUNT_THRESHOLD = 40.0 # إرسال لو انخفض 40% عن المتوسط

# ========== Flask Server ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Amazon SA Deals Monitor Bot is running!", 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ========== SQLite Database setup ==========
DB_NAME = "amazon_tracker.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            asin TEXT PRIMARY KEY,
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
            asin TEXT PRIMARY KEY,
            sent_price REAL,
            sent_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def update_product_and_check(deal):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    asin = deal['asin']
    price = deal['price']
    orig_price = deal['old_price']
    
    cursor.execute("SELECT current_price, avg_price, price_count FROM products WHERE asin = ?", (asin,))
    row = cursor.fetchone()
    
    should_send = False
    reason = ""
    
    if row is None:
        avg_price = price
        price_count = 1
        cursor.execute('''
            INSERT INTO products (asin, title, category, current_price, original_price, avg_price, price_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (asin, deal['title'], deal['category'], price, orig_price, avg_price, price_count, datetime.now()))
        
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
            WHERE asin = ?
        ''', (price, orig_price, new_avg, new_count, datetime.now(), asin))
        
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
        cursor.execute("SELECT sent_price FROM sent_log WHERE asin = ?", (asin,))
        sent = cursor.fetchone()
        if sent and abs(sent[0] - price) < 1.0:
            should_send = False
        else:
            cursor.execute("INSERT OR REPLACE INTO sent_log (asin, sent_price, sent_at) VALUES (?, ?, ?)",
                           (asin, price, datetime.now()))

    conn.commit()
    conn.close()
    return should_send, reason

# ========== Amazon Custom Links Setup ==========
CATEGORIES_AMAZON = [
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=12462934031&ref_=cct_cg_aeappsbc_5a1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "💄 Beauty Section"),
    ("https://www.amazon.sa/-/en/s?_encoding=UTF8&i=fashion&k=bags&ref_=cct_cg_aeappsbc_4f1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "👜 Fashion Bags"),
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=12463618031&ref_=cct_cg_aeappsbc_5b1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "🌸 Perfumes Category"),
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=12463276031&ref_=cct_cg_aeappsbc_4e1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "🍳 Kitchen Category"),
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=21023214031&ref_=cct_cg_aeappsbc_4d1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "🏠 Home Appliances"),
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=20509033031&ref_=cct_cg_aeappsbc_3a1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "💻 Computers Category"),
    ("https://www.amazon.sa/fmc/global-store?_encoding=UTF8&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF&ref_=cct_cg_aeappsbc_2c1", "🌐 Global Store"),
    ("https://www.amazon.sa/deals?_encoding=UTF8&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF&ref_=cct_cg_aeappsbc_2b1&bubble-id=deals-collection-coupons", "🎟️ Coupons Collection"),
    ("https://www.amazon.sa/-/en/gp/goldbox?ie=UTF8&ref_=cct_cg_aeappsbc_2a1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "⚡ Today's Goldbox Deals"),
    ("https://www.amazon.sa/-/en/b?_encoding=UTF8&node=12462991031&ref_=cct_cg_aeappsbc_3b1&pf_rd_p=5bca21fd-7cc6-43e6-948c-8c49601e6be9&pf_rd_r=BA46M7Q8N5YER6QMP1HF", "📱 Mobile Category"),
    ("https://www.amazon.sa/-/en/gp/browse.html?node=26031445031&ref_=navm_em_allpf_outlet_0_1_1_11", "🏷️ Outlet Clearance"),
    ("https://www.amazon.sa/-/en/gp/bestsellers/?ref_=navm_em_cs_bestsellers_0_1_1_2", "🏆 Best Sellers Main"),
    ("https://www.amazon.sa/-/en/deals/?_encoding=UTF8&ref_=sa_cat_halo_bau_deals_D", "🔥 Halo Deals"),
    ("https://www.amazon.sa/-/en/tez/browse/?_encoding=UTF8&qcbrand=sAuWWBROaG&ref_=sa_cat_halo_now", "🚀 Amazon Yalla / Now"),
    ("https://www.amazon.sa/gp/bestsellers/supermarket/", "🛒 Supermarket Best Sellers")
]

def fetch_amazon_category(target_url, category_name):
    deals = []
    
    if SCRAPER_API_KEY and SCRAPER_API_KEY != "ضع_مفتاح_SCRAPERAPI_هنا":
        api_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={target_url}&country_code=sa"
    else:
        api_url = target_url

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        r = requests.get(api_url, headers=headers, timeout=60)
        
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.find_all('div', {'data-component-type': 's-search-result'}) or soup.find_all('div', id=re.compile(r'grid-item'))
            
            if not items:
                items = soup.find_all('div', class_=re.compile(r'a-cardui|p13n-grid-content|a-section'))

            for item in items:
                try:
                    asin = item.get('data-asin')
                    link_tag = item.find('a', class_=re.compile(r'a-link-normal'), href=True)
                    if not link_tag:
                        continue
                    
                    href = link_tag['href']
                    if not asin:
                        asin_match = re.search(r'/dp/([A-Z0-9]{10})', href)
                        asin = asin_match.group(1) if asin_match else hashlib.md5(href.encode()).hexdigest()[:10]

                    link = f"https://www.amazon.sa{href}" if href.startswith('/') else href

                    title_tag = item.find('h2') or item.find('span', class_=re.compile(r'a-text-normal|p13n-sc-truncate'))
                    title = title_tag.text.strip() if title_tag else "منتج أمازون"

                    price_whole = item.find('span', class_='a-price-whole')
                    price_fraction = item.find('span', class_='a-price-fraction')
                    
                    if not price_whole:
                        continue
                        
                    p_str = price_whole.text.replace(',', '').strip()
                    if price_fraction:
                        p_str += "." + price_fraction.text.strip()
                    price = float(re.sub(r'[^\d.]', '', p_str))

                    old_price_tag = item.find('span', class_='a-price a-text-price') or item.find('span', class_='a-offscreen')
                    old_price = price
                    if old_price_tag:
                        try:
                            old_p_str = re.sub(r'[^\d.]', '', old_price_tag.text.replace(',', ''))
                            if old_p_str:
                                float_old = float(old_p_str)
                                if float_old > price:
                                    old_price = float_old
                        except:
                            old_price = price

                    discount = 0
                    if old_price > price:
                        discount = int(((old_price - price) / old_price) * 100)

                    img_tag = item.find('img', class_=re.compile(r's-image|a-dynamic-image'))
                    img = img_tag['src'] if img_tag and 'src' in img_tag.attrs else ""

                    deals.append({
                        'asin': asin,
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
        logger.error(f"Error fetching Amazon category {category_name}: {e}")
        
    return deals

def send_telegram_alert(bot, deal, reason):
    savings = deal['old_price'] - deal['price']
    msg = f"""
🚨 *تنبيه صيدة جديدة من أمازون السعودية!*

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
    logger.info("⚡ Auto Monitor loop started for Amazon SA...")
    
    while True:
        try:
            for base_url, category_name in CATEGORIES_AMAZON:
                logger.info(f"Scanning category: {category_name}")
                deals = fetch_amazon_category(base_url, category_name)
                
                for deal in deals:
                    should_send, reason = update_product_and_check(deal)
                    if should_send:
                        logger.info(f"🎯 Alert triggered for {deal['title']}")
                        send_telegram_alert(bot, deal, reason)
                        time.sleep(2)
                
                time.sleep(random.uniform(5, 10))
                
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
    
    logger.info("🤖 Bot initialized and monitoring Amazon SA active!")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
