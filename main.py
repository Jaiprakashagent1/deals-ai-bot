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

# 5. GOOGLE/SERP STYLE SEARCH SNIPPET PRICE EXTRACTION ENGINE
def fetch_price_from_search_engine(product_title: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        # Querying live search indexing for direct price snippet
        clean_query = re.sub(r'[^a-zA-Z0-9\s]', '', product_title)[:60]
        search_url = f"https://html.duckduckgo.com/html/?q={quote(clean_query + ' price india amazon flipkart')}"
        res = requests.get(search_url, headers=headers, timeout=6)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        snippets = []
        for result in soup.find_all('a', class_='result__snippet')[:5]:
            snippets.append(result.text.strip())
            
        combined_text = " ".join(snippets)
        # Extract rupees patterns indexed by search engines
        prices = re.findall(r'(?:₹|Rs\.?|INR)\s*([\d,]+)', combined_text, flags=re.IGNORECASE)
        valid_prices = [p for p in prices if len(p.replace(',', '')) >= 3]
        
        if valid_prices:
            return f"₹{valid_prices[0]}"
        return ""
    except Exception:
        return ""

# 6. Multi-Layer Enterprise Scraper Engine
def scrape_link_data(url: str, raw_user_text: str = "") -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    # Layer A: Check Raw User Input for explicit price
    user_price_match = re.search(r'(?:₹|Rs\.?|price)?\s*([\d,]{3,7})', raw_user_text, flags=re.IGNORECASE)
    manual_price = f"₹{user_price_match.group(1)}" if user_price_match and len(user_price_match.group(1)) >= 3 else ""

    clean_text_title = re.sub(r'https?://\S+', '', raw_user_text)
    clean_text_title = re.sub(r'(Take a look at this|on Flipkart|on Amazon|on Myntra|on Ajio|Check out)', '', clean_text_title, flags=re.IGNORECASE).strip()

    extracted_title = ""
    extracted_price = manual_price

    bad_keywords = ["recaptcha", "captcha", "robot check", "access denied", "security check", "blocked", "cloudflare"]

    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=8, allow_redirects=True)
        final_url = res.url

        # URL Slug Extraction
        parsed_path = urlparse(final_url).path
        slug_parts = [p for p in parsed_path.split('/') if p and not p.isdigit() and len(p) > 3]
        if slug_parts:
            candidate_slug = slug_parts[0]
            if candidate_slug not in ['dp', 'gp', 'p', 'buy', 'product', 'dl']:
                extracted_title = unquote(candidate_slug).replace('-', ' ').replace('_', ' ').title()

        soup = BeautifulSoup(res.text, 'html.parser')

        # Page Title Extraction
        page_title = soup.title.string.strip() if soup.title else ""
        if page_title and not any(bad in page_title.lower() for bad in bad_keywords):
            cleaned_page_title = re.sub(r'\s*[\|-]\s*(Amazon|Flipkart|Myntra|Ajio|Nykaa|Meesho).*', '', page_title, flags=re.IGNORECASE).strip()
            if len(cleaned_page_title) > 5:
                extracted_title = cleaned_page_title

        # Layer B: Direct Scraping Price Extraction
        if not extracted_price:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string if script.string else "")
                    if isinstance(data, list): data = data[0]
                    if isinstance(data, dict):
                        offers = data.get("offers")
                        if isinstance(offers, dict) and "price" in offers:
                            extracted_price = f"₹{offers['price']}"
                            break
                        elif isinstance(offers, list) and len(offers) > 0 and "price" in offers[0]:
                            extracted_price = f"₹{offers[0]['price']}"
                            break
                except Exception:
                    continue

        if not extracted_price:
            meta_price = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
            if meta_price and meta_price.get("content"):
                extracted_price = f"₹{meta_price['content'].strip()}"
            else:
                price_elem = soup.find("span", class_="a-price-whole") or soup.find("div", class_="_30jeq3") or soup.find("div", class_="Nx9q3U")
                if price_elem:
                    extracted_price = f"₹{price_elem.text.strip().rstrip('.')}"

        if not extracted_title or any(bad in extracted_title.lower() for bad in bad_keywords):
            extracted_title = clean_text_title if len(clean_text_title) > 3 else "Trending Deal Product"

        # Layer C: Google/SERP Snippet Search Fallback for Price
        if not extracted_price or "Check Link" in extracted_price:
            serp_price = fetch_price_from_search_engine(extracted_title)
            if serp_price:
                extracted_price = serp_price

        return {
            "url": final_url, 
            "title": extracted_title, 
            "price": extracted_price if extracted_price else "Best Market Live Deal"
        }
    except Exception:
        fallback = clean_text_title if len(clean_text_title) > 3 else "Featured Deal Product"
        serp_price = fetch_price_from_search_engine(fallback)
        return {
            "url": url, 
            "title": fallback, 
            "price": serp_price if serp_price else "Best Market Live Deal"
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

# 8. AI Master Prompt Generator - PRASAD TECH STYLE & ZERO-HALLUCINATION
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    badge_header = ""
    if deal_type == "BYPASS":
        badge_header = "🔥 **PREMIUM VERIFIED DEAL** 🤯"
    elif deal_type == "SPONSORED":
        badge_header = "📢 **SPONSORED SPECIAL PROMOTION** 🤩"
    else:
        badge_header = "🤯 **UNBELIEVABLE PRICE DROP ALERT** 😱"

    return f"""
    You are a viral Tech Creator & Master AI Agent For Deals (style of Prasad Tech in Telugu).
    Generate a high-converting, highly energetic English Telegram deal post based on:
    - Verified Product Name: {scraped_info.get('title', '')}
    - Real Live Price Tag: {scraped_info.get('price', 'Best Market Live Deal')}
    - Buy Link: {scraped_info.get('url', '')}

    STRICT HIGH-CONVERTING DIRECTIVES:
    1. HEADER BADGE: Start with {badge_header}
    2. PRODUCT TITLE: Format in **Bold** with exciting Face Emojis and Product Emojis (e.g., 🤩, 🤯, 📱, 🎧, ⚡).
    3. PRICE HOOK: Show the Live Price Tag clearly (e.g., "💰 **Deal Price:** {scraped_info.get('price', 'Best Market Live Deal')} 📉").
       - CRITICAL: Do NOT put URLs in the price section!
    4. HIGHLIGHTS: Provide 3 crisp, exciting bullet points using 🤩, 🔥, or ✅ based strictly on verified specs.
    5. CALL TO ACTION (ONLY URL LOCATION): Output EXACTLY ONE buy link at the very end (e.g., "🛒 **Grab This Crazy Deal Now:** [Link]").
       - NEVER repeat URLs elsewhere!
    6. MASTER CATEGORY HASHTAGS: Pick 1 or 2 ACCURATE hashtags ONLY from this official list:
       [#Automobile, #Electronics, #Fashion, #Furniture, #Home_Kitchen, #Beauty, #Health_PersonalCare, #Medical, #Grocery, #Toys_Games, #Sports_Fitness, #Baby_Kids, #Luggage_Travel, #Books_Stationery].
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
