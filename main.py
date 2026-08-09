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

# Initialize Groq Client
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
# 5. Advanced Data Extraction Engine (Facebook ExternalHit Scraper)
# ---------------------------------------------------------------------------
JUNK_TITLE_PATTERN = re.compile(r'^[!\W]*[A-Za-z0-9]{8,}$')
BRAND_SUFFIX_PATTERN = re.compile(
    r'\s*[\|\-–]\s*(Amazon(\.in)?|Flipkart|Myntra|Ajio|Nykaa|Meesho|Tata\s*CLiQ).*',
    flags=re.IGNORECASE
)

def clean_title(raw_title: str, raw_user_text: str) -> str:
    title = (raw_title or "").strip()
    title = BRAND_SUFFIX_PATTERN.sub('', title).strip()

    if not title or JUNK_TITLE_PATTERN.match(title) or len(title) < 4:
        fallback = re.sub(r'https?://\S+', '', raw_user_text or "").strip()
        title = fallback if len(fallback) > 3 else ""

    return title if title else "Featured Deal Product"

def extract_price_from_jsonld(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            offers = item.get("offers") if isinstance(item, dict) else None
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price"):
                return f"₹{offers['price']}"
    return ""

def extract_price_from_text(text: str) -> str:
    patterns = [
        r'(?:Deal Price|Price)["\s:>]{1,15}(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d{1,2})?)',
        r'(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d{1,2})?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            return f"₹{match.group(1)}"
    return ""

def scrape_link_data(url: str, raw_user_text: str = "") -> dict:
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    final_url = url
    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=8, allow_redirects=True)
        final_url = res.url
        soup = BeautifulSoup(res.text, "html.parser")

        og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        raw_title = og_title["content"].strip() if og_title and og_title.get("content") else ""
        if not raw_title and soup.title and soup.title.string:
            raw_title = soup.title.string.strip()

        og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "twitter:description"})
        desc = og_desc["content"].strip() if og_desc and og_desc.get("content") else ""

        meta_price = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
        price = f"₹{meta_price['content'].strip()}" if meta_price and meta_price.get("content") else ""
        if not price:
            price = extract_price_from_jsonld(soup)
        if not price:
            price = extract_price_from_text(desc) or extract_price_from_text(res.text)

        return {
            "url": final_url,
            "title": clean_title(raw_title, raw_user_text),
            "price": price if price else "Check Link",
            "desc": desc,
        }
    except Exception:
        return {
            "url": url,
            "title": clean_title("", raw_user_text),
            "price": "Check Link",
            "desc": "",
        }

def get_numeric_price(price_str: str):
    if not price_str:
        return None
    digits = re.sub(r'[^\d.]', '', price_str)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# 6. Groq AI Engine (Prasad Tech Telugu Formatting)
# ---------------------------------------------------------------------------
def build_master_prompt(user_input: str, scraped_info: dict, deal_type: str = "NORMAL") -> str:
    category_list = ", ".join(CATEGORIES)
    return f"""You are an automated deal poster formatting strict, ultra-clean Telegram deals exactly like Prasad Tech in Telugu.

INPUT DATA:
- Product Title: {scraped_info.get('title', '')}
- Live Deal Price: {scraped_info.get('price', 'Check Link')}
- Buy Link: {scraped_info.get('url', '')}
- Product Specs: {scraped_info.get('desc', '')}
- User Notes: {user_input}
- Deal Type: {deal_type}

OUTPUT ONLY the exact template below. NO intro, NO explanatory text, NO markdown code fences, NO conversational conclusions.

🔥🔥 [Full Product Title with Main Specs like RAM/Storage/Color/Wattage]

🎁 Deal Price : {scraped_info.get('price', 'Check Link')}

🔍 Cross Platform Price : Best Competitive Offer Across Market

Buy Here : {scraped_info.get('url', '')}

💥 Bank Offer : [Extract active bank offer if present in specs/notes, otherwise OMIT this entire line]

[Exactly ONE hashtag from this official list: {category_list}]

STRICT RULES:
1. The buy link appears EXACTLY ONCE, under "Buy Here :". Never repeat it.
2. No fluff sentences. No text before 🔥🔥 or after the hashtag line.
3. Pick exactly one category hashtag from the official list above - never invent a new one.
"""

def call_groq_ai(prompt: str) -> str:
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        raise Exception(f"Groq API Call Failed: {str(e)}")

# ---------------------------------------------------------------------------
# 7. Telegram Handlers & Auto-Posting Logic
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 AI Agent For Deals is Active!\n\n"
        "• Send any product link to get an instant formatted deal post.\n"
        "• /track [Link] [Price] - Set a personal price-drop alert\n"
        "• /mytracks - View your active price alerts\n"
        "• /bypass [Link] - Admin-only: verified premium deal\n"
        "• /sponsored [Link] - Admin-only: sponsored promotion"
    )

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not CHANNEL_ID:
        return False
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        return True
    except Exception as e:
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Failed to post to channel: {e}")
            except Exception:
                pass
        return False

async def handle_deal_request(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, deal_type: str = "NORMAL"):
    try:
        url_match = re.search(r'https?://\S+', raw_text)
        scraped = scrape_link_data(url_match.group(0), raw_user_text=raw_text) if url_match else {
            "url": "", "title": clean_title("", raw_text), "price": "Check Link", "desc": ""
        }

        prompt = build_master_prompt(raw_text, scraped, deal_type)
        formatted_deal = call_groq_ai(prompt)

        await update.message.reply_text(formatted_deal)

        if CHANNEL_ID:
            posted = await post_to_channel(context, formatted_deal)
            if not posted:
                await update.message.reply_text("⚠️ Could not post to channel - check bot admin rights in channel.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error processing deal: {str(e)}")
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Exception in handle_deal_request: {e}")
            except Exception:
                pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_deal_request(update, context, update.message.text, deal_type="NORMAL")

async def bypass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized: restricted to bot admin.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /bypass [Product Link/Details]")
        return
    await handle_deal_request(update, context, " ".join(context.args), deal_type="BYPASS_PREMIUM")

async def sponsored_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized: restricted to bot admin.")
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
        target_price = float(context.args[1].replace(",", ""))
        user_id = update.effective_user.id

        conn = get_conn()
        conn.execute(
            "INSERT INTO price_alerts (user_id, product_link, target_price) VALUES (?, ?, ?)",
            (user_id, link, target_price),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ Price alert saved for ₹{target_price:.2f}. Checked every {PRICE_CHECK_MINUTES} min."
        )
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numerical target price.")

async def mytracks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, product_link, target_price, status FROM price_alerts WHERE user_id = ? ORDER BY id DESC",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("You have no active price alerts.")
        return

    lines = [f"#{r['id']} [{r['status']}] ₹{r['target_price']:.2f} - {r['product_link']}" for r in rows]
    await update.message.reply_text("\n".join(lines))

# ---------------------------------------------------------------------------
# 8. Background Job Engine (Scheduled Price Alert Checking)
# ---------------------------------------------------------------------------
async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, user_id, product_link, target_price FROM price_alerts WHERE status = 'ACTIVE'"
    ).fetchall()
    conn.close()

    for row in rows:
        try:
            scraped = scrape_link_data(row["product_link"])
            current_price = get_numeric_price(scraped.get("price", ""))
            if current_price is not None and current_price <= row["target_price"]:
                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=(
                        f"🔥 Price Drop Alert!\n\n{scraped.get('title', 'Your tracked product')}\n"
                        f"Now: ₹{current_price:.2f} (target was ₹{row['target_price']:.2f})\n"
                        f"Buy: {scraped.get('url', row['product_link'])}"
                    ),
                )
                conn = get_conn()
                conn.execute("UPDATE price_alerts SET status = 'TRIGGERED' WHERE id = ?", (row["id"],))
                conn.commit()
                conn.close()
        except Exception as e:
            if ADMIN_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=int(ADMIN_CHAT_ID), text=f"⚠️ Price check failed for alert #{row['id']}: {e}"
                    )
                except Exception:
                    pass

# ---------------------------------------------------------------------------
# 9. Main Application Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bypass", bypass_command))
    app.add_handler(CommandHandler("sponsored", sponsored_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("mytracks", mytracks_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(check_price_alerts, interval=PRICE_CHECK_MINUTES * 60, first=60)

    app.run_polling()
   
