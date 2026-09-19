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

RESEND_COOLDOWN_DAYS = 7  # إعادة إرسال نفس المنتج بعد أسبوع فقط

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
        "products_sent": len(sent_log),
        "timestamp": datetime.now().isoformat(),
        "pages": stats.get('total_pages', 0),
        "visited": stats.get('visited_pages', 0),
        "remaining": stats.get('remaining_pages', 0),
        "progress": stats.get('progress_percent', 0)
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
sent_log = {}   # deal_id -> تاريخ آخر إرسال (منع الإعادة لأسبوع)
hash_log = {}   # title_hash -> تاريخ آخر إرسال

MIN_DISCOUNT = 70  # حد الخصم الأدنى 70%

# ========== نظام تدوير الصفحات الشامل (كل صفحة مرة واحدة فقط) ==========
class PageRotationManager:
    def __init__(self):
        self.visited_pages = {}   # page_id -> timestamp الزيارة
        self.page_queue_amazon = deque()
        self.page_queue_now = deque()
        self.all_pages = []
        self.rotation_count = 0

    def load_state(self):
        try:
            if os.path.exists('page_rotation.json'):
                with open('page_rotation.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    v = data.get('visited', {})
                    if isinstance(v, list):  # توافق مع الصيغة القديمة
                        self.visited_pages = {p: datetime.now().isoformat() for p in v}
                    else:
                        self.visited_pages = v
                    self.rotation_count = data.get('rotation_count', 0)
        except Exception as e:
            logger.error(f"Error loading rotation state: {e}")

    def save_state(self):
        try:
            with open('page_rotation.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'visited': self.visited_pages,
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
        logger.info(f"📚 Generated {len(self.all_pages)} pages from {len(categories)} categories")
        return self.all_pages

    def _build_page_url(self, base_url, page_num):
        if page_num == 1:
            return base_url
        separator = '&' if '?' in base_url else '?'
        return f"{base_url}{separator}page={page_num}" if 's?' in base_url else f"{base_url}{separator}pg={page_num}"

    def _refill_queues(self):
        # عبّي الصفوف بالصفحات اللي لسه متزارتش فقط
        amazon_pages = [p for p in self.all_pages if not p['type'].startswith('now') and p['id'] not in self.visited_pages]
        now_pages = [p for p in self.all_pages if p['type'].startswith('now') and p['id'] not in self.visited_pages]

        random.shuffle(amazon_pages)
        random.shuffle(now_pages)

        self.page_queue_amazon = deque(amazon_pages)
        self.page_queue_now = deque(now_pages)

    def has_unvisited(self):
        return any(p['id'] not in self.visited_pages for p in self.all_pages)

    def get_balanced_batch(self, batch_size=10):
        """دفعة متوازنة من صفحات جديدة تماماً (مش متزورة قبل كده)"""
        batch = []
        half = batch_size // 2

        for _ in range(half):
            while self.page_queue_amazon:
                page = self.page_queue_amazon.popleft()
                if page['id'] not in self.visited_pages:
                    batch.append(page)
                    break

        for _ in range(half):
            while self.page_queue_now:
                page = self.page_queue_now.popleft()
                if page['id'] not in self.visited_pages:
                    batch.append(page)
                    break

        return batch

    def mark_visited(self, page_id):
        """تسجيل الصفحة كمتزورة - مش هنرجع لها تاني"""
        self.visited_pages[page_id] = datetime.now().isoformat()
        self.save_state()

    def reset_old_visits(self):
        """إعادة فتح الصفحات اللي فات عليها أسبوع أو أكتر"""
        now = datetime.now()
        try:
            old = [pid for pid, ts in self.visited_pages.items()
                   if (now - datetime.fromisoformat(ts)).days >= RESEND_COOLDOWN_DAYS]
        except Exception:
            old = list(self.visited_pages.keys())
        for pid in old:
            del self.visited_pages[pid]
        if old:
            self.rotation_count += 1
            self._refill_queues()
            self.save_state()
            logger.info(f"🔄 Weekly reset: reopened {len(old)} pages")
        return len(old)

    def get_stats(self):
        visited = len(self.visited_pages)
        total = len(self.all_pages)
        return {
            'total_pages': total,
            'visited_pages': visited,
            'remaining_pages': max(0, total - visited),
            'progress_percent': (visited / total * 100) if total else 0,
            'rotation_count': self.rotation_count
        }

page_rotator = PageRotationManager()

def load_database():
    global sent_products, sent_hashes, sent_log, hash_log
    try:
        if os.path.exists('bot_database.json'):
            with open('bot_database.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                sent_log = data.get('sent_log', {})
                hash_log = data.get('hash_log', {})
                # توافق مع النسخة القديمة (بدون تواريخ)
                if not sent_log and data.get('ids'):
                    sent_log = {i: datetime.now().isoformat() for i in data.get('ids', [])}
                if not hash_log and data.get('hashes'):
                    hash_log = {h: datetime.now().isoformat() for h in data.get('hashes', [])}
                sent_products = set(sent_log.keys())
                sent_hashes = set(hash_log.keys())
    except Exception as e:
        logger.error(f"Error loading DB: {e}")

def save_database():
    try:
        with open('bot_database.json', 'w', encoding='utf-8') as f:
            json.dump({
                'sent_log': sent_log,
                'hash_log': hash_log
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

def get_product_id(deal):
    asin = extract_asin(deal.get('link', ''))
    if asin:
        return f"ASIN_{asin}"
    key = f"{deal.get('title', '')}_{deal.get('price', 0)}"
    return f"HASH_{hashlib.md5(key.encode()).hexdigest()[:12]}"

def is_on_cooldown(deal_id, title):
    """ممنوع إعادة الإرسال إلا بعد أسبوع (7 أيام)"""
    now = datetime.now()
    ts = sent_log.get(deal_id)
    if ts:
        try:
            if (now - datetime.fromisoformat(ts)).days < RESEND_COOLDOWN_DAYS:
                return True
        except Exception:
            pass
    h = create_title_hash(title)
    hts = hash_log.get(h)
    if hts:
        try:
            if (now - datetime.fromisoformat(hts)).days < RESEND_COOLDOWN_DAYS:
                return True
        except Exception:
            pass
    return False

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
    'now_grocery': 4,
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
    'now_breakfast': 4,
    'now_pantry': 4,
    'dept': 4,        # الأقسام العامة الجديدة
}

CATEGORIES_DEF = [
    # ==== Amazon Now Fresh & Grocery ====
    ("https://www.amazon.sa/s?k=fresh&rh=p_8%3A70-", "⚡ Amazon Now Fresh", 'now'),
    ("https://www.amazon.sa/s?k=amazon+now&rh=p_8%3A70-", "⚡ Amazon Now Deals", 'now_daily_deals'),
    ("https://www.amazon.sa/s?k=supermarket&rh=p_8%3A70-", "🛒 Supermarket Deals", 'now_supermarket'),
    ("https://www.amazon.sa/s?k=groceries&rh=p_8%3A70-", "🛒 Groceries 70% Off", 'now_grocery'),
    ("https://www.amazon.sa/s?k=fruits&rh=p_8%3A70-", "🍎 Fruits", 'now_fruits'),
    ("https://www.amazon.sa/s?k=vegetables&rh=p_8%3A70-", "🥬 Vegetables", 'now_vegetables'),
    ("https://www.amazon.sa/s?k=meat&rh=p_8%3A70-", "🥩 Meat & Poultry", 'now_meat'),
    ("https://www.amazon.sa/s?k=dairy&rh=p_8%3A70-", "🥛 Dairy & Eggs", 'now_dairy'),
    ("https://www.amazon.sa/s?k=bakery&rh=p_8%3A70-", "🍞 Bakery", 'now_bakery'),
    ("https://www.amazon.sa/s?k=frozen&rh=p_8%3A70-", "🧊 Frozen Food", 'now_frozen'),
    ("https://www.amazon.sa/s?k=drinks&rh=p_8%3A70-", "🥤 Drinks & Beverages", 'now_drinks'),
    ("https://www.amazon.sa/s?k=snacks&rh=p_8%3A70-", "🍿 Snacks", 'now_snacks'),
    ("https://www.amazon.sa/s?k=baby+food&rh=p_8%3A70-", "👶 Baby Food", 'now_baby'),
    ("https://www.amazon.sa/s?k=pet+food&rh=p_8%3A70-", "🐾 Pet Food", 'now_pet_food'),
    ("https://www.amazon.sa/s?k=cleaning&rh=p_8%3A70-", "🧼 Cleaning Products", 'now_cleaning'),
    ("https://www.amazon.sa/s?k=personal+care&rh=p_8%3A70-", "🧴 Personal Care", 'now_personal_care'),
    ("https://www.amazon.sa/s?k=breakfast&rh=p_8%3A70-", "🥣 Breakfast & Cereal", 'now_breakfast'),
    ("https://www.amazon.sa/s?k=rice+and+pasta&rh=p_8%3A70-", "🍚 Rice & Pasta", 'now_pantry'),

    # ==== عروض أمازون العامة ====
    ("https://www.amazon.sa/s?rh=p_8%3A70-99", "🔥 All Deals 70% Off", 'deals'),
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox Today Deals", 'deals'),
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals", 'warehouse'),
    ("https://www.amazon.sa/outlet", "🎁 Outlet Store", 'outlet'),

    # ==== Best Sellers ====
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/grocery", "🥫 Grocery Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/home", "🏠 Home Best Seller", 'best_sellers'),

    # ==== أقسام جديدة (خصم 70%+) ====
    ("https://www.amazon.sa/s?k=toys&rh=p_8%3A70-", "🧸 Toys & Games", 'dept'),
    ("https://www.amazon.sa/s?k=sports&rh=p_8%3A70-", "⚽ Sports & Outdoors", 'dept'),
    ("https://www.amazon.sa/s?k=kitchen&rh=p_8%3A70-", "🍳 Kitchen & Dining", 'dept'),
    ("https://www.amazon.sa/s?k=tools&rh=p_8%3A70-", "🔧 Tools & DIY", 'dept'),
    ("https://www.amazon.sa/s?k=car+accessories&rh=p_8%3A70-", "🚗 Car Accessories", 'dept'),
    ("https://www.amazon.sa/s?k=baby+products&rh=p_8%3A70-", "🍼 Baby Products", 'dept'),
    ("https://www.amazon.sa/s?k=pet+supplies&rh=p_8%3A70-", "🐕 Pet Supplies", 'dept'),
    ("https://www.amazon.sa/s?k=office+supplies&rh=p_8%3A70-", "📎 Office Supplies", 'dept'),
    ("https://www.amazon.sa/s?k=perfume&rh=p_8%3A70-", "🌸 Perfumes 70% Off", 'dept'),
    ("https://www.amazon.sa/s?k=watches&rh=p_8%3A70-", "⌚ Watches 70% Off", 'dept'),
    ("https://www.amazon.sa/s?k=phone+accessories&rh=p_8%3A70-", "📱 Phone Accessories", 'dept'),
    ("https://www.amazon.sa/s?k=gaming&rh=p_8%3A70-", "🎮 Gaming Deals", 'dept'),
    ("https://www.amazon.sa/s?k=home+appliances&rh=p_8%3A70-", "🔌 Home Appliances", 'dept'),
    ("https://www.amazon.sa/s?k=home+improvement&rh=p_8%3A70-", "🏡 Home Improvement", 'dept'),
    ("https://www.amazon.sa/s?k=hair+care&rh=p_8%3A70-", "💇 Hair Care", 'dept'),
    ("https://www.amazon.sa/s?k=skin+care&rh=p_8%3A70-", "✨ Skin Care", 'dept'),
]

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

    # ممنوع إعادة الإرسال قبل مرور أسبوع
    if is_on_cooldown(deal_id, deal['title']):
        return False

    deal_type = f"💰 {deal['discount']}%"
    if 'Warehouse' in deal['category']: deal_type = '🏭 WAREHOUSE'
    elif 'Amazon Now' in deal['category'] or 'Now ' in deal['category'] or 'Fresh' in deal['category'] or 'Grocery' in deal['category'] or 'Supermarket' in deal['category'] or 'Fruits' in deal['category'] or 'Vegetables' in deal['category'] or 'Meat' in deal['category'] or 'Dairy' in deal['category'] or 'Bakery' in deal['category'] or 'Frozen' in deal['category'] or 'Drinks' in deal['category'] or 'Snacks' in deal['category'] or 'Breakfast' in deal['category'] or 'Rice' in deal['category'] or 'Baby Food' in deal['category'] or 'Pet Food' in deal['category']:
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

        now_iso = datetime.now().isoformat()
        h = create_title_hash(deal['title'])
        sent_products.add(deal_id)
        sent_hashes.add(h)
        sent_log[deal_id] = now_iso      # تسجيل تاريخ الإرسال (لمنع الإعادة لأسبوع)
        hash_log[h] = now_iso
        save_database()
        export_to_excel([deal])          # تسجيل في الشيت مرة واحدة
        logger.info(f"✅ Sent Deal: {deal['title'][:30]} - Discount: {deal['discount']}%")
        return True
    except Exception as e:
        logger.error(f"Error sending deal: {e}")
        return False

def scan_batch_and_send(bot, target_chat_id=None, limit=10):
    """فحص دفعة واحدة من صفحات جديدة تماماً (مش متزورة قبل كده)"""
    session = create_session()
    pages = page_rotator.get_balanced_batch(batch_size=limit)
    found_count = 0

    if not pages:
        return 0

    for page_info in pages:
        html = fetch_page(session, page_info['url'])
        page_rotator.mark_visited(page_info['id'])  # الصفحة دي خلاص اتزارت، مش هنرجعلها

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
    """الفحص التلقائي المستمر - كل صفحة مرة واحدة فقط، وبعد أسبوع بيتحدث كل حاجة"""
    if not page_rotator.all_pages:
        page_rotator.generate_all_pages(CATEGORIES_DEF)
        page_rotator.load_state()

    while True:
        try:
            if not page_rotator.has_unvisited():
                # كل الصفحات اتفحصت - استنى الأسبوع يعدي وبعدين افتحها من تاني
                page_rotator.reset_old_visits()
                if not page_rotator.has_unvisited():
                    logger.info("✅ All pages visited. Waiting for weekly reset...")
                    time.sleep(3600)  # كل ساعة بيشيك لو الأسبوع عدى
                    continue

            scan_batch_and_send(bot)
            time.sleep(15)
        except Exception as e:
            logger.error(f"Error in auto scan loop: {e}")
            time.sleep(10)

# ========== معالجة الأوامر والرسائل النصية ==========
def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🤖 أهلاً بك! البوت يعمل 24 ساعة للفحص التلقائي.\n\n💬 ابعت لي **\"هاي\"** في أي وقت وهبحث لك في صفحات جديدة تماماً عن أقوى العروض من أمازون وأمازون ناو!\n\n⏳ كل منتج بيترسل مرة واحدة بس ومبيترجعش إلا بعد أسبوع.")

def status_cmd(update: Update, context: CallbackContext):
    stats = page_rotator.get_stats()
    update.message.reply_text(
        f"📊 *حالة البوت:*\n\n"
        f"📦 منتجات مبعوتة: {len(sent_log)}\n"
        f"🚫 إعادة الإرسال بعد: {RESEND_COOLDOWN_DAYS} أيام\n"
        f"📄 إجمالي الصفحات: {stats['total_pages']}\n"
        f"✅ صفحات متفحوصة: {stats['visited_pages']}\n"
        f"🔜 صفحات متبقية: {stats['remaining_pages']}\n"
        f"📈 نسبة الفحص: {stats['progress_percent']:.1f}%",
        parse_mode='Markdown'
    )

def clear_cmd(update: Update, context: CallbackContext):
    sent_products.clear()
    sent_hashes.clear()
    sent_log.clear()
    hash_log.clear()
    page_rotator.visited_pages.clear()
    page_rotator._refill_queues()
    page_rotator.save_state()
    save_database()
    update.message.reply_text("🗑️ تم مسح كل السجلات! هيبدأ فحص جديد من الأول.")

def handle_text_messages(update: Update, context: CallbackContext):
    """الرد الفوري - كل بحث في صفحات جديدة تماماً"""
    chat_id = update.message.chat_id

    # لو فيه صفحات قديمة فات عليها أسبوع، افتحها من تاني
    page_rotator.reset_old_visits()

    if not page_rotator.has_unvisited():
        update.message.reply_text("✅ تم فحص جميع الصفحات حالياً! ⏳ الفحص بيتجدد تلقائياً بعد أسبوع من آخر زيارة لكل صفحة.")
        return

    update.message.reply_text("🔎 جاري البحث فوراً في صفحات جديدة تماماً من أمازون وأمازون ناو بالتوازي... ⏳")

    found = scan_batch_and_send(context.bot, target_chat_id=chat_id, limit=8)

    if found == 0:
        update.message.reply_text("👍 تم فحص الدفعة الحالية، مفيش عروض جديدة بخصم 70%+ دلوقتي. ابعتلي تاني وهفحصلك صفحات تانية جديدة!")

def main():
    load_database()

    page_rotator.generate_all_pages(CATEGORIES_DEF)
    page_rotator.load_state()

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    threading.Thread(target=auto_scan_and_send, args=(updater.bot,), daemon=True).start()

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text_messages))

    logger.info("🤖 Telegram Bot ready & Scanning...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
