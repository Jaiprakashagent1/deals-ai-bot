import os
import re
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
        return True  # Allows access during initial setup if ADMIN_CHAT_ID is not set yet
    return str(user_id) == str(ADMIN_CHAT_ID)

# 5. Link Scraper & Metadata Extractor
def scrape_link_data(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.title.string.strip() if soup.title else "Deal Product"
        return {"url": res.url, "title": title}
    except Exception:
        return {"url": url, "title": "Deal Product"}

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
                temperature=0.6,
                max_tokens=1024,
            )
            return completion.choices[0].message.content
        except Exception as e:
            # Send immediate alert to Admin if all API keys fail
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

# 7. AI Master Prompt Generator
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    badge_rule = ""
    if deal_type == "BYPASS":
        badge_rule = "Include Badge: '🔥 PREMIUM VERIFIED DEAL'"
    elif deal_type == "SPONSORED":
        badge_rule = "Include Badge: '📢 SPONSORED PROMOTION'"
    else:
        badge_rule = "Include dynamic smart badges like '📉 PRICE DROPPED!' or '⚡ FLASH DEAL' if applicable."

    return f"""
    You are the Master AI Agent For Deals.
    Generate a high-converting, clean, professional English Telegram deal post based on:
    - User Request / Input: {user_input}
    - Scraped Product Title: {scraped_info.get('title', '')}
    - Product Link: {scraped_info.get('url', '')}

    Follow these STRICT MASTER SPECIFICATIONS:
    1. {badge_rule}
    2. Format Product Title clearly in **Bold**.
    3. Multi-Store Price Comparison Breakdown:
       - Show Original MRP vs Deal Price (and Discount %)
       - Compare prices across major stores (Amazon vs Flipkart vs Myntra) and highlight the Lowest Price platform.
       - Include Bank/Credit Card Offers (Estimate HDFC/ICICI/SBI if mentioned).
       - Final Effective Price.
    4. Provide 3 Bullet Highlights of the product.
    5. AUTO CATEGORY TAGGING: Append exact hashtags at the very bottom:
       (Select appropriate ones: #Electronics, #Fashion, #Furniture, #Beauty, #Grocery_Medical, #HomeDecor).
    6. Include clear Call-To-Action (CTA) with the link. Ensure clean layout with relevant emojis.
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
        await update.message.reply_text("⛔ Unauthorized: This command is restricted to Bot Admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /bypass [Product Link/Details]")
        return
    await handle_deal_request(update, context, " ".join(context.args), deal_type="BYPASS")

async def sponsored_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized: This command is restricted to Bot Admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /sponsored [Product Link/Details]")
        return
    await handle_deal_request(update, context, " ".join(context.args), deal_type="SPONSORED")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /track [Product Link] [Target Price]\nExample: /track https://amzn.in/... 9500")
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
        
        await update.message.reply_text(f"✅ Price Alert Saved! We will alert you when price drops to ₹{target_price:.2f}")
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
