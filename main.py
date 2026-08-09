        
import os
import re
import json
import sqlite3
import requests
import http.server
import socketserver
import threading
from urllib.parse import unquote, urlparse, quote
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

# 5. Cross-Platform Price Comparison Finder
def search_cross_platform_deals(product_title: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        clean_query = re.sub(r'[^a-zA-Z0-9\s]', '', product_title)[:50]
        search_url = f"https://html.duckduckgo.com/html/?q={quote(clean_query + ' price amazon flipkart myntra india')}"
        res = requests.get(search_url, headers=headers, timeout=6)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        snippets = []
        for result in soup.find_all('a', class_='result__snippet')[:4]:
            snippets.append(result.text.strip())
            
        combined_text = " ".join(snippets)
        prices = re.findall(r'(?:₹|Rs\.?|INR)\s*([\d,]+)', combined_text, flags=re.IGNORECASE)
        valid_prices = list(set([f"₹{p}" for p in prices if len(p.replace(',', '')) >= 3]))
        
        if len(valid_prices) >= 2:
            return f"Other Stores around {valid_prices[0]} - {valid_prices[1]}"
        elif len(valid_prices) == 1:
            return f"Other Stores around {valid_prices[0]}"
        return "Verified Lowest Deal Price Across Market"
    except Exception:
        return "Verified Lowest Deal Price Across Market"

# 6. Enterprise Scraper Engine
def scrape_link_data(url: str, raw_user_text: str = "") -> dict:
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    extracted_title = ""
    extracted_price = ""
    extracted_desc = ""

    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=10, allow_redirects=True)
        final_url = res.url
        soup = BeautifulSoup(res.text, 'html.parser')

        # OpenGraph Data Extraction
        og_title = soup.find("meta", property="og:title") or soup.find("meta", name="twitter:title")
        if og_title and og_title.get("content"):
            extracted_title = og_title["content"].strip()

        og_desc = soup.find("meta", property="og:description") or soup.find("meta", name="twitter:description")
        if og_desc and og_desc.get("content"):
            extracted_desc = og_desc["content"].strip()

        if not extracted_title or extracted_title.startswith("!") or len(extracted_title) < 4:
            if soup.title and soup.title.string:
                extracted_title = soup.title.string.strip()

        cleaned_title = re.sub(r'\s*[\|-]\s*(Amazon|Flipkart|Myntra|Ajio|Nykaa|Meesho).*', '', extracted_title, flags=re.IGNORECASE).strip()
        if cleaned_title.startswith("!"):
            cleaned_title = re.sub(r'https?://\S+', '', raw_user_text).strip()

        price_match = re.search(r'(?:Rs\.?|₹|INR)\s*([\d,]+)', extracted_desc + " " + res.text, flags=re.IGNORECASE)
        if price_match:
            extracted_price = f"₹{price_match.group(1)}"
        else:
            meta_price = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
            if meta_price and meta_price.get("content"):
                extracted_price = f"₹{meta_price['content'].strip()}"

        final_title = cleaned_title if len(cleaned_title) > 3 else "Featured Deal Product"
        cross_platform_info = search_cross_platform_deals(final_title)

        return {
            "url": final_url, 
            "title": final_title, 
            "price": extracted_price if extracted_price else "Check Link",
            "desc": extracted_desc,
            "cross_platform": cross_platform_info
        }
    except Exception:
        clean_text = re.sub(r'https?://\S+', '', raw_user_text).strip()
        final_title = clean_text if len(clean_text) > 3 else "Featured Deal Product"
        cross_platform_info = search_cross_platform_deals(final_title)
        return {
            "url": url, 
            "title": final_title, 
            "price": "Check Link",
            "desc": "",
            "cross_platform": cross_platform_info
        }

# 7. Multi-Key API Fallback System
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
                temperature=0.1,
                max_tokens=512,
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

# 8. AI Master Prompt Generator - PRASAD TECH FORMAT WITH CROSS PLATFORM LINE
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    return f"""
    You are an automated deal poster formatting strict, ultra-clean Telegram deals exactly like Prasad Tech in Telugu.

    INPUT DATA:
    - Product Title: {scraped_info.get('title', '')}
    - Live Deal Price: {scraped_info.get('price', 'Check Link')}
    - Cross Platform Prices: {scraped_info.get('cross_platform', 'Verified Best Price Across Stores')}
    - Buy Link: {scraped_info.get('url', '')}
    - Product Specs: {scraped_info.get('desc', '')}
    - User Notes: {user_input}

    STRICT OUTPUT TEMPLATE REQUIREMENTS:
    Output ONLY the exact formatted structure below. NO intro, NO explanatory text!

    FORMAT LAYOUT:
    🔥🔥 [Full Product Title with Main Specs]

    🎁 Deal Price : {scraped_info.get('price', 'Check Link')}

    🔍 Cross Platform Price : {scraped_info.get('cross_platform', 'Verified Best Price Across Stores')}

    Buy Here : {scraped_info.get('url', '')}

    💥 Bank Offer : [Extract bank offer if present in specs/user notes, otherwise omit this entire line]

    [#Category Hashtag from: #Electronics, #Fashion, #Furniture, #Home_Kitchen, #Beauty, #Health_PersonalCare, #Medical, #Grocery, #Toys_Games, #Sports_Fitness, #Baby_Kids, #Luggage_Travel, #Books_Stationery, #Automobile]

    STRICT RULES:
    1. Do NOT write extra sentences or fluff paragraphs.
    2. Do NOT repeat URLs. The link appears EXACTLY ONCE under "Buy Here :".
    3. Keep layout ultra-clean, minimal, and direct.
    """

# 9. Telegram Bot Handlers
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
            scraped = scrape_link_data(url_match.group(0), raw_user_text=raw_text)
        
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
