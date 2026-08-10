import os
import re
import json
import sqlite3
import requests
import threading
import http.server
import socketserver
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ==============================================================================
# 1. CLOUD SERVER KEEP-ALIVE (Render Web Service Support)
# ==============================================================================
def run_health_server():
    """Starts a dummy HTTP server so Render doesn't shut down the bot."""
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ==============================================================================
# 2. ENVIRONMENT VARIABLES & CONFIGURATION
# ==============================================================================
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_API_KEY_BACKUP = os.environ.get("GROQ_API_KEY_BACKUP", "").strip()
ADMIN_CHAT_ID       = os.environ.get("ADMIN_CHAT_ID", "").strip()
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "").strip()
DB_PATH             = os.environ.get("DB_PATH", "deals_bot.db").strip()
PRICE_CHECK_MINUTES = int(os.environ.get("PRICE_CHECK_MINUTES", "60"))

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise SystemExit("❌ CRITICAL ERROR: TELEGRAM_TOKEN or GROQ_API_KEY is missing!")

# Initialize AI Clients
primary_ai = Groq(api_key=GROQ_API_KEY)
backup_ai  = Groq(api_key=GROQ_API_KEY_BACKUP) if GROQ_API_KEY_BACKUP else None

# The 14 Official Categories
CATEGORIES = [
    "#Automobile", "#Electronics", "#Fashion", "#Furniture", "#Home_Kitchen",
    "#Beauty", "#Health_PersonalCare", "#Medical", "#Grocery", "#Toys_Games",
    "#Sports_Fitness", "#Baby_Kids", "#Luggage_Travel", "#Books_Stationery"
]

# ==============================================================================
# 3. DATABASE MODULE (SQLite for Price Tracking)
# ==============================================================================
def setup_database():
    """Initializes the database table for tracking price drops."""
    with sqlite3.connect(DB_PATH) as conn:
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

setup_database()

# ==============================================================================
# 4. ADVANCED SCRAPING & DATA EXTRACTION ENGINE
# ==============================================================================
def resolve_final_url(url: str) -> str:
    """Expands short URLs (like dl.flipkart.com) to their full original URLs."""
    try:
        res = requests.head(url, allow_redirects=True, timeout=5)
        if res.url and res.url != url:
            return res.url
        res = requests.get(url, allow_redirects=True, timeout=5, stream=True)
        return res.url
    except Exception:
        return url

def extract_share_text_details(raw_text: str) -> tuple[str, str]:
    """Extracts product name and price if the user forwards a message from an app."""
    title, price = "", ""
    if not raw_text: return title, price

    # Find Flipkart specific share format
    fk_match = re.search(r'Take a look at this\s+(.*?)\s+on Flipkart', raw_text, re.IGNORECASE)
    if fk_match:
        title = fk_match.group(1).strip()
    else:
        # Generic match for any text before the URL
        generic_match = re.match(r'^(.*?)\s*https?://\S+', raw_text.strip(), re.IGNORECASE | re.DOTALL)
        if generic_match and len(generic_match.group(1).strip()) > 3:
            title = generic_match.group(1).strip()

    # Find Price in text
    price_match = re.search(r'(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d{1,2})?)', raw_text, re.IGNORECASE)
    if price_match:
        price = f"₹{price_match.group(1)}"

    return title, price

def get_live_price(html: str, soup: BeautifulSoup) -> str:
    """Multi-layered approach to find the accurate live deal price."""
    # 1. Check Flipkart JS Data
    for pattern in [r'"finalPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)', r'"minPrice"\s*:\s*(\d+)']:
        match = re.search(pattern, html)
        if match: return f"₹{int(match.group(1)):,}"
    
    # 2. Check Meta Tags
    meta = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
    if meta and meta.get("content"): return f"₹{meta['content'].strip()}"
    
    # 3. Check JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                offers = item.get("offers", {})
                if isinstance(offers, list): offers = offers[0]
                if isinstance(offers, dict) and offers.get("price"):
                    return f"₹{offers['price']}"
        except Exception:
            continue
            
    return ""

def scrape_product_info(url: str, user_message: str) -> dict:
    """Master function to gather all product details safely."""
    share_title, share_price = extract_share_text_details(user_message)
    final_url = resolve_final_url(url)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }
    
    try:
        response = requests.get(final_url, headers=headers, timeout=8)
        soup = BeautifulSoup(response.text, "html.parser")
        final_url = response.url # Update again just in case

        # Extract Title
        og_title = soup.find("meta", property="og:title")
        raw_title = og_title["content"].strip() if og_title else (soup.title.string.strip() if soup.title else "")
        
        # Clean Title
        clean_title = re.sub(r'\s*[\|\-–]\s*(Amazon|Flipkart|Myntra|Ajio|Nykaa).*', '', raw_title, flags=re.IGNORECASE).strip()
        if not clean_title or re.match(r'^[!\W]*[A-Za-z0-9]{8,}$', clean_title):
            clean_title = share_title or "Featured Deal Product"

        # Extract Description
        og_desc = soup.find("meta", property="og:description")
        desc = og_desc["content"].strip() if og_desc else ""

        # Extract Price (Live price prioritised over share text price)
        live_price = get_live_price(response.text, soup)
        final_price = live_price if live_price else share_price

        return {
            "url": final_url,
            "title": clean_title,
            "price": final_price if final_price else "Check Link",
            "desc": desc
        }
    except Exception:
        return {
            "url": final_url,
            "title": share_title or "Featured Deal Product",
            "price": share_price or "Check Link",
            "desc": ""
        }

# ==============================================================================
# 5. AI FORMATTING ENGINE (Groq)
# ==============================================================================
def format_deal_with_ai(product_data: dict, extra_notes: str, deal_type: str) -> str:
    """Generates the Prasad Tech in Telugu 4-line format using Groq AI."""
    prompt = f"""You are an automated deal poster. Format the output STRICTLY like 'Prasad Tech in Telugu'.

INPUT DATA:
- Title: {product_data.get('title')}
- Price: {product_data.get('price')}
- Link: {product_data.get('url')}
- Specs: {product_data.get('desc')}
- User Notes: {extra_notes}
- Type: {deal_type}

OUTPUT TEMPLATE:
🔥🔥 [Full Product Title with Main Specs]

🎁 Deal Price : {product_data.get('price')}

🔍 Cross Platform Price : Best Competitive Offer Across Market

Buy Here : {product_data.get('url')}

💥 Bank Offer : [Extract bank offer if present, else remove this line]

[Exactly ONE category hashtag from this list: {", ".join(CATEGORIES)}]

RULES: No intro, no conversational text. The link MUST be printed exactly once."""

    last_error = None
    for ai_client in [primary_ai, backup_ai]:
        if not ai_client: continue
        try:
            res = ai_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            last_error = e
            
    return f"❌ AI Generation Failed: {last_error}"

# ==============================================================================
# 6. TELEGRAM BOT HANDLERS
# ==============================================================================
async def process_deal_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, deal_type="NORMAL"):
    """Core pipeline: Extract URL -> Scrape -> AI Format -> Reply & Post."""
    url_match = re.search(r'https?://\S+', user_text)
    
    if not url_match:
        await update.message.reply_text("⚠️ No valid link found in your message.")
        return

    try:
        # 1. Scrape Info
        scraped_data = scrape_product_info(url_match.group(0), user_text)
        
        # 2. Format with AI
        formatted_post = format_deal_with_ai(scraped_data, user_text, deal_type)
        
        # 3. Reply to Admin
        await update.message.reply_text(formatted_post)
        
        # 4. Auto-Post to Channel
        if CHANNEL_ID:
            try:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=formatted_post)
            except Exception as e:
                await update.message.reply_text(f"⚠️ Channel Post Failed: {e}")
                
    except Exception as e:
        await update.message.reply_text(f"❌ Processing Error: {e}")

async def handle_standard_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_deal_request(update, context, update.message.text, "NORMAL")

async def cmd_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_CHAT_ID: return
    await process_deal_request(update, context, " ".join(context.args), "BYPASS_PREMIUM")

async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets a price alert in SQLite."""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /track [Link] [TargetPrice]")
        return
    try:
        link = context.args[0]
        price = float(context.args[1].replace(",", ""))
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO price_alerts (user_id, product_link, target_price) VALUES (?, ?, ?)", 
                         (update.effective_user.id, link, price))
            
        await update.message.reply_text(f"✅ Alert set! Will notify when price drops to ₹{price:.2f}")
    except Exception:
        await update.message.reply_text("❌ Invalid price format.")

# ==============================================================================
# 7. BACKGROUND JOB (Price Drop Monitor)
# ==============================================================================
async def background_price_checker(context: ContextTypes.DEFAULT_TYPE):
    """Checks tracked products every hour for price drops."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        active_alerts = conn.execute("SELECT * FROM price_alerts WHERE status = 'ACTIVE'").fetchall()

    for alert in active_alerts:
        try:
            data = scrape_product_info(alert["product_link"], "")
            digits = re.sub(r'[^\d.]', '', data.get("price", ""))
            current_price = float(digits) if digits else None

            if current_price and current_price <= alert["target_price"]:
                msg = f"🔥 PRICE DROP ALERT!\n\n{data['title']}\nNow: ₹{current_price}\nLink: {data['url']}"
                await context.bot.send_message(chat_id=alert["user_id"], text=msg)
                
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("UPDATE price_alerts SET status = 'TRIGGERED' WHERE id = ?", (alert["id"],))
        except Exception:
            continue

# ==============================================================================
# 8. MAIN APPLICATION RUNNER
# ==============================================================================
if __name__ == "__main__":
    print("🚀 Starting Deals AI Agent...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands & Handlers
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("✅ Deals Bot is Online!")))
    app.add_handler(CommandHandler("bypass", cmd_bypass))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_standard_message))

    # Start Background Job Scheduler
    app.job_queue.run_repeating(background_price_checker, interval=PRICE_CHECK_MINUTES * 60, first=60)

    app.run_polling()
