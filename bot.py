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

MIN_DISCOUNT = 70  # حد الخصم الأدنى 70%

# ========== نظام تدوير الصفحات الشامل ==========
class PageRotationManager:
    def __init__(self):
        self.visited_pages = set()
        self.page_queue_amazon = deque()
        self.page_queue_now = deque()
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
            max_pages = PAGES_CONFIG.get(cat_type, 3)
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
        self._refill_queues()
        return self.all_pages
    
    def _build_page_url(self, base_url, page_num):
        if page_num == 1:
            return base_url
        separator = '&' if '?' in base_url else '?'
        return f"{base_url}{separator}page={page_num}" if 's?' in base_url else f"{base_url}{separator}pg={page_num}"
    
    def _refill_queues(self):
        amazon_pages = [p for p in self.all_pages if not p['type'].startswith('now')]
        now_pages = [p for p in self.all_pages if p['type'].startswith('now')]
        
        random.shuffle(amazon_pages)
        random.shuffle(now_pages)
        
        self.page_queue_amazon = deque(amazon_pages)
        self.page_queue_now = deque(now_pages)

    def get_balanced_batch(self, batch_size=10):
        """تجهيز دفعة متوازنة بالنصف بين أمازون العادي وأمازون ناو"""
        batch = []
        half = batch_size // 2
        
        # أخذ صفحات من أمازون العادي
        for _ in range(half):
            if not self.page_queue_amazon:
                self._refill_queues()
            if self.page_queue_amazon:
                batch.append(self.page_queue_amazon.popleft())

        # أخذ صفحات من أمازون ناو
        for _ in range(half):
            if not self.page_queue_now:
                self._refill_queues()
            if self.page_queue_now:
                batch.append(self.page_queue_now.popleft())

        return batch
    
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
        cols_to_keep = ['title', 'price', 'old_price', 'discount', 'category', 'type', 'link', 'id']
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

def create_session():
    session = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
        delay=5
    )
    session.headers.update({
        'User-Agent': ua.random,
        'Accept-Language': 'ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': 'https://www.amazon.sa/',
    })
    return session

def fetch_page(session, url):
    for i in range(2):
        try:
            time.sleep(random.uniform(1, 2))
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
    return None

# ========== إعدادات عدد الصفحات لكل نوع قسم ==========
PAGES_CONFIG = {
    'best_sellers': 3,
    'deals': 4,
    'warehouse': 3,
    'outlet': 3,
    'clearance': 4,
    'now': 5,
    'now_grocery': 5,
    'now_supermarket': 5,
    'now_fruits': 4,
    'now_vegetables': 4,
    'now_meat': 4,
    'now_dairy': 4,
    'now_bakery': 4,
    'now_frozen': 4,
    'now_drinks': 4,
    'now_snacks': 4,
    'now_baby': 4,
    'now_pet_food': 4,
    'now_cleaning': 4,
    'now_personal_care': 4,
    'now_daily_deals': 5,
}

CATEGORIES_DEF = [
    # ==== Amazon Now Fresh & Grocery ====
    ("https://www.amazon.sa/s?k=fresh&rh=p_8%3A70-", "⚡ Amazon Now Fresh", 'now'),
    ("https://www.amazon.sa/s?k=amazon+now&rh=p_8%3A70-", "⚡ Amazon Now Deals", 'now_daily_deals'),
    ("https://www.amazon.sa/s?k=supermarket&rh=p_8%3A70-", "🛒 Supermarket Deals", 'now_supermarket'),
    ("https://www.amazon.sa/s?k=groceries&rh=p_8%3A70-", "🛒 Groceries 70% Off", 'now_grocery'),
    ("https://www.amazon.sa/s?k=fruits&rh=p_8%3A70-", "🍎 Fruits & Vegetables", 'now_fruits'),
    ("https://www.amazon.sa/s?k=dairy&rh=p_8%3A70-", "🥛 Dairy & Eggs", 'now_dairy'),
    ("https://www.amazon.sa/s?k=cleaning&rh=p_8%3A70-", "🧼 Cleaning Products", 'now_cleaning'),
    ("https://www.amazon.sa/s?k=personal+care&rh=p_8%3A70-", "🧴 Personal Care", 'now_personal_care'),

    # ==== عروض أمازون العامة ====
    ("https://www.amazon.sa/s?rh=p_8%3A70-99", "🔥 All Deals 70% Off", 'deals'),
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox Today Deals", 'deals'),
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/grocery", "🥫 Grocery Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/home", "🏠 Home Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals", 'warehouse'),
    ("https://www.amazon.sa/outlet", "🎁 Outlet Store", 'outlet'),
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
    if deal['discount'] < MIN_DISCOUNT or deal['price'] <= 0 or deal['old_price'] <= deal['price']:
        return False
    return True

def parse_item(item, category, is_best_seller):
    price = None
    price_el = item.select_one('.a-price .a-offscreen') or item.select_one('.a-price-whole') or item.select_one('.a-price')
    if price_el:
        try:
            txt = price_el.text.replace(',', '').replace('ريال', '').replace('SAR', '').strip()
            match = re.search(r'[\d.]+', txt)
            if match:
                price = float(match.group())
        except Exception:
            pass

    if not price or price <= 0:
        return None

    old_price = 0
    discount = 0

    old_el = item.select_one('span.a-text-price span.a-offscreen') or item.select_one('span.a-text-price') or item.select_one('.a-possibility-price')
    if old_el:
        try:
            txt = old_el.text.replace(',', '').replace('ريال', '').replace('SAR', '').strip()
            match = re.search(r'[\d.]+', txt)
            if match:
                val = float(match.group())
                if val > price:
                    old_price = val
                    discount = int(((old_price - price) / old_price) * 100)
        except Exception:
            pass

    if discount == 0:
        badge = item.find(string=re.compile(r'خصم\s*(\d+)%|(\d+)%\s*off', re.I))
        if badge:
            try:
                match = re.search(r'(\d+)', str(badge))
                if match:
                    discount = int(match.group(1))
                    if 0 < discount < 100:
                        old_price = round(price / (1 - (discount / 100)), 2)
            except Exception:
                pass

    if discount < MIN_DISCOUNT or old_price <= price:
        return None

    title = ""
    for sel in ['h2 a span', 'h2 span', '.a-size-base-plus', '.a-size-medium', '.a-size-mini span']:
        el = item.select_one(sel)
        if el and len(el.text.strip()) > 5:
            title = el.text.strip()
            break

    if not title:
        return None

    link = ""
    a = item.find('a', href=True)
    if a:
        href = a['href']
        link = f"https://www.amazon.sa{href}" if href.startswith('/') else href

    return {
        'title': title,
        'price': price,
        'old_price': round(old_price, 2),
        'discount': discount,
        'link': link,
        'category': category,
        'is_best_seller': is_best_seller,
        'id': get_product_id({'title': title, 'link': link, 'price': price})
    }

def send_deal(bot, deal, target_chat_id=None):
    global sent_products, sent_hashes
    
    chat_id = target_chat_id or TELEGRAM_CHAT_ID
    deal_id = deal['id']
    should_resend = update_price_history_and_check_resend(deal)
    
    if (deal_id in sent_products or is_similar_product(deal['title'])) and not should_resend and not target_chat_id:
        return False

    deal_type = f"💰 {deal['discount']}%"
    if 'Warehouse' in deal['category']: deal_type = '🏭 WAREHOUSE'
    elif 'Amazon Now' in deal['category'] or 'Now ' in deal['category'] or 'Fresh' in deal['category'] or 'Grocery' in deal['category'] or 'Supermarket' in deal['category']:
        deal_type = '⚡ AMAZON NOW'
    elif deal['is_best_seller']: deal_type = '⭐ BEST SELLER'

    savings = round(deal['old_price'] - deal['price'], 2)
    sav_txt = f"💵 توفير: {savings:.2f} ريال\n" if savings > 0 else ""
    old_txt = f"🏷️ قبل: {deal['old_price']:.2f} ريال\n" if deal['old_price'] > 0 else ""
    
    msg = f"""
{deal_type} *🔥 عرض جديد!*

📦 {deal['title'][:120]}

💵 *{deal['price']:.2f} ريال*
{old_txt}{sav_txt}📉 خصم: {deal['discount']}%
📍 {deal['category']}

🔗 [عرض المنتج على Amazon]({deal['link']})
    """
    try:
        bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

        sent_products.add(deal_id)
        sent_hashes.add(create_title_hash(deal['title']))
        save_database()
        export_to_excel([deal])
        logger.info(f"✅ Sent Deal: {deal['title'][:30]} - Discount: {deal['discount']}%")
        return True
    except Exception as e:
        logger.error(f"Error sending deal: {e}")
        return False

def scan_batch_and_send(bot, target_chat_id=None, limit=10):
    """دالة تقوم بفحص دفعة واحدة متوازنة من أمازون العادي وأمازون ناو"""
    session = create_session()
    pages = page_rotator.get_balanced_batch(batch_size=limit)
    found_count = 0

    for page_info in pages:
        html = fetch_page(session, page_info['url'])
        if not html:
            continue
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.find_all('div', {'data-component-type': 's-search-result'})
        if not items:
            items = soup.find_all('div', class_='s-result-item')
        if not items:
            items = soup.find_all('li', class_='zg-item-immersion')

        for item in items:
            deal = parse_item(item, page_info['category'], 'best_sellers' in page_info['type'])
            if deal and is_valid_deal(deal):
                if send_deal(bot, deal, target_chat_id=target_chat_id):
                    found_count += 1
        
        time.sleep(random.uniform(1, 2))
    return found_count

def auto_scan_and_send(bot):
    """دالة الفحص التلقائي المستمر طوال اليوم"""
    if not page_rotator.all_pages:
        page_rotator.generate_all_pages(CATEGORIES_DEF)
        page_rotator.load_state()

    while True:
        try:
            scan_batch_and_send(bot)
            time.sleep(15)
        except Exception as e:
            logger.error(f"Error in auto scan loop: {e}")
            time.sleep(10)

# ========== معالجة الأوامر والرسائل النصية ==========
def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🤖 أهلاً بك! البوت يعمل 24 ساعة للفحص التلقائي.\n\n💬 ابعت لي **\"هاي\"** في أي وقت وهبدأ أبحث لكِ فوراً عن أقوى العروض الحالية من أمازون وأمازون ناو!")

def status_cmd(update: Update, context: CallbackContext):
    stats = page_rotator.get_stats()
    update.message.reply_text(f"📊 *حالة البوت:*\n\n📦 تم إرسال: {len(sent_products)} منتج\n📈 نسبة التدوير: {stats['progress_percent']:.1f}%\n🔄 دورات التدوير: {stats['rotation_count']}", parse_mode='Markdown')

def clear_cmd(update: Update, context: CallbackContext):
    sent_products.clear()
    sent_hashes.clear()
    save_database()
    update.message.reply_text("🗑️ تم مسح سجل الإرسال بنجاح!")

def handle_text_messages(update: Update, context: CallbackContext):
    """الرد الفوري عند إرسال 'هاي' أو أي كلمة للبحث عن العروض"""
    user_text = update.message.text.strip().lower()
    chat_id = update.message.chat_id

    update.message.reply_text("🔎 أهلاً بك! جاري البحث فوراً عن أحدث العروض من أمازون وأمازون ناو بالتوازي... ⏳")
    
    # تشغيل عملية فحص سريعة ومخصصة للرد على المستخدم
    found = scan_batch_and_send(context.bot, target_chat_id=chat_id, limit=8)
    
    if found == 0:
        update.message.reply_text("👍 جاري استكمال الفحص، إذا ظهرت عروض جديدة بخصم أكثر من 70% سأرسلها لكِ فوراً!")

def main():
    load_database()
    
    page_rotator.generate_all_pages(CATEGORIES_DEF)
    page_rotator.load_state()
    
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # تشغيل الفحص المستمر طوال اليوم في الخفاء
    threading.Thread(target=auto_scan_and_send, args=(updater.bot,), daemon=True).start()

    # تسجيل الأوامر والرسائل
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    
    # معالج الرسائل العادية (استجابة لكلمة هاي أو أي رسالة)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text_messages))

    logger.info("🤖 Telegram Bot ready & Scanning...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
