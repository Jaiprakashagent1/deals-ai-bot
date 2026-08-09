import os
import re
import json
import sqlite3
import requests
import http.server
import socketserver
import threading
from urllib.parse import unquote
from bs4 import BeautifulSoup
from groq import Groq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 1. Dummy Port Server for Render Cloud Platform
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# 2. Environment Variables Retrieval
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY_1 = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_BACKUP", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

# 3. SQLite Database Initialization for Price Alerts
def init_db():
    conn = sqlite3.connect('deals_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_link TEXT,
            target_price REAL,
            status TEXT DEFAULT 'ACTIVE'
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# 4. Security Verification Helper (Admin Authorization Lock)
def is_admin(user_id: int) -> bool:
    if not ADMIN_CHAT_ID:
        return True
    return str(user_id) == str(ADMIN_CHAT_ID)

# 5. Bulletproof Link Scraper & Title/Price Extractor
def scrape_link_data(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=8, allow_redirects=True)
        final_url = res.url

        # A. Smart Title Extraction directly from Amazon / Flipkart URL Slug
        extracted_title = ""
        if "amazon" in final_url or "amzn" in final_url:
            match = re.search(r'amazon\.in/([^/]+)/(?:dp|gp/product)/', final_url)
            if match:
                extracted_title = unquote(match.group(1)).replace('-', ' ').title()
        elif "flipkart" in final_url:
            match = re.search(r'flipkart\.com/([^/]+)/p/', final_url)
            if match:
                extracted_title = unquote(match.group(1)).replace('-', ' ').title()

        soup = BeautifulSoup(res.text, 'html.parser')

        # Fallback to HTML Title if URL Slug fails
        if not extracted_title or len(extracted_title) < 5:
            if soup.title and "Amazon" not in soup.title.string and "Robot Check" not in soup.title.string:
                extracted_title = soup.title.string.strip()
            else:
                extracted_title = "Featured Product Deal"

        # B. Real Live Price Extraction
        extracted_price = "Check Link for Live Price"

        # Try Meta tags (OpenGraph / Schema)
        meta_price = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
        if meta_price and meta_price.get("content"):
            extracted_price = f"₹{meta_price['content'].strip()}"
        else:
            # Selector Fallbacks
            price_elem = soup.find("span", class_="a-price-whole") or soup.find("span", id="priceblock_ourprice")
            if price_elem:
                extracted_price = f"₹{price_elem.text.strip().rstrip('.')}"
            else:
                fk_price = soup.find("div", class_="_30jeq3") or soup.find("div", class_="Nx9q3U")
                if fk_price:
                    extracted_price = fk_price.text.strip()

        return {"url": final_url, "title": extracted_title, "price": extracted_price}
    except Exception:
        return {"url": url, "title": "Featured Product Deal", "price": "Check Link for Live Price"}

# 6. Multi-Key API Fallback System
async def call_groq_ai(prompt: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    keys = [k for k in [GROQ_API_KEY_1, GROQ_API_KEY_2] if k]
    if not keys:
        raise Exception("No Groq API keys found in Environment Variables!")

    for idx, key in enumerate(keys):
        try:
            client = Groq(api_key=key)
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if idx == len(keys) - 1:
                if ADMIN_CHAT_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=int(ADMIN_CHAT_ID),
                            text=f"⚠️ **CRITICAL BOT ALERT**\nGroq API Failure: {str(e)}"
                        )
                    except Exception:
                        pass
                raise e

# 7. AI Master Prompt Generator - HIGHLY ATTRACTIVE & VIRAL TELEGRAM FORMATTING
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    badge_header = ""
    if deal_type == "BYPASS":
        badge_header = "🔥 **PREMIUM VERIFIED DEAL** 🔥"
    elif deal_type == "SPONSORED":
        badge_header = "📢 **SPONSORED SPECIAL PROMOTION** 📢"
    else:
        badge_header = "🔥 **MEGA PRICE DROP ALERT** 💥"

    return f"""
    You are the Master AI Agent For Deals creating viral, ultra-attractive Telegram deal posts.
    Create a high-converting, heavily styled English Telegram post for:
    - Verified Product Title: {scraped_info.get('title', '')}
    - Live Price: {scraped_info.get('price', 'Check Link for Price')}
    - Link: {scraped_info.get('url', '')}
    - User Notes: {user_input}

    STRICT ATTRACTIVE FORMATTING RULES:
    1. HEADER BADGE: Start with {badge_header}
    2. PRODUCT TITLE: Format in **Bold** with relevant emoji (e.g., 🎁, ⚡, 🎧, 📱, 💆‍♂️).
    3. PRICE SECTION:
       - Show Deal Price clearly with 💥 or 📉 emojis.
       - Include Bank/Card offer note if applicable.
    4. HIGHLIGHTS: Provide 3 exciting, crisp bullet points using 🔥 or ✅ emojis.
    5. CALL TO ACTION: Add an irresistible Buy Link button/CTA with 🛒 🎁 ⚡ emojis (e.g., "🛒 **Grab This Deal Now:** [Link]").
    6. CATEGORY HASHTAGS: Pick 1 or 2 ACCURATE hashtags ONLY from this official list:
       [#Automobile, #Electronics, #Fashion, #Furniture, #Home_Kitchen, #Beauty, #Health_PersonalCare, #Medical, #Grocery, #Toys_Games, #Sports_Fitness, #Baby_Kids, #Luggage_Travel, #Books_Stationery].
       - CRITICAL: Massage guns / Grooming / Massagers MUST be tagged as #Health_PersonalCare or #Medical. Do NOT tag as #Home_Kitchen.
    """

# 8. Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 **AI Agent For Deals is Active!**\n\n"
        "• Send any product link or details directly to generate formatted deals.\n"
        "• `/track [Link] [Price]` - Set Personal Price Drop Alert\n"
        "• `/bypass [Link]` - Admin Override for Premium Deals\n"
        "• `/sponsored [Link]` - Admin Sponsored Promotion"
    )

async def handle_deal_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, deal_type: str = "NORMAL"):
    try:
        url_match = re.search(r'https?://\S+', raw_text)
        scraped = {}
        if url_match:
            scraped = scrape_link_data(url_match.group(0))
        
        prompt = build_master_prompt(raw_text, scraped, deal_type)
        ai_response = await call_groq_ai(prompt, context)
        await update.message.reply_text(ai_response)
    except Exception as e:
        await update.message.reply_text(f"❌ Error processing deal: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_deal_request(update, context, update.message.text, deal_type="NORMAL")

async def bypass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized: Restricted to Bot Admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /bypass [Product Link/Details]")
        return
    await handle_deal_request(update, context, " ".join(context.args), deal_type="BYPASS")

async def sponsored_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized: Restricted to Bot Admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /sponsored [Product Link/Details]")
        return
    await handle_deal_request(update, context, " ".join(context.args), deal_type="SPONSORED")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /track [Product Link] [Target Price]")
        return
    try:
        link = context.args[0]
        target_price = float(context.args[1])
        user_id = update.effective_user.id
        
        conn = sqlite3.connect('deals_bot.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO price_alerts (user_id, product_link, target_price) VALUES (?, ?, ?)',
            (user_id, link, target_price)
        )
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ Price Alert Saved for ₹{target_price:.2f}")
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numerical target price.")

if __name__ == '__main__':
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bypass", bypass_command))
    app.add_handler(CommandHandler("sponsored", sponsored_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()
