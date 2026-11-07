import re
import os
import logging
import sqlite3
import uuid
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Keep Alive Server for Render ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Database Setup ---
# Note: Render's free tier wipes the disk on restart. 
# For permanent storage, you would use MongoDB, but SQLite is fine for testing.
DB_PATH = "quizzes.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS saved_quizzes
                 (id TEXT PRIMARY KEY, data TEXT)''')
    conn.commit()
    conn.close()

def save_quiz_to_db(quiz_data: str) -> str:
    quiz_id = str(uuid.uuid4())[:8] # Generate a short 8-character ID
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO saved_quizzes (id, data) VALUES (?, ?)", (quiz_id, quiz_data))
    conn.commit()
    conn.close()
    return quiz_id

def get_quiz_from_db(quiz_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT data FROM saved_quizzes WHERE id = ?", (quiz_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# --- Quiz Parsing Logic ---
def parse_multiple_quizzes(text: str):
    """
    Parses a text block into multiple quizzes. 
    Quizzes should be separated by double newlines (\n\n).
    """
    # Split text by double newlines to separate different questions
    raw_blocks = re.split(r'\n\s*\n', text.strip())
    parsed_quizzes = []

    for block in raw_blocks:
        if not block.strip():
            continue

        lines = block.strip().split('\n')
        question_lines = []
        options = []
        correct_option_id = 0
        explanation = ""

        for line in lines:
            line = line.strip()
            # Match options: a) b) c) or A) B) C)
            if re.match(r'^[a-zA-Z]\)', line):
                option_text = re.sub(r'^[a-zA-Z]\)\s*', '', line)
                if '✅' in option_text or '✔️' in option_text:
                    correct_option_id = len(options)
                    option_text = option_text.replace('✅', '').replace('✔️', '').strip()
                options.append(option_text)
            
            # Match Explanation
            elif line.lower().startswith('explanation:') or line.lower().startswith('ex:'):
                explanation = re.sub(r'^(explanation|ex):\s*', '', line, flags=re.IGNORECASE).strip()
            
            # Everything else is part of the question (handles multi-line questions)
            else:
                if not options: # Only add to question if we haven't hit options yet
                    question_lines.append(line)

        question = "\n".join(question_lines).strip()

        if question and len(options) >= 2:
            parsed_quizzes.append({
                "question": question,
                "options": options,
                "correct_id": correct_option_id,
                "explanation": explanation or "No explanation provided."
            })

    return parsed_quizzes

async def send_polls(message, quizzes):
    """Sends a list of polls sequentially."""
    for i, quiz in enumerate(quizzes):
        try:
            # Validate Telegram Poll limits
            if len(quiz['question']) > 300:
                await message.reply_text(f"⚠️ Question {i+1} is too long (Max 300 chars). Skipped.")
                continue
                
            await message.reply_poll(
                question=quiz['question'],
                options=quiz['options'],
                type=Poll.QUIZ,
                correct_option_id=quiz['correct_id'],
                explanation=quiz['explanation'][:200], # Explanation max 200 chars
                is_anonymous=False,
                protect_content=False # CRUCIAL: Allows forwarding without restricted content tag
            )
            # Small delay to avoid Telegram flood limits when sending many polls
            await asyncio.sleep(1) 
        except Exception as e:
            await message.reply_text(f"❌ Error on Q{i+1}: {str(e)}")

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "👋 **Welcome to the Advanced Quiz Maker!**\n\n"
        "**1. To Save a Quiz (in DM):**\n"
        "Just send the quiz text. I will save it and give you an ID.\n\n"
        "**2. To Post a Saved Quiz (in Group):**\n"
        "`/quizpoll <ID>`\n\n"
        "**3. To Post Immediately:**\n"
        "Send the text directly or use `/pollquiz <text>` in a group.\n\n"
        "**Format (Separate multiple questions with a blank line):**\n"
        "What is 2+2?\n"
        "a) 3\n"
        "b) 4 ✅\n"
        "Explanation: Basic math."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def save_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user sends plain text in DM."""
    text = update.message.text
    
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        await update.message.reply_text("❌ Format not recognized. Please send a valid quiz.")
        return

    quiz_id = save_quiz_to_db(text)
    quizzes = parse_multiple_quizzes(text)
    
    await update.message.reply_text(
        f"✅ **Saved {len(quizzes)} question(s)!**\n\n"
        f"🆔 **Quiz ID:** `{quiz_id}`\n"
        f"Use `/quizpoll {quiz_id}` in any group to deploy it.",
        parse_mode='Markdown'
    )
    # Also send them the polls right here so they can see it works
    await send_polls(update.message, quizzes)

async def quizpoll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by /quizpoll <ID>"""
    if not context.args:
        await update.message.reply_text("Usage: `/quizpoll <ID>`", parse_mode='Markdown')
        return

    quiz_id = context.args[0]
    saved_data = get_quiz_from_db(quiz_id)

    if not saved_data:
        await update.message.reply_text("❌ Quiz ID not found. Make sure you saved it in my DM first.")
        return

    quizzes = parse_multiple_quizzes(saved_data)
    status_msg = await update.message.reply_text(f"📥 Deploying {len(quizzes)} questions...")
    await send_polls(update.message, quizzes)
    await status_msg.delete()

async def pollquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by /pollquiz <text> for immediate posting."""
    if not context.args:
        await update.message.reply_text("Usage: `/pollquiz <quiz text>`")
        return
    text = " ".join(context.args)
    quizzes = parse_multiple_quizzes(text)
    await send_polls(update.message, quizzes)

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles plain text in groups."""
    text = update.message.text
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        return
    quizzes = parse_multiple_quizzes(text)
    await send_polls(update.message, quizzes)

def main():
    init_db() # Setup database
    Thread(target=run_flask, daemon=True).start()

    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        print("Error: BOT_TOKEN not set.")
        return

    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("quizpoll", quizpoll_command))
    application.add_handler(CommandHandler("pollquiz", pollquiz_command))
    
    # DM Handler (Saves quizzes)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, save_quiz))
    
    # Group Handler (Auto-posts quizzes)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), handle_group_text))

    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
