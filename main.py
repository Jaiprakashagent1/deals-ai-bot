            import os
import re
import json
import sqlite3
import requests
import http.server
import socketserver
import threading
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------------------------------------------------------------------
# 1. Dummy HTTP Server for Render Cloud Platform (Keeps Service Alive)
# ---------------------------------------------------------------------------
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# ---------------------------------------------------------------------------
# 2. Environment Variables Retrieval
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "").strip()
ADMIN_CHAT_ID       = os.environ.get("ADMIN_CHAT_ID", "").strip()
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "").strip()
DB_PATH             = os.environ.get("DB_PATH", "deals_bot.db").strip()
PRICE_CHECK_MINUTES = int(os.environ.get("PRICE_CHECK_MINUTES", "60"))

if not TELEGRAM_TOKEN:
    raise SystemExit("CRITICAL ERROR: TELEGRAM_TOKEN is not set in Environment Variables!")
if not GROQ_API_KEY:
    raise SystemExit("CRITICAL ERROR: GROQ_API_KEY is not set in Environment Variables!")

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# 3. Official 14 Categories List
# ---------------------------------------------------------------------------
CATEGORIES = [
    "#Automobile", "#Electronics", "#Fashion", "#Furniture", "#Home_Kitchen",
    "#Beauty", "#Health_PersonalCare", "#Medical", "#Grocery", "#Toys_Games",
    "#Sports_Fitness", "#Baby_Kids", "#Luggage_Travel", "#Books_Stationery",
]

# ---------------------------------------------------------------------------
# 4. SQLite Database Engine (Price Drop Tracking)
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_link TEXT NOT NULL,
            target_price REAL NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def is_admin(user_id: int) -> bool:
    if not ADMIN_CHAT_ID:
        return True
    return str(user_id) == str(ADMIN_CHAT_ID)

# ---------------------------------------------------------------------------
# 5. Advanced Data & Price Extraction Engine
# ---------------------------------------------------------------------------
JUNK_TITLE_PATTERN = re.compile(r'^[!\W]*[A-Za-z0-9]{8,}$')
BRAND_SUFFIX_PATTERN = re.compile(
    r'\s*[\|\-–]\s*(Amazon(\.in)?|Flipkart|Myntra|Ajio|Nykaa|Meesho|Tata\s*CLiQ).*',
    flags=re.IGNORECASE
)

def parse_app_share_text(raw_text: str) -> tuple:
    """Flipkart / Amazon యాప్ షేర్ మెసేజ్ల నుండి ప్రొడక్ట్ టైటిల్ & ప్రైస్‌ను పట్టుకునే ఫంక్షన్."""
    title = ""
    price = ""
    
    # Match 'Take a look at this <Title> on Flipkart'
    fk_match = re.search(r'Take a look at this\s+(.*?)\s+on Flipkart', raw_text, flags=re.IGNORECASE)
    if fk_match:
        title = fk_match.group(1).strip()

    price_match = re.search(r'(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d{1,2})?)', raw_text, flags=re.IGNORECASE)
    if price_match:
        price = f"₹{price_match.group(1)}"

    return title, price

def extract_title_from_url_slug(url: str) -> str:
    try:
        path = unquote(urlparse(url).path)
        parts = [p for p in path.split('/') if p]
        for part in parts:
            if part.lower() not in ['p', 's', 'dl', 'dp', 'gp'] and not part.startswith('itm') and len(part) > 5:
                clean_name = re.sub(r'[-_]', ' ', part).title().strip()
                if not any(x in clean_name.lower() for x in ['flipkart', 'amazon', 'myntra', 'buy', 'online']):
                    return clean_name
    except Exception:
        pass
    return ""

def clean_title(raw_title: str, final_url: str, raw_user_text: str) -> str:
    share_title, _ = parse_app_share_text(raw_user_text)
    if share_title:
        return share_title

    title = (raw_title or "").strip()
    title = BRAND_SUFFIX_PATTERN.sub('', title).strip()

    if not title or JUNK_TITLE_PATTERN.match(title) or len(title) < 4:
        title = extract_title_from_url_slug(final_url)

    if not title:
        fallback = re.sub(r'https?://\S+', '', raw_user_text or "").strip()
        title = fallback if len(fallback) > 3 else ""

    return title if title else "Featured Deal Product"

def extract_flipkart_price(html_text: str) -> str:
    patterns = [
        r'"finalPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
        r'"minPrice"\s*:\s*(\d+)',
        r'"pricing"\s*:\s*\{\s*"finalPrice"\s*:\s*(\d+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if match:
            val = int(match.group(1))
            return f"₹{val:,}"
    return ""

def extract_price_from_jsonld(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):

🔥🔥 
