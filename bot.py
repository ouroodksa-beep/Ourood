import os
import re
import json
import logging
import requests
import cloudscraper
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from fake_useragent import UserAgent
import time
import random
import hashlib
import threading
from flask import Flask
from collections import deque
import pandas as pd

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "432826122")
PORT = int(os.environ.get("PORT", 8080))

# ========== Flask App for Keep-Alive ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!", 200

@app.route('/health')
def health():
    stats = page_rotator.get_stats() if page_rotator.all_pages else {}
    return {
        "status": "ok",
        "products": len(sent_products),
        "timestamp": datetime.now().isoformat(),
        "pages": stats.get('total_pages', 0),
        "visited": stats.get('visited_pages', 0),
        "progress": stats.get('progress_percent', 0),
        "rotation": stats.get('rotation_count', 0)
    }, 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

def keep_alive_ping():
    while True:
        try:
            time.sleep(600)
            logger.info("💓 Keep-alive ping")
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
            time.sleep(60)

ua = UserAgent()
sent_products = set()
sent_hashes = set()
price_history = {}

MIN_DISCOUNT = 70
MIN_RATING = 3.5

# ========== نظام تدوير الصفحات ==========
class PageRotationManager:
    def __init__(self):
        self.visited_pages = set()
        self.page_queue = deque()
        self.all_pages = []
        self.rotation_count = 0
        
    def load_state(self):
        try:
            if os.path.exists('page_rotation.json'):
                with open('page_rotation.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.visited_pages = set(data.get('visited', []))
                    self.rotation_count = data.get('rotation_count', 0)
        except Exception as e:
            logger.error(f"Error loading rotation state: {e}")
    
    def save_state(self):
        try:
            with open('page_rotation.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'visited': list(self.visited_pages),
                    'rotation_count': self.rotation_count,
                    'last_update': datetime.now().isoformat()
                }, f)
        except Exception as e:
            logger.error(f"Error saving rotation state: {e}")
    
    def generate_all_pages(self, categories):
        self.all_pages = []
        for base_url, cat_name, cat_type in categories:
            max_pages = PAGES_CONFIG.get(cat_type, 1)
            for page_num in range(1, max_pages + 1):
                page_url = self._build_page_url(base_url, page_num)
                page_id = f"{cat_name}_page{page_num}"
                self.all_pages.append({
                    'id': page_id,
                    'url': page_url,
                    'category': cat_name,
                    'type': cat_type,
                    'page_num': page_num,
                    'base_url': base_url
                })
        return self.all_pages
    
    def _build_page_url(self, base_url, page_num):
        if page_num == 1:
            return base_url
        separator = '&' if '?' in base_url else '?'
        return f"{base_url}{separator}page={page_num}" if 's?' in base_url else f"{base_url}{separator}pg={page_num}"
    
    def get_next_batch(self, batch_size=20):
        if not self.page_queue:
            self._refill_queue()
        
        available_pages = [p for p in self.page_queue if p['id'] not in self.visited_pages]
        if len(self.visited_pages) >= len(self.all_pages) * 0.9:
            self.visited_pages.clear()
            self.rotation_count += 1
            self._refill_queue()
            available_pages = list(self.page_queue)
        
        random.shuffle(available_pages)
        batch = available_pages[:batch_size]
        for page in batch:
            if page in self.page_queue:
                self.page_queue.remove(page)
            self.visited_pages.add(page['id'])
        self.save_state()
        return batch
    
    def _refill_queue(self):
        unvisited = [p for p in self.all_pages if p['id'] not in self.visited_pages]
        if not unvisited:
            unvisited = self.all_pages.copy()
            self.visited_pages.clear()
            self.rotation_count += 1
        random.shuffle(unvisited)
        self.page_queue = deque(unvisited)
    
    def get_stats(self):
        return {
            'total_pages': len(self.all_pages),
            'visited_pages': len(self.visited_pages),
            'progress_percent': (len(self.visited_pages) / len(self.all_pages) * 100) if self.all_pages else 0,
            'rotation_count': self.rotation_count
        }

page_rotator = PageRotationManager()

def load_database():
    global sent_products, sent_hashes, price_history
    try:
        if os.path.exists('bot_database.json'):
            with open('bot_database.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                sent_products = set(data.get('ids', []))
                sent_hashes = set(data.get('hashes', []))
                price_history = data.get('price_history', {})
    except Exception as e:
        logger.error(f"Error loading DB: {e}")

def save_database():
    try:
        with open('bot_database.json', 'w', encoding='utf-8') as f:
            json.dump({
                'ids': list(sent_products),
                'hashes': list(sent_hashes),
                'price_history': price_history
            }, f)
    except Exception as e:
        logger.error(f"Error saving DB: {e}")

def export_to_excel(deals):
    try:
        excel_file = 'amazon_deals.xlsx'
        df_new = pd.DataFrame(deals)
        cols_to_keep = ['title', 'price', 'old_price', 'discount', 'rating', 'reviews', 'category', 'type', 'link', 'id']
        df_new = df_new[[c for c in cols_to_keep if c in df_new.columns]]
        df_new['date_added'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if os.path.exists(excel_file):
            try:
                df_existing = pd.read_excel(excel_file)
                df_combined = pd.concat([df_existing, df_new], ignore_index=True)
                df_combined.drop_duplicates(subset=['id'], keep='last', inplace=True)
                df_combined.to_excel(excel_file, index=False)
            except Exception:
                df_new.to_excel(excel_file, index=False)
        else:
            df_new.to_excel(excel_file, index=False)
    except Exception as e:
        logger.error(f"Error exporting to Excel: {e}")

def extract_asin(link):
    if not link:
        return None
    patterns = [r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})', r'product/([A-Z0-9]{10})']
    for p in patterns:
        match = re.search(p, link, re.I)
        if match:
            return match.group(1).upper()
    return None

def create_title_hash(title):
    clean = re.sub(r'[^\w\s]', '', title.lower())
    clean = re.sub(r'\s+', ' ', clean).strip()
    clean = re.sub(r'\d+', '', clean)
    for word in ['amazon', 'saudi', 'ريال', 'sar', 'new', 'جديد', 'shipped', 'شحن']:
        clean = clean.replace(word, '')
    return hashlib.md5(clean[:30].strip().encode()).hexdigest()[:16]

def is_similar_product(title):
    new_hash = create_title_hash(title)
    if new_hash in sent_hashes:
        return True
    return False

def get_product_id(deal):
    asin = extract_asin(deal.get('link', ''))
    if asin:
        return f"ASIN_{asin}"
    key = f"{deal.get('title', '')}_{deal.get('price', 0)}"
    return f"HASH_{hashlib.md5(key.encode()).hexdigest()[:12]}"

def parse_rating(text):
    if not text:
        return 0
    match = re.search(r'(\d+\.?\d*)', str(text))
    return float(match.group(1)) if match else 0

def create_session():
    session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=10
    )
    session.headers.update({
        'User-Agent': ua.random,
        'Accept-Language': 'ar-SA,ar;q=0.9',
        'Referer': 'https://www.amazon.sa/',
    })
    return session

def fetch_page(session, url):
    for i in range(2):
        try:
            time.sleep(random.uniform(1, 2))
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
    return None

PAGES_CONFIG = {'best_sellers': 2, 'deals': 2, 'warehouse': 2, 'coupons': 2, 'search': 2, 'outlet': 2, 'prime': 2, 'lightning': 1, 'today': 2, 'clearance': 2, 'now': 2}

CATEGORIES_DEF = [
    ("https://www.amazon.sa/s?k=fresh&rh=p_8%3A70-99", "⚡ Amazon Now Fresh", 'now'),
    ("https://www.amazon.sa/s?k=amazon+now&rh=p_8%3A70-99", "⚡ Amazon Now Deals", 'now'),
    ("https://www.amazon.sa/s?k=groceries&rh=p_8%3A70-99", "🛒 Amazon Now Grocery", 'now'),
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox", 'deals'),
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals", 'warehouse'),
    ("https://www.amazon.sa/outlet", "🎁 Outlet", 'outlet'),
    ("https://www.amazon.sa/s?k=clearance&rh=p_8%3A70-99", "🔥 Clearance", 'clearance')
]

def update_price_history_and_check_resend(deal):
    deal_id = deal['id']
    curr_discount = deal['discount']
    
    if deal_id not in price_history:
        price_history[deal_id] = [curr_discount]
        return False
    
    price_history[deal_id].append(curr_discount)
    avg_discount = sum(price_history[deal_id]) / len(price_history[deal_id])
    return avg_discount <= 40

def is_valid_deal(deal):
    if deal['discount'] < MIN_DISCOUNT or deal['rating'] < MIN_RATING or deal['price'] <= 0:
        return False
    return True

def parse_item(item, category, is_best_seller):
    price = None
    for sel in ['.a-price-whole', '.a-price .a-offscreen', '.a-price']:
        el = item.select_one(sel)
        if el:
            try:
                txt = el.text.replace(',', '').replace('ريال', '').strip()
                match = re.search(r'[\d,]+\.?\d*', txt)
                if match:
                    price = float(match.group().replace(',', ''))
                    break
            except:
                pass
    if not price:
        return None

    old_price = 0
    discount = 0
    old_el = item.find('span', class_='a-text-price')
    if old_el:
        match = re.search(r'[\d,]+\.?\d*', old_el.get_text().replace(',', ''))
        if match:
            old_price = float(match.group())
            if old_price > price:
                discount = int(((old_price - price) / old_price) * 100)

    if discount == 0:
        badge = item.find(string=re.compile(r'(\d+)%'))
        if badge:
            match = re.search(r'(\d+)', str(badge))
            if match:
                discount = int(match.group())
                old_price = price / (1 - discount/100)

    title = "Unknown"
    for sel in ['h2 a span', 'h2 span', '.a-size-mini span', '.a-size-base-plus']:
        el = item.select_one(sel)
        if el and len(el.text.strip()) > 5:
            title = el.text.strip()
            break

    link = ""
    a = item.find('a', href=True)
    if a:
        href = a['href']
        link = f"https://www.amazon.sa{href}" if href.startswith('/') else href

    img = ""
    for sel in ['img.s-image', '.s-image']:
        el = item.select_one(sel)
        if el:
            img = el.get('src', '') or el.get('data-src', '')
            if img.startswith('http'):
                break

    rating, reviews = 0, 0
    rate_el = item.find('span', class_='a-icon-alt')
    if rate_el:
        rating = parse_rating(rate_el.text)

    rev_el = item.find('span', class_='a-size-base')
    if rev_el:
        match = re.search(r'[\d,]+', rev_el.text)
        if match:
            reviews = int(match.group().replace(',', ''))

    return {
        'title': title,
        'price': price,
        'old_price': round(old_price, 2),
        'discount': discount,
        'rating': rating,
        'reviews': reviews,
        'link': link,
        'image': img,
        'category': category,
        'is_best_seller': is_best_seller,
        'id': get_product_id({'title': title, 'link': link, 'price': price})
    }

def send_deal(bot, deal):
    global sent_products, sent_hashes
    
    deal_id = deal['id']
    should_resend = update_price_history_and_check_resend(deal)
    
    if (deal_id in sent_products or is_similar_product(deal['title'])) and not should_resend:
        return False

    deal_type = f"💰 {deal['discount']}%"
    if 'Warehouse' in deal['category']: deal_type = '🏭 WAREHOUSE'
    elif 'Amazon Now' in deal['category']: deal_type = '⚡ AMAZON NOW'
    elif deal['is_best_seller']: deal_type = '⭐ BEST SELLER'

    savings = round(deal['old_price'] - deal['price'], 2) if deal['old_price'] > 0 else 0
    sav_txt = f"💵 توفير: {savings:.2f} ريال\n" if savings > 0 else ""
    old_txt = f"🏷️ قبل: {deal['old_price']:.2f} ريال\n" if deal['old_price'] > 0 else ""
    
    msg = f"""
{deal_type} *🔥 عرض جديد!*

📦 {deal['title'][:120]}

💵 *{deal['price']:.2f} ريال*
{old_txt}{sav_txt}📉 خصم: {deal['discount']}%
⭐ تقييم: {deal['rating']}/5
📍 {deal['category']}

🔗 [عرض المنتج على Amazon]({deal['link']})
    """
    try:
        if deal['image'].startswith('http'):
            bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=deal['image'], caption=msg, parse_mode='Markdown')
        else:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown')

        sent_products.add(deal_id)
        sent_hashes.add(create_title_hash(deal['title']))
        save_database()
        export_to_excel([deal])
        return True
    except Exception as e:
        logger.error(f"Error sending deal: {e}")
        return False

def auto_scan_and_send(bot):
    """دالة الفحص التلقائي والمستمر في الخلفية"""
    session = create_session()
    
    if not page_rotator.all_pages:
        page_rotator.generate_all_pages(CATEGORIES_DEF)
        page_rotator.load_state()

    while True:
        try:
            pages = page_rotator.get_next_batch(batch_size=10)
            for page_info in pages:
                html = fetch_page(session, page_info['url'])
                if not html:
                    continue
                
                soup = BeautifulSoup(html, 'html.parser')
                items = soup.find_all('div', {'data-component-type': 's-search-result'})
                items.extend(soup.find_all('div', class_='s-result-item'))

                for item in items:
                    deal = parse_item(item, page_info['category'], 'best_sellers' in page_info['type'])
                    if deal and is_valid_deal(deal):
                        # إرسال فور العثور عليه
                        send_deal(bot, deal)
                
                time.sleep(random.uniform(2, 4))
            
            time.sleep(30)
        except Exception as e:
            logger.error(f"Error in auto scan loop: {e}")
            time.sleep(10)

def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🤖 البوت يعمل تلقائياً الآن بالخلفية ويسحب العروض مباشرة!")

def status_cmd(update: Update, context: CallbackContext):
    stats = page_rotator.get_stats()
    update.message.reply_text(f"📊 *حالة البوت:*\n\n📦 تم إرسال: {len(sent_products)} منتج\n📈 نسبة التدوير: {stats['progress_percent']:.1f}%\n🔄 دورات التدوير: {stats['rotation_count']}", parse_mode='Markdown')

def clear_cmd(update: Update, context: CallbackContext):
    sent_products.clear()
    sent_hashes.clear()
    save_database()
    update.message.reply_text("🗑️ تم مسح سجل الإرسال بنجاح!")

def main():
    load_database()
    
    page_rotator.generate_all_pages(CATEGORIES_DEF)
    page_rotator.load_state()
    
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # تشغيل الفحص الآلي المستمر في Thread مستقل فور تشغيل البوت
    threading.Thread(target=auto_scan_and_send, args=(updater.bot,), daemon=True).start()

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))

    logger.info("🤖 Telegram Bot ready & Scanning...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
