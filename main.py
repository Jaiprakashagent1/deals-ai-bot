import os
import http.server
import socketserver
import threading
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Render డమ్మీ పోర్ట్ సర్వర్
def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# API Keys శుభ్రపరచడం (ఏ విధమైన స్పేస్‌లు, క్యారెక్టర్లు ఉన్నా తొలగిస్తుంది)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().replace('\n', '').replace('\r', '')
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().replace('\n', '').replace('\r', '')

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("AI Deals Bot is Active! Send me product details or links.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    prompt = f"""
    Create an engaging Telegram post for the channel 'Ai deals of today' using the following deal info or link:
    {user_text}
    
    Include:
    - Product Title
    - Key Highlights/Benefits
    - Call to Action with Link
    - Relevant Emojis and Formatting
    """
    
    try:
        response = model.generate_content(prompt)
        await update.message.reply_text(response.text)
    except Exception as e:
        await update.message.reply_text(f"Error processing deal: {str(e)}")

if __name__ == '__main__':
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

