import datetime
import time
import requests
from bs4 import BeautifulSoup
import sqlite3

# ==================== الإعدادات الأساسية ====================
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "@your_channel_username"  # أو معرّف القناة ID

# رابط البحث الأساسي في أمازون (تأكد من تعديل البحث k= أو الفئة حسب حاجتك)
BASE_URL = "https://www.amazon.sa/s?k=deals&page="

MAX_PAGES = 10  # عدد الصفحات التي سيلف عليها السكريبت
SLEEP_BETWEEN_PAGES = 4  # الانتظار بالثواني بين كل صفحة وأخرى
SLEEP_BETWEEN_CYCLES = (
    300  # الانتظار بالثواني عند انتهاء الدورة كاملة قبل إعادة الفحص (مثلاً 5 دقائق)
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/118.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ==================== إدارة قاعدة البيانات لمنع التكرار ====================
def init_db():
    """إنشاء قاعدة بيانات خفيفة لحفظ الأجناس/المنتجات المُنشرة وتاريخ نشرها."""
    conn = sqlite3.connect("posted_products.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posted_items (
            asin TEXT PRIMARY KEY,
            title TEXT,
            last_posted_date TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def should_post(asin):
    """التحقق مما إذا كان المنتج جديداً أو مر على آخِر نشر له أكثر من 7 أيام."""
    conn = sqlite3.connect("posted_products.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_posted_date FROM posted_items WHERE asin = ?", (asin,)
    )
    row = cursor.fetchone()

    if row is None:
        # المنتج جديد تماماً ولم يُنشر من قبل
        conn.close()
        return True

    # المنتج تم نشره سابقاً، نتحقق من الفرق الزمني (أسبوع = 7 أيام)
    last_posted_str = row[0]
    last_posted_date = datetime.datetime.strptime(
        last_posted_str, "%Y-%m-%d %H:%M:%S"
    )
    now = datetime.datetime.now()

    days_passed = (now - last_posted_date).days

    conn.close()

    # إذا مر 7 أيام أو أكثر يسمح بالنشر مجدداً
    return days_passed >= 7


def record_posted(asin, title):
    """تحديث أو إضافة تاريخ النشر للمنتج."""
    conn = sqlite3.connect("posted_products.db")
    cursor = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        INSERT INTO posted_items (asin, title, last_posted_date)
        VALUES (?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET last_posted_date = excluded.last_posted_date
    """,
        (asin, title, now_str),
    )

    conn.commit()
    conn.close()


# ==================== إرسال الرسائل لتيليجرام ====================
def send_telegram_message(title, price, old_price, discount, link, image_url):
    """إرسال العرض إلى قناة تيليجرام بنسق جذاب."""
    caption = f"🔥 **عرض جديد ومميز!**\n\n"
    caption += f"📌 **المنتج:** {title}\n\n"

    if old_price:
        caption += f"💰 **السعر بعد الخصم:** {price}\n"
        caption += f"❌ **السعر السابق:** {old_price}\n"
    else:
        caption += f"💰 **السعر:** {price}\n"

    if discount:
        caption += f"🏷️ **نسبة الخصم:** {discount}\n"

    caption += f"\n🔗 **رابط الشراء Direct:**\n{link}"

    # إرسال الصورة مع النص
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "Markdown",
    }

    try:
        res = requests.post(url, data=payload, timeout=10)
        if res.status_code == 200:
            print(f"[✓] تم النشر بنجاح على تيليجرام: {title[:30]}...")
            return True
        else:
            # في حال فشل إرسال الصورة، يتم إرسال النص فقط
            url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(
                url_msg,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": caption,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            return True
    except Exception as e:
        print(f"[-] خطأ أثناء الإرسال لتيليجرام: {e}")
        return False


# ==================== الفحص واستخراج العروض ====================
def scrape_and_process():
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{page}"
        print(f"\n[+] جاري فحص الصفحة رقم: {page}...")

        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                print(
                    f"[-] تعذر فتح الصفحة {page} (كود Response: {response.status_code})"
                )
                continue

            soup = BeautifulSoup(response.content, "html.parser")
            items = soup.find_all(
                "div", {"data-component-type": "s-search-result"}
            )

            print(f"   عُثر على {len(items)} عنصر في الصفحة.")

            for item in items:
                # استخراج المعرف الخاص بالمنتج (ASIN)
                asin = item.get("data-asin")
                if not asin:
                    continue

                # التحقق هل عليه عرض أصلًا (تخطي المنتجات بدون خصم)
                # غالبًا المنتجات ذات الخصم تحتوي على سعر سابق محطوط أو وسم خصم
                discount_elem = item.find("span", class_="a-letter-space")
                price_elem = item.find("span", class_="a-price-whole")
                old_price_elem = item.find(
                    "span", class_="a-price a-text-price"
                )

                # إذا لم يكن هناك سعر للخصم نمر للمنتج التالي
                if not price_elem or not old_price_elem:
                    continue

                # استخراج تفاصيل المنتج
                title_elem = item.find(
                    "h2", class_="a-size-mini a-spacing-none a-color-base s-line-clamp-2"
                )
                if not title_elem:
                    title_elem = item.find("h2")

                title = (
                    title_elem.text.strip() if title_elem else "منتج بدون عنوان"
                )

                # استخراج السعر
                price_fraction = item.find("span", class_="a-price-fraction")
                price = (
                    f"{price_elem.text.strip()}{'.' + price_fraction.text.strip() if price_fraction else ''} ر.س"
                )
                old_price = (
                    old_price_elem.find("span", class_="a-offscreen").text.strip()
                    if old_price_elem.find("span", class_="a-offscreen")
                    else ""
                )

                # استخراج نسبة الخصم إن وجدت
                discount_tag = item.find(
                    "span", string=lambda t: t and "%" in t
                )
                discount = discount_tag.text.strip() if discount_tag else ""

                # الرابط والصورة
                link = f"https://www.amazon.sa/dp/{asin}"
                img_elem = item.find("img", class_="s-image")
                image_url = img_elem["src"] if img_elem else ""

                # --- التحقق من شرط عدم التكرار (مرور أسبوع على النشر) ---
                if should_post(asin):
                    success = send_telegram_message(
                        title, price, old_price, discount, link, image_url
                    )
                    if success:
                        record_posted(asin, title)
                        time.sleep(
                            5
                        )  # مهلة بين إرسال الرسالة والأخرى لتجنب حظر Bot
                else:
                    print(
                        f"   [تجاوز] المنتج ({asin}) تم نشره مسبقاً خلال الـ 7 أيام الماضية."
                    )

        except Exception as e:
            print(f"[-] خطأ أثناء معالجة الصفحة {page}: {e}")

        time.sleep(SLEEP_BETWEEN_PAGES)


# ==================== التشغيل الرئيسي ====================
if __name__ == "__main__":
    init_db()  # إعداد قاعدة البياناتSQLite
    print("🚀 تم تشغيل البوت بنجاح.. سيقوم باللف المستمر على الصفحات.")

    while True:
        print("\n==========================================")
        print("🔄 بدء دورة فحص جديدة لجميع الصفحات...")
        print("==========================================")

        scrape_and_process()

        print(
            f"\n[✓] اكتملت الدورة! انتظار {SLEEP_BETWEEN_CYCLES // 60} دقائق قبل إعادة الفحص من الصفحة 1..."
        )
        time.sleep(SLEEP_BETWEEN_CYCLES)
