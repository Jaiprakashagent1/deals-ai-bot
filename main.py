import os
import re
import json
import sqlite3
import requests
import http.server
import socketserver
import threading
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

# 5. Universal E-Commerce Price & Metadata Scraper (Amazon, Flipkart, Myntra, Ajio, Meesho, Nykaa)
def scrape_link_data(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Extract Product Title
        title = soup.title.string.strip() if soup.title else "Deal Product"
        
        extracted_price = "Not Found"
        
        # Method A: JSON-LD Structured Data Parsing (Most Reliable across Stores)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string if script.string else "")
                if isinstance(data, list):
                    data = data[0]
                if isinstance(data, dict):
                    offers = data.get("offers")
                    if isinstance(offers, dict) and "price" in offers:
                        extracted_price = str(offers["price"])
                        break
                    elif isinstance(offers, list) and len(offers) > 0 and "price" in offers[0]:
                        extracted_price = str(offers[0]["price"])
                        break
            except Exception:
                continue

        # Method B: OpenGraph & Product Meta Tags (Ajio, Nykaa, Flipkart, Myntra)
        if extracted_price == "Not Found":
            meta_price = (
                soup.find("meta", property="product:price:amount") or 
                soup.find("meta", property="og:price:amount") or 
                soup.find("meta", name="twitter:data1")
            )
            if meta_price and meta_price.get("content"):
                extracted_price = meta_price["content"].strip()

        # Method C: Site-Specific CSS Selectors Fallback
        if extracted_price == "Not Found":
            # Amazon Selectors
            price_elem = soup.find("span", class_="a-price-whole") or soup.find("span", id="priceblock_ourprice") or soup.find("span", class_="a-offscreen")
            if price_elem:
                extracted_price = price_elem.text.strip()
            else:
                # Flipkart / Meesho Selectors
                fk_price = soup.find("div", class_="_30jeq3") or soup.find("div", class_="Nx9q3U") or soup.find("h8", class_="iBAtLg")
                if fk_price:
                    extracted_price = fk_price.text.strip()
                else:
                    # Ajio / Nykaa Selectors
                    fashion_price = soup.find("div", class_="prod-sp") or soup.find("span", class_="css-1jcz222")
                    if fashion_price:
                        extracted_price = fashion_price.text.strip()

        return {"url": res.url, "title": title, "price": extracted_price}
    except Exception:
        return {"url": url, "title": "Deal Product", "price": "Not Found"}

# 6. Multi-Key API Fallback & Critical Admin Alert System
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
                temperature=0.1, # Lowest temperature for absolute factual accuracy & Zero Hallucination
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

# 7. AI Master Prompt Generator with ZERO Hallucination & 14 Master Categories
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    badge_rule = ""
    if deal_type == "BYPASS":
        badge_rule = "Include Badge: '🔥 PREMIUM VERIFIED DEAL'"
    elif deal_type == "SPONSORED":
        badge_rule = "Include Badge: '📢 SPONSORED PROMOTION'"
    else:
        badge_rule = "Include dynamic smart badges like '📉 PRICE DROPPED!' or '⚡ FLASH DEAL' if discount is clear."

    return f"""
    You are the Master AI Agent For Deals.
    Generate a high-converting, clean, professional English Telegram deal post based on:
    - Raw User Input: {user_input}
    - Scraped Product Title: {scraped_info.get('title', '')}
    - Scraped Live Price: {scraped_info.get('price', 'Not Found')}
    - Product Link: {scraped_info.get('url', '')}

    STRICT ZERO-HALLUCINATION DIRECTIVES:
    1. NEVER INVENT OR GUESS DATA: Do NOT fabricate unverified MRPs, fake discount percentages, fake bank offers, or unverified technical specs. Use ONLY real facts present in the input or scraped price data.
    2. STRICT PRICE COMPARISON RULE: Display the official Live Deal Price based on scraped data. NEVER fabricate competitor prices across stores (e.g., Do NOT invent fake prices for Ajio, Flipkart, Meesho, or Nykaa unless exact scraped price data is provided).
    3. {badge_rule}
    4. Format Product Title clearly in **Bold**.
    5. Provide 3 crisp, strictly accurate Bullet Highlights based ONLY on verified product details.
    6. OFFICIAL 14 MASTER CATEGORIES: Select ONLY 1 or 2 most accurate hashtags strictly from this exact list:
       [#Automobile, #Electronics, #Fashion, #Furniture, #Home_Kitchen, #Beauty, #Health_PersonalCare, #Medical, #Grocery, #Toys_Games, #Sports_Fitness, #Baby_Kids, #Luggage_Travel, #Books_Stationery].
       - Example 1: Bike/Car accessories or helmets = #Automobile
       - Example 2: Kids toys/RC cars = #Toys_Games
       - Example 3: Travel bags/Luggage = #Luggage_Travel
       - Example 4: Grooming/Massage guns = #Health_PersonalCare or #Medical
    7. Clear Call-To-Action (CTA) link with clean layout and relevant emojis.
    """

# 8. Telegram Bot Command & Message Handlers
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
