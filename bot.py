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
from difflib import SequenceMatcher
import time
import random
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from flask import Flask
from collections import deque

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8769441239:AAFUuBQcJ6xj-9q-xhYFGEW6yNWT2xWzvAA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "432826122")
PORT = int(os.environ.get("PORT", 8080))

# ========== Flask App for 24/7 Keep-Alive ==========
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

# ========== Keep-Alive Ping ==========
def keep_alive_ping():
    """يرسل ping كل 10 دقايق عشان Render ما ينامش"""
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
is_scanning = False
updater = None

# Global flag to stop search
stop_search_flag = False

# ========== إعدادات البحث ==========
TARGET_DEALS_COUNT = 80      # عدد النتائج المطلوبة في كل مرة
MIN_DISCOUNT = 50            # الحد الأدنى للخصم (50%)
MIN_RATING = 3.5             # الحد الأدنى للتقييم (3.5 نجمة)

# ========== نظام تدوير الصفحات ==========
class PageRotationManager:
    def __init__(self):
        self.visited_pages = set()
        self.page_queue = deque()
        self.all_pages = []
        self.rotation_count = 0
        self.current_batch = []
        
    def load_state(self):
        try:
            if os.path.exists('page_rotation.json'):
                with open('page_rotation.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.visited_pages = set(data.get('visited', []))
                    self.rotation_count = data.get('rotation_count', 0)
                    logger.info(f"🔄 Loaded rotation state: {len(self.visited_pages)} visited, {self.rotation_count} rotations")
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
        
        logger.info(f"📋 Generated {len(self.all_pages)} total pages")
        return self.all_pages
    
    def _build_page_url(self, base_url, page_num):
        if page_num == 1:
            return base_url
            
        if 'gp/bestsellers' in base_url or 'gp/goldbox' in base_url:
            separator = '&' if '?' in base_url else '?'
            return f"{base_url}{separator}pg={page_num}"
        elif '/s?' in base_url or 'keywords=' in base_url:
            separator = '&' if '?' in base_url else '?'
            return f"{base_url}{separator}page={page_num}"
        else:
            separator = '&' if '?' in base_url else '?'
            return f"{base_url}{separator}page={page_num}"
    
    def get_next_batch(self, batch_size=50):
        if not self.page_queue:
            self._refill_queue()
        
        batch = []
        available_pages = [p for p in self.page_queue if p['id'] not in self.visited_pages]
        
        if len(self.visited_pages) >= len(self.all_pages) * 0.9:
            logger.info("🔄 All pages visited, resetting rotation...")
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
        
        self.current_batch = batch
        self.save_state()
        
        logger.info(f"🎯 Selected batch: {len(batch)} pages (Total visited: {len(self.visited_pages)})")
        return batch
    
    def _refill_queue(self):
        unvisited = [p for p in self.all_pages if p['id'] not in self.visited_pages]
        
        if not unvisited:
            unvisited = self.all_pages.copy()
            self.visited_pages.clear()
            self.rotation_count += 1
        
        random.shuffle(unvisited)
        self.page_queue = deque(unvisited)
        logger.info(f"🔄 Refilled queue with {len(unvisited)} pages")
    
    def get_stats(self):
        return {
            'total_pages': len(self.all_pages),
            'visited_pages': len(self.visited_pages),
            'remaining_pages': len(self.all_pages) - len(self.visited_pages),
            'rotation_count': self.rotation_count,
            'progress_percent': (len(self.visited_pages) / len(self.all_pages) * 100) if self.all_pages else 0
        }

page_rotator = PageRotationManager()

def load_database():
    global sent_products, sent_hashes
    try:
        if os.path.exists('bot_database.json'):
            with open('bot_database.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                sent_products = set(data.get('ids', []))
                sent_hashes = set(data.get('hashes', []))
                logger.info(f"📦 Loaded: {len(sent_products)}")
    except Exception as e:
        logger.error(f"Error loading DB: {e}")

def save_database():
    try:
        with open('bot_database.json', 'w', encoding='utf-8') as f:
            json.dump({
                'ids': list(sent_products),
                'hashes': list(sent_hashes)
            }, f)
    except Exception as e:
        logger.error(f"Error saving DB: {e}")

def extract_asin(link):
    if not link:
        return None
    patterns = [r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})', r'product/([A-Z0-9]{10})']
    for p in patterns:
        match = re.search(p, link, re.I)
        if match:
            asin = match.group(1).upper()
            if len(asin) == 10:
                return asin
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
    for existing_hash in list(sent_hashes)[-200:]:
        if new_hash[:10] == existing_hash[:10]:
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
    for i in range(3):
        try:
            time.sleep(random.uniform(1, 2))
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Attempt {i+1} failed: {e}")
    return None

PAGES_CONFIG = {
    'best_sellers': 3,
    'deals': 2,
    'warehouse': 2,
    'coupons': 2,
    'search': 2,
    'outlet': 2,
    'prime': 2,
    'lightning': 1,
    'today': 2,
    'clearance': 3,
}

CATEGORIES_DEF = [
    ("https://www.amazon.sa/gp/bestsellers/electronics", "📱 Electronics Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/fashion", "👕 Fashion Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/beauty", "💄 Beauty Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/watches", "⌚ Watches Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/shoes", "👟 Shoes Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/kitchen", "🍳 Kitchen Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/home", "🏠 Home Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/computers", "💻 Computers Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/mobile", "📱 Mobile Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/perfumes", "🌸 Perfumes Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/toys", "🎮 Toys Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/sports", "⚽ Sports Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/baby", "👶 Baby Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/grocery", "🛒 Grocery Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/automotive", "🚗 Automotive Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/tools", "🔧 Tools Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/books", "📚 Books Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/jewelry", "💎 Jewelry Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/luggage", "🧳 Luggage Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/pet", "🐾 Pet Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/office", "📎 Office Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/personal-care", "🧴 Personal Care Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/health", "💊 Health Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/video-games", "🎮 Games Best Seller", 'best_sellers'),
    ("https://www.amazon.sa/gp/bestsellers/camera", "📷 Camera Best Seller", 'best_sellers'),
    
    ("https://www.amazon.sa/gp/goldbox", "🔥 Goldbox", 'deals'),
    ("https://www.amazon.sa/deals/electronics", "📱 Electronics Deals", 'deals'),
    ("https://www.amazon.sa/deals/fashion", "👕 Fashion Deals", 'deals'),
    ("https://www.amazon.sa/deals/beauty", "💄 Beauty Deals", 'deals'),
    ("https://www.amazon.sa/deals/home", "🏠 Home Deals", 'deals'),
    ("https://www.amazon.sa/deals/kitchen", "🍳 Kitchen Deals", 'deals'),
    ("https://www.amazon.sa/deals/watches", "⌚ Watches Deals", 'deals'),
    ("https://www.amazon.sa/deals/perfumes", "🌸 Perfumes Deals", 'deals'),
    ("https://www.amazon.sa/deals/toys", "🎮 Toys Deals", 'deals'),
    ("https://www.amazon.sa/deals/sports", "⚽ Sports Deals", 'deals'),
    ("https://www.amazon.sa/deals/baby", "👶 Baby Deals", 'deals'),
    ("https://www.amazon.sa/deals/grocery", "🛒 Grocery Deals", 'deals'),
    ("https://www.amazon.sa/deals/automotive", "🚗 Automotive Deals", 'deals'),
    ("https://www.amazon.sa/deals/tools", "🔧 Tools Deals", 'deals'),
    ("https://www.amazon.sa/deals/office", "📎 Office Deals", 'deals'),
    ("https://www.amazon.sa/deals/books", "📚 Books Deals", 'deals'),
    
    ("https://www.amazon.sa/gp/warehouse-deals", "🏭 Warehouse Deals", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/electronics", "🏭 Warehouse Electronics", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/fashion", "🏭 Warehouse Fashion", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/home", "🏭 Warehouse Home", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/kitchen", "🏭 Warehouse Kitchen", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/beauty", "🏭 Warehouse Beauty", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/sports", "🏭 Warehouse Sports", 'warehouse'),
    ("https://www.amazon.sa/gp/warehouse-deals/tools", "🏭 Warehouse Tools", 'warehouse'),
    
    ("https://www.amazon.sa/gp/coupons", "🎟️ Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/electronics", "🎟️ Electronics Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/fashion", "🎟️ Fashion Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/home", "🎟️ Home Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/beauty", "🎟️ Beauty Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/grocery", "🎟️ Grocery Coupons", 'coupons'),
    ("https://www.amazon.sa/gp/coupons/baby", "🎟️ Baby Coupons", 'coupons'),
    
    ("https://www.amazon.sa/gp/prime/pipeline/prime_exclusives", "👑 Prime Exclusives", 'prime'),
    ("https://www.amazon.sa/gp/prime/pipeline/lightning_deals", "⚡ Lightning Deals", 'lightning'),
    
    ("https://www.amazon.sa/gp/todays-deals", "📅 Today Deals", 'today'),
    ("https://www.amazon.sa/gp/todays-deals/electronics", "📅 Today Electronics", 'today'),
    ("https://www.amazon.sa/gp/todays-deals/fashion", "📅 Today Fashion", 'today'),
    ("https://www.amazon.sa/gp/todays-deals/home", "📅 Today Home", 'today'),
    ("https://www.amazon.sa/gp/todays-deals/beauty", "📅 Today Beauty", 'today'),
    
    ("https://www.amazon.sa/outlet", "🎁 Outlet", 'outlet'),
    ("https://www.amazon.sa/outlet/electronics", "🎁 Outlet Electronics", 'outlet'),
    ("https://www.amazon.sa/outlet/home", "🎁 Outlet Home", 'outlet'),
    ("https://www.amazon.sa/outlet/fashion", "🎁 Outlet Fashion", 'outlet'),
    ("https://www.amazon.sa/outlet/beauty", "🎁 Outlet Beauty", 'outlet'),
    
    ("https://www.amazon.sa/s?k=clearance&rh=p_8%3A50-99", "🔥 Clearance", 'clearance'),
    ("https://www.amazon.sa/s?k=last+chance&rh=p_8%3A50-99", "🔥 Last Chance", 'clearance'),
    ("https://www.amazon.sa/s?k=final+sale&rh=p_8%3A50-99", "🔥 Final Sale", 'clearance'),
    ("https://www.amazon.sa/s?k=limited+time&rh=p_8%3A50-99", "⏰ Limited Time", 'clearance'),
    ("https://www.amazon.sa/s?k=flash+sale&rh=p_8%3A50-99", "⚡ Flash Sale", 'clearance'),
    ("https://www.amazon.sa/s?k=super+sale&rh=p_8%3A50-99", "💥 Super Sale", 'clearance'),
    ("https://www.amazon.sa/s?k=mega+deal&rh=p_8%3A50-99", "🎯 Mega Deal", 'clearance'),
    ("https://www.amazon.sa/s?k=big+sale&rh=p_8%3A50-99", "🎪 Big Sale", 'clearance'),
    
    ("https://www.amazon.sa/s?k=iphone&rh=p_8%3A30-99", "🍎 iPhone", 'search'),
    ("https://www.amazon.sa/s?k=ipad&rh=p_8%3A30-99", "🍎 iPad", 'search'),
    ("https://www.amazon.sa/s?k=macbook&rh=p_8%3A30-99", "🍎 MacBook", 'search'),
    ("https://www.amazon.sa/s?k=airpods&rh=p_8%3A30-99", "🍎 AirPods", 'search'),
    ("https://www.amazon.sa/s?k=apple+watch&rh=p_8%3A30-99", "🍎 Apple Watch", 'search'),
    ("https://www.amazon.sa/s?k=apple+tv&rh=p_8%3A30-99", "🍎 Apple TV", 'search'),
    ("https://www.amazon.sa/s?k=airtag&rh=p_8%3A30-99", "🍎 AirTag", 'search'),
    ("https://www.amazon.sa/s?k=homepod&rh=p_8%3A30-99", "🍎 HomePod", 'search'),
    
    ("https://www.amazon.sa/s?k=samsung+galaxy&rh=p_8%3A30-99", "📱 Galaxy Phone", 'search'),
    ("https://www.amazon.sa/s?k=samsung+tablet&rh=p_8%3A30-99", "📱 Galaxy Tab", 'search'),
    ("https://www.amazon.sa/s?k=samsung+watch&rh=p_8%3A30-99", "📱 Galaxy Watch", 'search'),
    ("https://www.amazon.sa/s?k=samsung+buds&rh=p_8%3A30-99", "📱 Galaxy Buds", 'search'),
    ("https://www.amazon.sa/s?k=samsung+tv&rh=p_8%3A30-99", "📱 Samsung TV", 'search'),
    ("https://www.amazon.sa/s?k=samsung+monitor&rh=p_8%3A30-99", "📱 Samsung Monitor", 'search'),
    
    ("https://www.amazon.sa/s?k=sony+headphones&rh=p_8%3A30-99", "🎧 Sony Headphones", 'search'),
    ("https://www.amazon.sa/s?k=bose+headphones&rh=p_8%3A30-99", "🎧 Bose Headphones", 'search'),
    ("https://www.amazon.sa/s?k=beats+headphones&rh=p_8%3A30-99", "🎧 Beats Headphones", 'search'),
    ("https://www.amazon.sa/s?k=jbl+speaker&rh=p_8%3A30-99", "🎧 JBL Speaker", 'search'),
    ("https://www.amazon.sa/s?k=harman+kardon&rh=p_8%3A30-99", "🎧 Harman Kardon", 'search'),
    ("https://www.amazon.sa/s?k=marshall&rh=p_8%3A30-99", "🎧 Marshall", 'search'),
    ("https://www.amazon.sa/s?k=skullcandy&rh=p_8%3A30-99", "🎧 Skullcandy", 'search'),
    ("https://www.amazon.sa/s?k=sennheiser&rh=p_8%3A30-99", "🎧 Sennheiser", 'search'),
    
    ("https://www.amazon.sa/s?k=lenovo+laptop&rh=p_8%3A30-99", "💻 Lenovo Laptop", 'search'),
    ("https://www.amazon.sa/s?k=hp+laptop&rh=p_8%3A30-99", "💻 HP Laptop", 'search'),
    ("https://www.amazon.sa/s?k=dell+laptop&rh=p_8%3A30-99", "💻 Dell Laptop", 'search'),
    ("https://www.amazon.sa/s?k=asus+laptop&rh=p_8%3A30-99", "💻 Asus Laptop", 'search'),
    ("https://www.amazon.sa/s?k=acer+laptop&rh=p_8%3A30-99", "💻 Acer Laptop", 'search'),
    ("https://www.amazon.sa/s?k=msi+laptop&rh=p_8%3A30-99", "💻 MSI Laptop", 'search'),
    ("https://www.amazon.sa/s?k=razer+laptop&rh=p_8%3A30-99", "💻 Razer Laptop", 'search'),
    ("https://www.amazon.sa/s?k=alienware&rh=p_8%3A30-99", "💻 Alienware", 'search'),
    
    ("https://www.amazon.sa/s?k=playstation+5&rh=p_8%3A30-99", "🎮 PS5", 'search'),
    ("https://www.amazon.sa/s?k=playstation+4&rh=p_8%3A30-99", "🎮 PS4", 'search'),
    ("https://www.amazon.sa/s?k=xbox+series&rh=p_8%3A30-99", "🎮 Xbox Series", 'search'),
    ("https://www.amazon.sa/s?k=nintendo+switch&rh=p_8%3A30-99", "🎮 Nintendo Switch", 'search'),
    ("https://www.amazon.sa/s?k=gaming+mouse&rh=p_8%3A30-99", "🎮 Gaming Mouse", 'search'),
    ("https://www.amazon.sa/s?k=gaming+keyboard&rh=p_8%3A30-99", "🎮 Gaming Keyboard", 'search'),
    ("https://www.amazon.sa/s?k=gaming+headset&rh=p_8%3A30-99", "🎮 Gaming Headset", 'search'),
    ("https://www.amazon.sa/s?k=gaming+chair&rh=p_8%3A30-99", "🎮 Gaming Chair", 'search'),
    ("https://www.amazon.sa/s?k=rtx+graphics&rh=p_8%3A30-99", "🎮 RTX Graphics", 'search'),
    
    ("https://www.amazon.sa/s?k=canon+camera&rh=p_8%3A30-99", "📷 Canon Camera", 'search'),
    ("https://www.amazon.sa/s?k=nikon+camera&rh=p_8%3A30-99", "📷 Nikon Camera", 'search'),
    ("https://www.amazon.sa/s?k=sony+camera&rh=p_8%3A30-99", "📷 Sony Camera", 'search'),
    ("https://www.amazon.sa/s?k=fujifilm&rh=p_8%3A30-99", "📷 Fujifilm", 'search'),
    ("https://www.amazon.sa/s?k=gopro&rh=p_8%3A30-99", "📷 GoPro", 'search'),
    ("https://www.amazon.sa/s?k=dji&rh=p_8%3A30-99", "📷 DJI Drone", 'search'),
    
    ("https://www.amazon.sa/s?k=apple+watch&rh=p_8%3A30-99", "⌚ Apple Watch", 'search'),
    ("https://www.amazon.sa/s?k=samsung+watch&rh=p_8%3A30-99", "⌚ Galaxy Watch", 'search'),
    ("https://www.amazon.sa/s?k=garmin&rh=p_8%3A30-99", "⌚ Garmin", 'search'),
    ("https://www.amazon.sa/s?k=fitbit&rh=p_8%3A30-99", "⌚ Fitbit", 'search'),
    ("https://www.amazon.sa/s?k=huawei+watch&rh=p_8%3A30-99", "⌚ Huawei Watch", 'search'),
    ("https://www.amazon.sa/s?k=amazfit&rh=p_8%3A30-99", "⌚ Amazfit", 'search'),
    ("https://www.amazon.sa/s?k=casio+g+shock&rh=p_8%3A30-99", "⌚ G-Shock", 'search'),
    ("https://www.amazon.sa/s?k=casio+edifice&rh=p_8%3A30-99", "⌚ Edifice", 'search'),
    ("https://www.amazon.sa/s?k=seiko&rh=p_8%3A30-99", "⌚ Seiko", 'search'),
    ("https://www.amazon.sa/s?k=citizen&rh=p_8%3A30-99", "⌚ Citizen", 'search'),
    
    ("https://www.amazon.sa/s?k=chanel+perfume&rh=p_8%3A30-99", "🌸 Chanel", 'search'),
    ("https://www.amazon.sa/s?k=dior+perfume&rh=p_8%3A30-99", "🌸 Dior", 'search'),
    ("https://www.amazon.sa/s?k=gucci+perfume&rh=p_8%3A30-99", "🌸 Gucci", 'search'),
    ("https://www.amazon.sa/s?k=versace+perfume&rh=p_8%3A30-99", "🌸 Versace", 'search'),
    ("https://www.amazon.sa/s?k=armani+perfume&rh=p_8%3A30-99", "🌸 Armani", 'search'),
    ("https://www.amazon.sa/s?k=prada+perfume&rh=p_8%3A30-99", "🌸 Prada", 'search'),
    ("https://www.amazon.sa/s?k=burberry+perfume&rh=p_8%3A30-99", "🌸 Burberry", 'search'),
    ("https://www.amazon.sa/s?k=calvin+klein+perfume&rh=p_8%3A30-99", "🌸 CK Perfume", 'search'),
    ("https://www.amazon.sa/s?k=tom+ford+perfume&rh=p_8%3A30-99", "🌸 Tom Ford", 'search'),
    ("https://www.amazon.sa/s?k=yves+saint+laurent+perfume&rh=p_8%3A30-99", "🌸 YSL", 'search'),
    ("https://www.amazon.sa/s?k=creed+perfume&rh=p_8%3A30-99", "🌸 Creed", 'search'),
    ("https://www.amazon.sa/s?k=jo+malone&rh=p_8%3A30-99", "🌸 Jo Malone", 'search'),
    
    ("https://www.amazon.sa/s?k=nike+shoes&rh=p_8%3A30-99", "👟 Nike Shoes", 'search'),
    ("https://www.amazon.sa/s?k=adidas+shoes&rh=p_8%3A30-99", "👟 Adidas Shoes", 'search'),
    ("https://www.amazon.sa/s?k=jordan&rh=p_8%3A30-99", "👟 Jordan", 'search'),
    ("https://www.amazon.sa/s?k=yeezy&rh=p_8%3A30-99", "👟 Yeezy", 'search'),
    ("https://www.amazon.sa/s?k=new+balance+shoes&rh=p_8%3A30-99", "👟 New Balance", 'search'),
    ("https://www.amazon.sa/s?k=puma+shoes&rh=p_8%3A30-99", "👟 Puma Shoes", 'search'),
    ("https://www.amazon.sa/s?k=reebok+shoes&rh=p_8%3A30-99", "👟 Reebok Shoes", 'search'),
    ("https://www.amazon.sa/s?k=under+armour+shoes&rh=p_8%3A30-99", "👟 UA Shoes", 'search'),
    ("https://www.amazon.sa/s?k=asics&rh=p_8%3A30-99", "👟 Asics", 'search'),
    ("https://www.amazon.sa/s?k=vans&rh=p_8%3A30-99", "👟 Vans", 'search'),
    ("https://www.amazon.sa/s?k=converse&rh=p_8%3A30-99", "👟 Converse", 'search'),
    ("https://www.amazon.sa/s?k=crocs&rh=p_8%3A30-99", "👟 Crocs", 'search'),
    
    ("https://www.amazon.sa/s?k=calvin+klein+men&rh=p_8%3A30-99", "👔 CK Men", 'search'),
    ("https://www.amazon.sa/s?k=tommy+hilfiger+men&rh=p_8%3A30-99", "👔 Tommy Men", 'search'),
    ("https://www.amazon.sa/s?k=ralph+lauren+men&rh=p_8%3A30-99", "👔 RL Men", 'search'),
    ("https://www.amazon.sa/s?k=lacoste+men&rh=p_8%3A30-99", "👔 Lacoste Men", 'search'),
    ("https://www.amazon.sa/s?k=hugo+boss&rh=p_8%3A30-99", "👔 Hugo Boss", 'search'),
    ("https://www.amazon.sa/s?k=levis+jeans&rh=p_8%3A30-99", "👔 Levis Jeans", 'search'),
    ("https://www.amazon.sa/s?k=wrangler+jeans&rh=p_8%3A30-99", "👔 Wrangler Jeans", 'search'),
    ("https://www.amazon.sa/s?k=diesel&rh=p_8%3A30-99", "👔 Diesel", 'search'),
    ("https://www.amazon.sa/s?k=g+star&rh=p_8%3A30-99", "👔 G-Star", 'search'),
    
    ("https://www.amazon.sa/s?k=michael+kors+bag&rh=p_8%3A30-99", "👜 MK Bags", 'search'),
    ("https://www.amazon.sa/s?k=kate+spade&rh=p_8%3A30-99", "👜 Kate Spade", 'search'),
    ("https://www.amazon.sa/s?k=coach+bag&rh=p_8%3A30-99", "👜 Coach", 'search'),
    ("https://www.amazon.sa/s?k=guess+bag&rh=p_8%3A30-99", "👜 Guess", 'search'),
    ("https://www.amazon.sa/s?k=fossil+bag&rh=p_8%3A30-99", "👜 Fossil Bag", 'search'),
    ("https://www.amazon.sa/s?k=vera+bradley&rh=p_8%3A30-99", "👜 Vera Bradley", 'search'),
    
    ("https://www.amazon.sa/s?k=swarovski&rh=p_8%3A30-99", "💎 Swarovski", 'search'),
    ("https://www.amazon.sa/s?k=pandora&rh=p_8%3A30-99", "💎 Pandora", 'search'),
    ("https://www.amazon.sa/s?k=tiffany&rh=p_8%3A30-99", "💎 Tiffany", 'search'),
    ("https://www.amazon.sa/s?k=cartier&rh=p_8%3A30-99", "💎 Cartier", 'search'),
    ("https://www.amazon.sa/s?k=bulova&rh=p_8%3A30-99", "💎 Bulova", 'search'),
    ("https://www.amazon.sa/s?k=anne+klein&rh=p_8%3A30-99", "💎 Anne Klein", 'search'),
    
    ("https://www.amazon.sa/s?k=ray+ban&rh=p_8%3A30-99", "🕶️ Ray Ban", 'search'),
    ("https://www.amazon.sa/s?k=oakley&rh=p_8%3A30-99", "🕶️ Oakley", 'search'),
    ("https://www.amazon.sa/s?k=persol&rh=p_8%3A30-99", "🕶️ Persol", 'search'),
    ("https://www.amazon.sa/s?k=prada+sunglasses&rh=p_8%3A30-99", "🕶️ Prada", 'search'),
    ("https://www.amazon.sa/s?k=gucci+sunglasses&rh=p_8%3A30-99", "🕶️ Gucci Sun", 'search'),
    ("https://www.amazon.sa/s?k=burberry+sunglasses&rh=p_8%3A30-99", "🕶️ Burberry Sun", 'search'),
    
    ("https://www.amazon.sa/s?k=mac+makeup&rh=p_8%3A30-99", "💄 MAC", 'search'),
    ("https://www.amazon.sa/s?k=nyx+makeup&rh=p_8%3A30-99", "💄 NYX", 'search'),
    ("https://www.amazon.sa/s?k=maybelline+makeup&rh=p_8%3A30-99", "💄 Maybelline", 'search'),
    ("https://www.amazon.sa/s?k=loreal+makeup&rh=p_8%3A30-99", "💄 L'Oreal", 'search'),
    ("https://www.amazon.sa/s?k=revlon&rh=p_8%3A30-99", "💄 Revlon", 'search'),
    ("https://www.amazon.sa/s?k=covergirl&rh=p_8%3A30-99", "💄 Covergirl", 'search'),
    ("https://www.amazon.sa/s?k=bobbi+brown&rh=p_8%3A30-99", "💄 Bobbi Brown", 'search'),
    ("https://www.amazon.sa/s?k=anastasia&rh=p_8%3A30-99", "💄 Anastasia", 'search'),
    ("https://www.amazon.sa/s?k=huda+beauty&rh=p_8%3A30-99", "💄 Huda Beauty", 'search'),
    ("https://www.amazon.sa/s?k=fenty+beauty&rh=p_8%3A30-99", "💄 Fenty", 'search'),
    
    ("https://www.amazon.sa/s?k=olay&rh=p_8%3A30-99", "💆 Olay", 'search'),
    ("https://www.amazon.sa/s?k=neutrogena&rh=p_8%3A30-99", "💆 Neutrogena", 'search'),
    ("https://www.amazon.sa/s?k=cerave&rh=p_8%3A30-99", "💆 CeraVe", 'search'),
    ("https://www.amazon.sa/s?k=cetaphil&rh=p_8%3A30-99", "💆 Cetaphil", 'search'),
    ("https://www.amazon.sa/s?k=la+roche+posay&rh=p_8%3A30-99", "💆 La Roche", 'search'),
    ("https://www.amazon.sa/s?k=vichy&rh=p_8%3A30-99", "💆 Vichy", 'search'),
    ("https://www.amazon.sa/s?k=eucerin&rh=p_8%3A30-99", "💆 Eucerin", 'search'),
    ("https://www.amazon.sa/s?k=aveeno&rh=p_8%3A30-99", "💆 Aveeno", 'search'),
    ("https://www.amazon.sa/s?k=bioderma&rh=p_8%3A30-99", "💆 Bioderma", 'search'),
    
    ("https://www.amazon.sa/s?k=pampers&rh=p_8%3A30-99", "👶 Pampers", 'search'),
    ("https://www.amazon.sa/s?k=huggies&rh=p_8%3A30-99", "👶 Huggies", 'search'),
    ("https://www.amazon.sa/s?k=johnson+baby&rh=p_8%3A30-99", "👶 Johnson's", 'search'),
    ("https://www.amazon.sa/s?k=mustela&rh=p_8%3A30-99", "👶 Mustela", 'search'),
    ("https://www.amazon.sa/s?k=aveeno+baby&rh=p_8%3A30-99", "👶 Aveeno Baby", 'search'),
    ("https://www.amazon.sa/s?k=lego&rh=p_8%3A30-99", "🧱 LEGO", 'search'),
    ("https://www.amazon.sa/s?k=barbie&rh=p_8%3A30-99", "👸 Barbie", 'search'),
    ("https://www.amazon.sa/s?k=hot+wheels&rh=p_8%3A30-99", "🚗 Hot Wheels", 'search'),
    ("https://www.amazon.sa/s?k=fisher+price&rh=p_8%3A30-99", "🎠 Fisher Price", 'search'),
    ("https://www.amazon.sa/s?k=little+tikes&rh=p_8%3A30-99", "🎪 Little Tikes", 'search'),
    ("https://www.amazon.sa/s?k=playskool&rh=p_8%3A30-99", "🎨 Playskool", 'search'),
    ("https://www.amazon.sa/s?k=vtech&rh=p_8%3A30-99", "🔤 VTech", 'search'),
    ("https://www.amazon.sa/s?k=leapfrog&rh=p_8%3A30-99", "🐸 LeapFrog", 'search'),
    
    ("https://www.amazon.sa/s?k=fitness+equipment&rh=p_8%3A30-99", "🏋️ Fitness", 'search'),
    ("https://www.amazon.sa/s?k=yoga+mat&rh=p_8%3A30-99", "🧘 Yoga Mat", 'search'),
    ("https://www.amazon.sa/s?k=dumbbells&rh=p_8%3A30-99", "🏋️ Dumbbells", 'search'),
    ("https://www.amazon.sa/s?k=kettlebell&rh=p_8%3A30-99", "🏋️ Kettlebell", 'search'),
    ("https://www.amazon.sa/s?k=resistance+bands&rh=p_8%3A30-99", "🏋️ Resistance", 'search'),
    ("https://www.amazon.sa/s?k=treadmill&rh=p_8%3A30-99", "🏃 Treadmill", 'search'),
    ("https://www.amazon.sa/s?k=exercise+bike&rh=p_8%3A30-99", "🚴 Exercise Bike", 'search'),
    ("https://www.amazon.sa/s?k=elliptical&rh=p_8%3A30-99", "🏃 Elliptical", 'search'),
    ("https://www.amazon.sa/s?k=protein+powder&rh=p_8%3A30-99", "💪 Protein", 'search'),
    ("https://www.amazon.sa/s?k=bcaa&rh=p_8%3A30-99", "💪 BCAA", 'search'),
    ("https://www.amazon.sa/s?k=creatine&rh=p_8%3A30-99", "💪 Creatine", 'search'),
    ("https://www.amazon.sa/s?k=pre+workout&rh=p_8%3A30-99", "💪 Pre Workout", 'search'),
    ("https://www.amazon.sa/s?k=optimum+nutrition&rh=p_8%3A30-99", "💪 ON", 'search'),
    ("https://www.amazon.sa/s?k=muscletech&rh=p_8%3A30-99", "💪 MuscleTech", 'search'),
    ("https://www.amazon.sa/s?k=dymatize&rh=p_8%3A30-99", "💪 Dymatize", 'search'),
    ("https://www.amazon.sa/s?k=bpi+sports&rh=p_8%3A30-99", "💪 BPI", 'search'),
    
    ("https://www.amazon.sa/s?k=philips+air+fryer&rh=p_8%3A30-99", "🏠 Philips AirFryer", 'search'),
    ("https://www.amazon.sa/s?k=ninja+blender&rh=p_8%3A30-99", "🥤 Ninja", 'search'),
    ("https://www.amazon.sa/s?k=nespresso&rh=p_8%3A30-99", "☕ Nespresso", 'search'),
    ("https://www.amazon.sa/s?k=delonghi&rh=p_8%3A30-99", "☕ DeLonghi", 'search'),
    ("https://www.amazon.sa/s?k=breville&rh=p_8%3A30-99", "🏠 Breville", 'search'),
    ("https://www.amazon.sa/s?k=kenwood&rh=p_8%3A30-99", "🏠 Kenwood", 'search'),
    ("https://www.amazon.sa/s?k=kitchenaid&rh=p_8%3A30-99", "🏠 KitchenAid", 'search'),
    ("https://www.amazon.sa/s?k=cuisinart&rh=p_8%3A30-99", "🏠 Cuisinart", 'search'),
    ("https://www.amazon.sa/s?k=tupperware&rh=p_8%3A30-99", "🥣 Tupperware", 'search'),
    ("https://www.amazon.sa/s?k=pyrex&rh=p_8%3A30-99", "🍽️ Pyrex", 'search'),
    ("https://www.amazon.sa/s?k=corelle&rh=p_8%3A30-99", "🍽️ Corelle", 'search'),
    ("https://www.amazon.sa/s?k=dyson+vacuum&rh=p_8%3A30-99", "🏠 Dyson", 'search'),
    ("https://www.amazon.sa/s?k=irobot&rh=p_8%3A30-99", "🏠 iRobot", 'search'),
    ("https://www.amazon.sa/s?k=ecovacs&rh=p_8%3A30-99", "🏠 Ecovacs", 'search'),
    ("https://www.amazon.sa/s?k=braun+blender&rh=p_8%3A30-99", "🏠 Braun", 'search'),
    
    ("https://www.amazon.sa/s?k=bosch+tools&rh=p_8%3A30-99", "🔧 Bosch", 'search'),
    ("https://www.amazon.sa/s?k=makita&rh=p_8%3A30-99", "🔧 Makita", 'search'),
    ("https://www.amazon.sa/s?k=dewalt&rh=p_8%3A30-99", "🔧 DeWalt", 'search'),
    ("https://www.amazon.sa/s?k=black+decker&rh=p_8%3A30-99", "🔧 Black & Decker", 'search'),
    ("https://www.amazon.sa/s?k=stanley&rh=p_8%3A30-99", "🔧 Stanley", 'search'),
    ("https://www.amazon.sa/s?k=craftsman&rh=p_8%3A30-99", "🔧 Craftsman", 'search'),
    ("https://www.amazon.sa/s?k=ryobi&rh=p_8%3A30-99", "🔧 Ryobi", 'search'),
    ("https://www.amazon.sa/s?k=worx&rh=p_8%3A30-99", "🔧 Worx", 'search'),
    
    ("https://www.amazon.sa/s?k=michelin+tires&rh=p_8%3A30-99", "🚗 Michelin", 'search'),
    ("https://www.amazon.sa/s?k=bridgestone+tires&rh=p_8%3A30-99", "🚗 Bridgestone", 'search'),
    ("https://www.amazon.sa/s?k=goodyear+tires&rh=p_8%3A30-99", "🚗 Goodyear", 'search'),
    ("https://www.amazon.sa/s?k=pirelli&rh=p_8%3A30-99", "🚗 Pirelli", 'search'),
    ("https://www.amazon.sa/s?k=continental+tires&rh=p_8%3A30-99", "🚗 Continental", 'search'),
    ("https://www.amazon.sa/s?k=bosch+car&rh=p_8%3A30-99", "🚗 Bosch Car", 'search'),
    ("https://www.amazon.sa/s?k=shell+oil&rh=p_8%3A30-99", "🚗 Shell", 'search'),
    ("https://www.amazon.sa/s?k=mobil+1&rh=p_8%3A30-99", "🚗 Mobil 1", 'search'),
    ("https://www.amazon.sa/s?k=castrol&rh=p_8%3A30-99", "🚗 Castrol", 'search'),
    
    ("https://www.amazon.sa/s?k=kindle&rh=p_8%3A30-99", "📚 Kindle", 'search'),
    ("https://www.amazon.sa/s?k=harry+potter+book&rh=p_8%3A30-99", "📚 Harry Potter", 'search'),
    
    ("https://www.amazon.sa/s?k=dates&rh=p_8%3A30-99", "🌴 Dates", 'search'),
    ("https://www.amazon.sa/s?k=oud&rh=p_8%3A30-99", "🌿 Oud", 'search'),
    ("https://www.amazon.sa/s?k=bakhoor&rh=p_8%3A30-99", "🌿 Bakhoor", 'search'),
    ("https://www.amazon.sa/s?k=prayer+mat&rh=p_8%3A30-99", "🕌 Prayer Mat", 'search'),
    ("https://www.amazon.sa/s?k=thobe&rh=p_8%3A30-99", "👘 Thobe", 'search'),
    ("https://www.amazon.sa/s?k=abaya&rh=p_8%3A30-99", "🧕 Abaya", 'search'),
    ("https://www.amazon.sa/s?k=ramadan&rh=p_8%3A30-99", "🌙 Ramadan", 'search'),
    ("https://www.amazon.sa/s?k=eid&rh=p_8%3A30-99", "🎉 Eid", 'search'),
    ("https://www.amazon.sa/s?k=hajj&rh=p_8%3A30-99", "🕋 Hajj", 'search'),
    ("https://www.amazon.sa/s?k=umrah&rh=p_8%3A30-99", "🕋 Umrah", 'search'),
    
    ("https://www.amazon.sa/s?k=louis+vuitton&rh=p_8%3A30-99", "👜 LV", 'search'),
    ("https://www.amazon.sa/s?k=hermes&rh=p_8%3A30-99", "👜 Hermes", 'search'),
    ("https://www.amazon.sa/s?k=coach&rh=p_8%3A30-99", "👜 Coach", 'search'),
    ("https://www.amazon.sa/s?k=kate+spade&rh=p_8%3A30-99", "👜 Kate Spade", 'search'),
    ("https://www.amazon.sa/s?k=burberry+bag&rh=p_8%3A30-99", "👜 Burberry", 'search'),
    ("https://www.amazon.sa/s?k=longchamp&rh=p_8%3A30-99", "👜 Longchamp", 'search'),
    ("https://www.amazon.sa/s?k=tumi&rh=p_8%3A30-99", "🧳 Tumi", 'search'),
    ("https://www.amazon.sa/s?k=samsonite&rh=p_8%3A30-99", "🧳 Samsonite", 'search'),
    ("https://www.amazon.sa/s?k=rimowa&rh=p_8%3A30-99", "🧳 Rimowa", 'search'),
]

def search_all_deals(chat_id, status_message_id):
    all_deals = []
    session = create_session()
    
    if not page_rotator.all_pages:
        page_rotator.generate_all_pages(CATEGORIES_DEF)
        page_rotator.load_state()
    
    # البحث في دفعات حتى نحصل على 40 نتيجة مميزة
    batch_size = 50
    max_attempts = 15  # عدد محاولات البحث لضمان الحصول على 40 نتيجة
    
    for attempt in range(max_attempts):
        # Check if user sent stop signal
        if stop_search_flag:
            logger.info("🛑 Search stopped by user")
            break

        if len(all_deals) >= TARGET_DEALS_COUNT * 3:  # نجمع أكثر لنضمن جودة التصفية
            break
            
        pages_to_search = page_rotator.get_next_batch(batch_size)
        
        if not pages_to_search:
            logger.error("No pages available for search")
            break
        
        total_pages = len(pages_to_search)
        processed = 0
        
        for page_info in pages_to_search:
            # Check stop signal
            if stop_search_flag:
                logger.info("🛑 Search stopped during page processing")
                break

            try:
                processed += 1
                
                if processed % 5 == 0:
                    stats = page_rotator.get_stats()
                    progress = f"⏳ جاري البحث... ({processed}/{total_pages} صفحة)\n📍 {page_info['category']} - صفحة {page_info['page_num']}\n🔄 دورة: {stats['rotation_count']}\n✅ تم جمع: {len(all_deals)} صفقة"
                    try:
                        updater.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_message_id,
                            text=progress
                        )
                    except:
                        pass
                
                logger.info(f"🔍 [{page_info['category']}] Page {page_info['page_num']}")
                html = fetch_page(session, page_info['url'])
                if not html:
                    continue
                
                soup = BeautifulSoup(html, 'html.parser')
                
                items = []
                if 'best_sellers' in page_info['type']:
                    items.extend(soup.find_all('li', class_='zg-item-immersion'))
                    items.extend(soup.find_all('div', class_='p13n-sc-uncoverable-faceout'))
                
                items.extend(soup.find_all('div', {'data-component-type': 's-search-result'}))
                items.extend(soup.find_all('div', {'data-testid': 'deal-card'}))
                items.extend(soup.find_all('div', class_='s-result-item'))
                items.extend(soup.find_all('div', class_='a-section'))
                
                logger.info(f"   Found {len(items)} items")
                
                for item in items:
                    try:
                        deal = parse_item(item, page_info['category'], 'best_sellers' in page_info['type'])
                        if deal and is_valid_deal(deal):
                            all_deals.append(deal)
                    except:
                        continue
                
                time.sleep(random.uniform(1.5, 3))
                
            except Exception as e:
                logger.error(f"Error in {page_info['category']}: {e}")
        
        logger.info(f"✅ Attempt {attempt+1}: Collected {len(all_deals)} valid deals")
    
    stats = page_rotator.get_stats()
    logger.info(f"✅ Total collected: {len(all_deals)} deals | Progress: {stats['progress_percent']:.1f}%")
    return all_deals

def is_valid_deal(deal):
    """التحقق من شروط الصفقة: خصم >50% وتقييم >3.5"""
    # التحقق من الخصم
    if deal['discount'] < MIN_DISCOUNT:
        return False
    
    # التحقق من التقييم
    if deal['rating'] < MIN_RATING:
        return False
    
    # التحقق من السعر المنطقي
    if deal['price'] <= 0 or deal['price'] > 10000:
        return False
    
    return True

def parse_item(item, category, is_best_seller):
    price = None
    for sel in ['.a-price-whole', '.a-price .a-offscreen', '.a-price-range', '.a-price']:
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
    
    if not price or price <= 0:
        return None
    
    old_price = 0
    discount = 0
    
    old_el = item.find('span', class_='a-text-price')
    if old_el:
        txt = old_el.get_text()
        match = re.search(r'[\d,]+\.?\d*', txt.replace(',', ''))
        if match:
            try:
                old_price = float(match.group())
                if old_price > price:
                    discount = int(((old_price - price) / old_price) * 100)
            except:
                pass
    
    if discount == 0:
        badge = item.find(string=re.compile(r'(\d+)%'))
        if badge:
            match = re.search(r'(\d+)', str(badge))
            if match:
                discount = int(match.group())
                old_price = price / (1 - discount/100)
    
    title = "Unknown"
    for sel in ['h2 a span', 'h2 span', '.a-size-mini span', '.a-size-base-plus', '.p13n-sc-truncated', '.a-size-medium']:
        el = item.select_one(sel)
        if el:
            title = el.text.strip()
            if len(title) > 5:
                break
    
    link = ""
    a = item.find('a', href=True)
    if a:
        href = a['href']
        if href.startswith('/'):
            link = f"https://www.amazon.sa{href}"
        elif 'amazon.sa' in href:
            link = href
        else:
            asin = extract_asin(href)
            if asin:
                link = f"https://www.amazon.sa/dp/{asin}"
    
    img = ""
    for sel in ['img.s-image', 'img[src]', '.s-image']:
        el = item.select_one(sel)
        if el:
            img = el.get('src', '') or el.get('data-src', '') or el.get('data-lazy-src', '')
            if img.startswith('http'):
                break
    
    rating = 0
    reviews = 0
    
    rate_el = item.find('span', class_='a-icon-alt')
    if rate_el:
        rating = parse_rating(rate_el.text)
    
    rev_el = item.find('span', class_='a-size-base')
    if rev_el:
        match = re.search(r'[\d,]+', rev_el.text)
        if match:
            try:
                reviews = int(match.group().replace(',', ''))
            except:
                pass
    
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

def filter_premium_deals(deals):
    """اختيار 40 نتيجة عشوائية مميزة بدون تكرار"""
    filtered = []
    seen_ids = set()
    
    # ترتيب عشوائي للنتائج
    random.shuffle(deals)
    
    for deal in deals:
        deal_id = deal['id']
        
        # التأكد من عدم التكرار في هذه الدفعة
        if deal_id in seen_ids:
            continue
        
        # التأكد من عدم الإرسال سابقاً
        if deal_id in sent_products:
            continue
        
        # التأكد من عدم التشابه
        if is_similar_product(deal['title']):
            continue
        
        seen_ids.add(deal_id)
        
        # تحديد نوع الصفقة
        if deal['price'] < 1:
            deal['type'] = '🔥 GLITCH'
        elif 'Warehouse' in deal['category']:
            deal['type'] = '🏭 WAREHOUSE'
        elif 'Outlet' in deal['category']:
            deal['type'] = '🎁 OUTLET'
        elif 'Coupon' in deal['category']:
            deal['type'] = '🎟️ COUPON'
        elif 'Prime' in deal['category']:
            deal['type'] = '👑 PRIME'
        elif 'Lightning' in deal['category']:
            deal['type'] = '⚡ LIGHTNING'
        elif deal.get('is_best_seller'):
            deal['type'] = '⭐ BEST SELLER'
        else:
            deal['type'] = f'💰 {deal["discount"]}%'
        
        deal['savings'] = round(deal['old_price'] - deal['price'], 2) if deal['old_price'] > 0 else 0
        filtered.append(deal)
        
        # التوقف عند الوصول للهدف
        if len(filtered) >= TARGET_DEALS_COUNT:
            break
    
    # ترتيب النهائي حسب الأولوية
    filtered.sort(key=lambda x: (
        0 if x['type'] == '🔥 GLITCH' else 1,
        0 if x['type'] == '🏭 WAREHOUSE' else 1,
        0 if x['type'] == '⚡ LIGHTNING' else 1,
        0 if x.get('is_best_seller') else 1,
        -x['discount']
    ))
    
    return filtered

def send_deals(deals, chat_id, status_message_id):
    global sent_products, sent_hashes, is_scanning
    
    try:
        try:
            updater.bot.delete_message(chat_id, status_message_id)
        except:
            pass
        
        if not deals:
            msg = "❌ *لا توجد عروض جديدة*\n\nجرب تاني بعدين!"
            updater.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            return
        
        stats = page_rotator.get_stats()
        
        bs = sum(1 for d in deals if d.get('is_best_seller'))
        glitch = sum(1 for d in deals if d['type'] == '🔥 GLITCH')
        warehouse = sum(1 for d in deals if d['type'] == '🏭 WAREHOUSE')
        outlet = sum(1 for d in deals if d['type'] == '🎁 OUTLET')
        lightning = sum(1 for d in deals if d['type'] == '⚡ LIGHTNING')
        
        summary = f"""
🎯 *{len(deals)} صفقة ممتازة!*

📊 *تقدم البحث:* {stats['progress_percent']:.1f}%
🔄 *دورة التدوير:* {stats['rotation_count']}

🔥 Glitch: {glitch}
🏭 Warehouse: {warehouse}
🎁 Outlet: {outlet}
⚡ Lightning: {lightning}
⭐ Best Sellers: {bs}
💰 خصومات عادية: {len(deals)-bs-glitch-warehouse-outlet-lightning}

📉 *الحد الأدنى للخصم:* {MIN_DISCOUNT}%
⭐ *الحد الأدنى للتقييم:* {MIN_RATING} نجمة
        """
        updater.bot.send_message(chat_id=chat_id, text=summary, parse_mode='Markdown')
        
        for i, d in enumerate(deals, 1):
            savings = f"💵 توفير: {d['savings']:.2f} ريال\n" if d['savings'] > 0 else ""
            old = f"🏷️ قبل: {d['old_price']:.2f} ريال\n" if d['old_price'] > 0 else ""
            rev = f"📝 {d['reviews']:,} مراجعة\n" if d['reviews'] > 0 else ""
            
            msg = f"""
{d['type']} *#{i}*

📦 {d['title'][:120]}

💵 *{d['price']:.2f} ريال*
{old}{savings}📉 خصم: {d['discount']}%
⭐ تقييم: {d['rating']}/5
{rev}📍 {d['category']}

🔗 [Amazon]({d['link']})
            """
            
            try:
                if d['image'].startswith('http'):
                    updater.bot.send_photo(chat_id=chat_id, photo=d['image'], caption=msg, parse_mode='Markdown')
                else:
                    updater.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                
                sent_products.add(d['id'])
                sent_hashes.add(create_title_hash(d['title']))
                time.sleep(1.5)
                
            except Exception as e:
                logger.error(f"Error #{i}: {e}")
                try:
                    updater.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    sent_products.add(d['id'])
                    time.sleep(1.5)
                except:
                    pass
        
        save_database()
        logger.info(f"✅ Done! Sent {len(deals)} deals. Total: {len(sent_products)}")
        
    finally:
        global stop_search_flag
        is_scanning = False
        stop_search_flag = False  # Reset for next search

def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(f"""
👋 *أهلاً بيك في Amazon Deals Bot!*

🎯 أنا ببحث في:
• 200+ قسم شامل 📁
• 500+ صفحة 📄
• *تدوير ذكي للصفحات* 🔄
• عروض Warehouse المخفية 🏭
• Outlet & Clearance 🎁
• Lightning Deals ⚡
• Prime Exclusives 👑
• Best Sellers ⭐
• براندات فاخرة 💎

🔥 *شروط العرض:*
• خصم فوق {MIN_DISCOUNT}%
• تقييم فوق {MIN_RATING} نجمة

اكتب *Hi* عشان تبدأ البحث عن 80 منتج!
    """, parse_mode='Markdown')

def hi_cmd(update: Update, context: CallbackContext):
    global is_scanning
    
    chat_id = update.effective_chat.id
    
    if is_scanning:
        update.message.reply_text("⏳ أنا ببحث دلوقتي... استنى شوية!")
        return
    
    is_scanning = True
    
    if not page_rotator.all_pages:
        page_rotator.generate_all_pages(CATEGORIES_DEF)
        page_rotator.load_state()
    
    stats = page_rotator.get_stats()
    
    status_msg = update.message.reply_text(
        f"🔍 *بدأت البحث عن {TARGET_DEALS_COUNT} صفقة مميزة...*\n"
        f"📊 التقدم: {stats['progress_percent']:.1f}%\n"
        f"🔄 الدورة: {stats['rotation_count']}\n"
        f"📉 الحد الأدنى للخصم: {MIN_DISCOUNT}%\n"
        f"⭐ الحد الأدنى للتقييم: {MIN_RATING}\n"
        f"⏱️ 5-10 دقائق", 
        parse_mode='Markdown'
    )
    
    try:
        load_database()
        deals = search_all_deals(chat_id, status_msg.message_id)
        premium = filter_premium_deals(deals)
        send_deals(premium, chat_id, status_msg.message_id)
    except Exception as e:
        logger.error(f"Search error: {e}")
        is_scanning = False
        try:
            updater.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text="❌ حصل خطأ! جرب تاني."
            )
        except:
            update.message.reply_text("❌ حصل خطأ! جرب تاني.")

def status_cmd(update: Update, context: CallbackContext):
    stats = page_rotator.get_stats()
    update.message.reply_text(f"""
📊 *حالة البوت:*

📦 منتجات مخزنة: {len(sent_products)}
🔍 بحوث متنوعة: {len(sent_hashes)}
📁 الأقسام: 200+
📄 إجمالي الصفحات: {stats['total_pages']}
✅ الصفحات المزارة: {stats['visited_pages']}
⏳ الصفحات المتبقية: {stats['remaining_pages']}
📈 نسبة التقدم: {stats['progress_percent']:.1f}%
🔄 دورة التدوير: {stats['rotation_count']}
⏰ التوقيت: {datetime.now().strftime('%H:%M:%S')}

🎯 *إعدادات البحث:*
• عدد النتائج: {TARGET_DEALS_COUNT}
• الحد الأدنى للخصم: {MIN_DISCOUNT}%
• الحد الأدنى للتقييم: {MIN_RATING}

✅ البوت شغال بكفاءة!
    """, parse_mode='Markdown')

def clear_cmd(update: Update, context: CallbackContext):
    global sent_products, sent_hashes
    sent_products.clear()
    sent_hashes.clear()
    page_rotator.visited_pages.clear()
    page_rotator.rotation_count = 0
    page_rotator.save_state()
    save_database()
    update.message.reply_text("🗑️ *تم مسح كل البيانات!*\n\nالآن البوت هيبدأ من جديد.", parse_mode='Markdown')

def reset_rotation_cmd(update: Update, context: CallbackContext):
    page_rotator.visited_pages.clear()
    page_rotator.rotation_count = 0
    page_rotator.save_state()
    update.message.reply_text("🔄 *تم إعادة تعيين التدوير!*\n\nسيبدأ البحث من الصفحة الأولى مرة أخرى.", parse_mode='Markdown')



def stop_cmd(update: Update, context: CallbackContext):
    """يوقف البحث الحالي ويبعت اللي لاقاه"""
    global stop_search_flag, is_scanning

    if not is_scanning:
        update.message.reply_text("❌ مفيش بحث شغال دلوقتي!")
        return

    stop_search_flag = True
    update.message.reply_text("🛑 *جاري إيقاف البحث...*\n\nهبعتلك اللي لاقيته حد دلوقتي!", parse_mode='Markdown')
    logger.info("🛑 User requested stop search")
def unknown(update: Update, context: CallbackContext):
    update.message.reply_text("🤔 *مش فاهم!*\n\nاكتب:\n• *Hi* للبحث عن عروض\n• /start للمساعدة\n• /status للحالة\n• /reset_rotation لإعادة التدوير", parse_mode='Markdown')

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        stats = page_rotator.get_stats()
        response = json.dumps({
            "status": "ok",
            "products": len(sent_products),
            "timestamp": datetime.now().isoformat(),
            "categories": 200,
            "pages": stats['total_pages'],
            "visited_pages": stats['visited_pages'],
            "progress": stats['progress_percent'],
            "rotation_count": stats['rotation_count'],
            "target_deals": TARGET_DEALS_COUNT,
            "min_discount": MIN_DISCOUNT,
            "min_rating": MIN_RATING
        })
        self.wfile.write(response.encode())
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    logger.info(f"🌐 Health server running on port {PORT}")
    server.serve_forever()

def main():
    global updater
    
    load_database()
    page_rotator.generate_all_pages(CATEGORIES_DEF)
    page_rotator.load_state()
    
    stats = page_rotator.get_stats()
    logger.info(f"🚀 Starting | Products: {len(sent_products)} | Pages: {stats['total_pages']} | Visited: {stats['visited_pages']}")
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    keep_alive_thread = threading.Thread(target=keep_alive_ping, daemon=True)
    keep_alive_thread.start()
    
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("clear", clear_cmd))
    dp.add_handler(CommandHandler("reset_rotation", reset_rotation_cmd))
    dp.add_handler(MessageHandler(Filters.regex(r'(?i)^hi$'), hi_cmd))
    dp.add_handler(MessageHandler(Filters.regex(r'(?i)^s$'), stop_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, unknown))
    
    logger.info("🤖 Bot starting with page rotation...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
