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
        return self.all_pages
    
    def _build_page_url(self, base_url, page_num):
        if page_num == 1:
            return base_url
        separator = '&' if '?' in base_url else '?'
        return f"{base_url}{separator}page={page_num}" if 's?' in base_url else f"{base_url}{separator}pg={page_num}"
    
    def get_next_batch(self, batch_size=15):
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
    'now': 5,          # Amazon Now Fresh
    'now_grocery': 5,  # Amazon Now Grocery
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
    'now_pets': 4,
    'now_household': 4,
    'now_personal_care': 4,
    'now_health': 4,
    'now_beauty': 4,
    'now_breakfast': 4,
    'now_canned': 4,
    'now_spices': 4,
    'now_rice': 4,
    'now_pasta': 4,
    'now_oil': 4,
    'now_sugar': 4,
    'now_water': 4,
    'now_juice': 4,
    'now_coffee': 4,
    'now_tea': 4,
    'now_eggs': 4,
    'now_chicken': 4,
    'now_fish': 4,
    'now_organic': 4,
    'now_gluten_free': 4,
    'now_vegan': 4,
    'now_keto': 4,
    'now_diet': 4,
    'now_ready_meals': 4,
    'now_desserts': 4,
    'now_ice_cream': 4,
    'now_chocolate': 4,
    'now_candy': 4,
    'now_nuts': 4,
    'now_honey': 4,
    'now_jam': 4,
    'now_sauce': 4,
    'now_condiments': 4,
    'now_baking': 4,
    'now_baby_food': 4,
    'now_diapers': 4,
    'now_wipes': 4,
    'now_formula': 4,
    'now_pet_food': 4,
    'now_cat_food': 4,
    'now_dog_food': 4,
    'now_cleaning': 4,
    'now_laundry': 4,
    'now_dishwashing': 4,
    'now_paper': 4,
    'now_air_fresheners': 4,
    'now_shampoo': 4,
    'now_soap': 4,
    'now_toothpaste': 4,
    'now_deodorant': 4,
    'now_shaving': 4,
    'now_skincare': 4,
    'now_haircare': 4,
    'now_vitamins': 4,
    'now_supplements': 4,
    'now_first_aid': 4,
    'now_medicine': 4,
    'now_sexual_health': 4,
    'now_fitness': 4,
    'now_wellness': 4,
    'now_organic_food': 4,
    'now_imported': 4,
    'now_local': 4,
    'now_fresh_market': 5,
    'now_daily_deals': 5,
    'now_best_sellers': 4,
    'now_new_arrivals': 4,
    'now_clearance': 5,
    'now_flash_deals': 5,
}

# ========== قائمة كل الأقسام الموسعة (كل صفحات أمازون المهمة + أمازون ناو) ==========
CATEGORIES_DEF = [
    # ==== Amazon Now Fresh & Grocery (كل التصنيفات) ====
    ("https://www.amazon.sa/s?k=fresh&rh=p_8%3A70-", "⚡ Amazon Now Fresh", 'now'),
    ("https://www.amazon.sa/s?k=amazon+now&rh=p_8%3A70-", "⚡ Amazon Now Deals", 'now_daily_deals'),
    ("https://www.amazon.sa/s?k=supermarket&rh=p_8%3A70-", "🛒 Supermarket Deals", 'now_supermarket'),
    ("https://www.amazon.sa/s?k=groceries&rh=p_8%3A70-", "🛒 Groceries 70% Off", 'now_grocery'),
    ("https://www.amazon.sa/s?k=fruits&rh=p_8%3A70-", "🍎 Fruits & Vegetables", 'now_fruits'),
    ("https://www.amazon.sa/s?k=vegetables&rh=p_8%3A70-", "🥕 Vegetables", 'now_vegetables'),
    ("https://www.amazon.sa/s?k=meat&rh=p_8%3A70-", "🥩 Meat & Poultry", 'now_meat'),
    ("https://www.amazon.sa/s?k=chicken&rh=p_8%3A70-", "🍗 Chicken", 'now_chicken'),
    ("https://www.amazon.sa/s?k=fish&rh=p_8%3A70-", "🐟 Fish & Seafood", 'now_fish'),
    ("https://www.amazon.sa/s?k=dairy&rh=p_8%3A70-", "🥛 Dairy & Eggs", 'now_dairy'),
    ("https://www.amazon.sa/s?k=eggs&rh=p_8%3A70-", "🥚 Eggs", 'now_eggs'),
    ("https://www.amazon.sa/s?k=bakery&rh=p_8%3A70-", "🍞 Bakery", 'now_bakery'),
    ("https://www.amazon.sa/s?k=frozen&rh=p_8%3A70-", "❄️ Frozen Food", 'now_frozen'),
    ("https://www.amazon.sa/s?k=drinks&rh=p_8%3A70-", "🥤 Drinks & Beverages", 'now_drinks'),
    ("https://www.amazon.sa/s?k=snacks&rh=p_8%3A70-", "🍿 Snacks", 'now_snacks'),
    ("https://www.amazon.sa/s?k=baby&rh=p_8%3A70-", "👶 Baby Products", 'now_baby'),
    ("https://www.amazon.sa/s?k=pet+food&rh=p_8%3A70-", "🐾 Pet Food", 'now_pet_food'),
    ("https://www.amazon.sa/s?k=cat+food&rh=p_8%3A70-", "🐱 Cat Food", 'now_cat_food'),
    ("https://www.amazon.sa/s?k=dog+food&rh=p_8%3A70-", "🐶 Dog Food", 'now_dog_food'),
    ("https://www.amazon.sa/s?k=household&rh=p_8%3A70-", "🏠 Household Supplies", 'now_household'),
    ("https://www.amazon.sa/s?k=cleaning&rh=p_8%3A70-", "🧼 Cleaning Products", 'now_cleaning'),
    ("https://www.amazon.sa/s?k=laundry&rh=p_8%3A70-", "🧺 Laundry", 'now_laundry'),
    ("https://www.amazon.sa/s?k=dishwashing&rh=p_8%3A70-", "🍽️ Dishwashing", 'now_dishwashing'),
    ("https://www.amazon.sa/s?k=paper+products&rh=p_8%3A70-", "🧻 Paper Products", 'now_paper'),
    ("https://www.amazon.sa/s?k=air+fresheners&rh=p_8%3A70-", "🌬️ Air Fresheners", 'now_air_fresheners'),
    ("https://www.amazon.sa/s?k=personal+care&rh=p_8%3A70-", "🧴 Personal Care", 'now_personal_care'),
    ("https://www.amazon.sa/s?k=shampoo&rh=p_8%3A70-", "💆 Shampoo", 'now_shampoo'),
    ("https://www.amazon.sa/s?k=soap&rh=p_8%3A70-", "🧼 Soap & Body Wash", 'now_soap'),
    ("https://www.amazon.sa/s?k=toothpaste&rh=p_8%3A70-", "🦷 Toothpaste", 'now_toothpaste'),
    ("https://www.amazon.sa/s?k=deodorant&rh=p_8%3A70-", "🧴 Deodorant", 'now_deodorant'),
    ("https://www.amazon.sa/s?k=shaving&rh=p_8%3A70-", "🪒 Shaving", 'now_shaving'),
    ("https://www.amazon.sa/s?k=skincare&rh=p_8%3A70-", "🧴 Skincare", 'now_skincare'),
    ("https://www.amazon.sa/s?k=haircare&rh=p_8%3A70-", "💇 Haircare", 'now_haircare'),
    ("https://www.amazon.sa/s?k=vitamins&rh=p_8%3A70-", "💊 Vitamins", 'now_vitamins'),
    ("https://www.amazon.sa/s?k=supplements&rh=p_8%3A70-", "💊 Supplements", 'now_supplements'),
    ("https://www.amazon.sa/s?k=first+aid&rh=p_8%3A70-", "🩹 First Aid", 'now_first_aid'),
    ("https://www.amazon.sa/s?k=medicine&rh=p_8%3A70-", "💊 Medicine", 'now_medicine'),
    ("https://www.amazon.sa/s?k=sexual+health&rh=p_8%3A70-", "❤️ Sexual Health", 'now_sexual_health'),
    ("https://www.amazon.sa/s?k=fitness&rh=p_8%3A70-", "🏋️ Fitness", 'now_fitness'),
    ("https://www.amazon.sa/s?k=wellness&rh=p_8%3A70-", "🌿 Wellness", 'now_wellness'),
    ("https://www.amazon.sa/s?k=organic&rh=p_8%3A70-", "🌱 Organic Food", 'now_organic'),
    ("https://www.amazon.sa/s?k=gluten+free&rh=p_8%3A70-", "🌾 Gluten Free", 'now_gluten_free'),
    ("https://www.amazon.sa/s?k=vegan&rh=p_8%3A70-", "🌱 Vegan", 'now_vegan'),
    ("https://www.amazon.sa/s?k=keto&rh=p_8%3A70-", "🥑 Keto", 'now_keto'),
    ("https://www.amazon.sa/s?k=diet&rh=p_8%3A70-", "🥗 Diet Food", 'now_diet'),
    ("https://www.amazon.sa/s?k=ready+meals&rh=p_8%3A70-", "🍱 Ready Meals", 'now_ready_meals'),
    ("https://www.amazon.sa/s?k=desserts&rh=p_8%3A70-", "🍰 Desserts", 'now_desserts'),
    ("https://www.amazon.sa/s?k=ice+cream&rh=p_8%3A70-", "🍦 Ice Cream", 'now_ice_cream'),
    ("https://www.amazon.sa/s?k=chocolate&rh=p_8%3A70-", "🍫 Chocolate", 'now_chocolate'),
    ("https://www.amazon.sa/s?k=candy&rh=p_8%3A70-", "🍬 Candy", 'now_candy'),
    ("https://www.amazon.sa/s?k=nuts&rh=p_8%3A70-", "🥜 Nuts & Seeds", 'now_nuts'),
    ("https://www.amazon.sa/s?k=honey&rh=p_8%3A70-", "🍯 Honey", 'now_honey'),
    ("https://www.amazon.sa/s?k=jam&rh=p_8%3A70-", "🍓 Jam & Spreads", 'now_jam'),
    ("https://www.amazon.sa/s?k=sauce&rh=p_8%3A70-", "🥫 Sauces", 'now_sauce'),
    ("https://www.amazon.sa/s?k=condiments&rh=p_8%3A70-", "🧂 Condiments", 'now_condiments'),
    ("https://www.amazon.sa/s?k=baking&rh=p_8%3A70-", "🥧 Baking Supplies", 'now_baking'),
    ("https://www.amazon.sa/s?k=baby+food&rh=p_8%3A70-", "👶 Baby Food", 'now_baby_food'),
    ("https://www.amazon.sa/s?k=diapers&rh=p_8%3A70-", "👶 Diapers", 'now_diapers'),
    ("https://www.amazon.sa/s?k=wipes&rh=p_8%3A70-", "👶 Wipes", 'now_wipes'),
    ("https://www.amazon.sa/s?k=formula&rh=p_8%3A70-", "👶 Baby Formula", 'now_formula'),
    ("https://www.amazon.sa/s?k=rice&rh=p_8%3A70-", "🍚 Rice", 'now_rice'),
    ("https://www.amazon.sa/s?k=pasta&rh=p_8%3A70-", "🍝 Pasta", 'now_pasta'),
    ("https://www.amazon.sa/s?k=cooking+oil&rh=p_8%3A70-", "🫒 Cooking Oil", 'now_oil'),
    ("https://www.amazon.sa/s?k=sugar&rh=p_8%3A70-", "🍬 Sugar", 'now_sugar'),
    ("https://www.amazon.sa/s?k=water&rh=p_8%3A70-", "💧 Water", 'now_water'),
    ("https://www.amazon.sa/s?k=juice&rh=p_8%3A70-", "🧃 Juice", 'now_juice'),
    ("https://www.amazon.sa/s?k=coffee&rh=p_8%3A70-", "☕ Coffee", 'now_coffee'),
    ("https://www.amazon.sa/s?k=tea&rh=p_8%3A70-", "🍵 Tea", 'now_tea'),
    ("https://www.amazon.sa/s?k=breakfast&rh=p_8%3A70-", "🥣 Breakfast", 'now_breakfast'),
    ("https://www.amazon.sa/s?k=canned+food&rh=p_8%3A70-", "🥫 Canned Food", 'now_canned'),
    ("https://www.amazon.sa/s?k=spices&rh=p_8%3A70-", "🌶️ Spices", 'now_spices'),
    ("https://www.amazon.sa/s?k=imported&rh=p_8%3A70-", "🌍 Imported Food", 'now_imported'),
    ("https://www.amazon.sa/s?k=local&rh=p_8%3A70-", "🇸🇦 Local Products", 'now_local'),
    ("https://www.amazon.sa/s?k=fresh+market&rh=p_8%3A70-", "🛒 Fresh Market", 'now_fresh_market'),
    ("https://www.amazon.sa/s?k=clearance&rh=p_8%3A70-", "🔥 Now Clearance", 'now_clearance'),
    ("https://www.amazon.sa/s?k=flash+deals&rh=p_8%3A70-", "⚡ Now Flash Deals", 'now_flash_deals'),
    ("https://www.amazon.sa/s?k=best+sellers&rh=p_8%3A70-", "⭐ Now Best Sellers", 'now_best_sellers'),
    ("https://www.amazon.sa/s?k=new+arrivals&rh=p_8%3A70-", "🆕 Now New Arrivals", 'now_new_arrivals'),

    # ==== عروض أمازون العامة (كل الأقسام المهمة) ====
    ("https://www.amazon.sa/s?rh=p_8%3A70-99", "🔥 All Deals 70% Off", 'deals'),
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox Today Deals", 'deals'),
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/grocery", "🥫 Grocery Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/home", "🏠 Home Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/kitchen", "🍳 Kitchen Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/toys", "🧸 Toys Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/sports", "⚽ Sports Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/automotive", "🚗 Automotive Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/baby", "👶 Baby Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/pet-supplies", "🐾 Pet Supplies Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals", 'warehouse'),
    ("https://www.amazon.sa/outlet", "🎁 Outlet Store", 'outlet'),
    ("https://www.amazon.sa/s?k=clearance&rh=p_8%3A70-", "🔥 Clearance Sale", 'clearance'),
    ("https://www.amazon.sa/s?k=deals&rh=p_8%3A70-", "🔥 Deals 70% Off", 'deals'),
    ("https://www.amazon.sa/s?k=electronics&rh=p_8%3A70-", "📱 Electronics Deals", 'deals'),
    ("https://www.amazon.sa/s?k=fashion&rh=p_8%3A70-", "👕 Fashion Deals", 'deals'),
    ("https://www.amazon.sa/s?k=beauty&rh=p_8%3A70-", "💄 Beauty Deals", 'deals'),
    ("https://www.amazon.sa/s?k=home&rh=p_8%3A70-", "🏠 Home Deals", 'deals'),
    ("https://www.amazon.sa/s?k=kitchen&rh=p_8%3A70-", "🍳 Kitchen Deals", 'deals'),
    ("https://www.amazon.sa/s?k=toys&rh=p_8%3A70-", "🧸 Toys Deals", 'deals'),
    ("https://www.amazon.sa/s?k=sports&rh=p_8%3A70-", "⚽ Sports Deals", 'deals'),
    ("https://www.amazon.sa/s?k=automotive&rh=p_8%3A70-", "🚗 Automotive Deals", 'deals'),
    ("https://www.amazon.sa/s?k=baby&rh=p_8%3A70-", "👶 Baby Deals", 'deals'),
    ("https://www.amazon.sa/s?k=pet+supplies&rh=p_8%3A70-", "🐾 Pet Supplies Deals", 'deals'),
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
    # التأكد الصارم من نسبة الخصم وأن السعر صالح
    if deal['discount'] < MIN_DISCOUNT or deal['price'] <= 0 or deal['old_price'] <= deal['price']:
        return False
    return True

def parse_item(item, category, is_best_seller):
    # 1. استخراج السعر الحالي
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

    # 2. استخراج السعر القديم الحقيقي والخصم
    old_price = 0
    discount = 0

    # البحث عن السعر الشاطبي/القديم
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

    # إذا لم يجد سعر سابق صريح، يبحث عن نسبة الخصم المكتوبة في بادج العرض
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

    # رفض المنتج إذا لم يكن هناك خصم موثوق به أصلًا
    if discount < MIN_DISCOUNT or old_price <= price:
        return None

    # 3. استخراج العنوان
    title = ""
    for sel in ['h2 a span', 'h2 span', '.a-size-base-plus', '.a-size-medium', '.a-size-mini span']:
        el = item.select_one(sel)
        if el and len(el.text.strip()) > 5:
            title = el.text.strip()
            break

    if not title:
        return None

    # 4. استخراج الرابط
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

def send_deal(bot, deal):
    global sent_products, sent_hashes
    
    deal_id = deal['id']
    should_resend = update_price_history_and_check_resend(deal)
    
    if (deal_id in sent_products or is_similar_product(deal['title'])) and not should_resend:
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
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown')

        sent_products.add(deal_id)
        sent_hashes.add(create_title_hash(deal['title']))
        save_database()
        export_to_excel([deal])
        logger.info(f"✅ Sent Deal: {deal['title'][:30]} - Discount: {deal['discount']}%")
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
            pages = page_rotator.get_next_batch(batch_size=12)
            for page_info in pages:
                html = fetch_page(session, page_info['url'])
                if not html:
                    continue
                
                soup = BeautifulSoup(html, 'html.parser')
                
                # تجميع عناصر المنتجات من مختلف أشكال صفحات أمازون
                items = soup.find_all('div', {'data-component-type': 's-search-result'})
                if not items:
                    items = soup.find_all('div', class_='s-result-item')
                if not items:
                    items = soup.find_all('li', class_='zg-item-immersion')
                if not items:
                    items = soup.find_all('div', id=re.compile(r'p13n-zg-list-grid-item'))

                for item in items:
                    deal = parse_item(item, page_info['category'], 'best_sellers' in page_info['type'])
                    if deal and is_valid_deal(deal):
                        send_deal(bot, deal)
                
                time.sleep(random.uniform(1.5, 3))
            
            time.sleep(15)  # تقليل فترة الراحة لزيادة سرعة الدوران على الأقسام
        except Exception as e:
            logger.error(f"Error in auto scan loop: {e}")
            time.sleep(10)

def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🤖 البوت يعمل تلقائياً بالخلفية ويفحص جميع الأقسام بسلاسة!")

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

    threading.Thread(target=auto_scan_and_send, args=(updater.bot,), daemon=True).start()

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))

    logger.info("🤖 Telegram Bot ready & Scanning...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
