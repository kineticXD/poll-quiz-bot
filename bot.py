import re
import os
import logging
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

# --- Bot Logic ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Welcome to the Custom Quiz Maker!**\n\n"
        "You can use me in two ways:\n\n"
        "**1. In Groups:**\n"
        "`/pollquiz [your quiz text]`\n\n"
        "**2. In DMs:**\n"
        "Just send the quiz text directly.\n\n"
        "**Quiz Format:**\n"
        "What is 2+2?\n"
        "a) 3\n"
        "b) 4 ✅\n"
        "c) 5\n"
        "Explanation: Basic addition."
    )

async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Parses text and sends a poll. Returns True if successful."""
    try:
        lines = text.strip().split('\n')
        question = lines[0].strip()
        options = []
        correct_option_id = 0
        explanation = ""

        for line in lines[1:]:
            line = line.strip()
            if re.match(r'^[a-dA-D]\)', line):
                option_text = re.sub(r'^[a-dA-D]\)\s*', '', line)
                if '✅' in option_text:
                    correct_option_id = len(options)
                    option_text = option_text.replace('✅', '').strip()
                options.append(option_text)
            elif line.lower().startswith('explanation:') or line.lower().startswith('ex:'):
                explanation = re.sub(r'^(explanation|ex):\s*', '', line, flags=re.IGNORECASE).strip()

        if not (2 <= len(options) <= 10):
            await update.message.reply_text("❌ A quiz must have between 2 and 10 options.")
            return False

        if not explanation:
            explanation = "No explanation provided."

        await update.message.reply_poll(
            question=question,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_option_id,
            explanation=explanation,
            is_anonymous=False
        )
        return True
    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing quiz: {str(e)}")
        return False

async def pollquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /pollquiz command."""
    if not context.args:
        await update.message.reply_text("Usage: `/pollquiz <quiz text>`\n\nExample:\n`/pollquiz What is 2+2?\na) 3\nb) 4 ✅\nc) 5`", parse_mode='Markdown')
        return
    text = " ".join(context.args)
    await create_poll(update, context, text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for plain text messages (DM and group)."""
    text = update.message.text
    if not re.search(r'^[a-dA-D]\)', text, re.MULTILINE):
        return  # Ignore non-quiz text
    await create_poll(update, context, text)

async def forward_without_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles forwarding a poll without the 'Forwarded from' header."""
    # This handler is triggered when a user replies to a poll with /forward
    if not update.message.reply_to_message or not update.message.reply_to_message.poll:
        await update.message.reply_text("Please reply to a poll with `/forward` to remove the header.")
        return

    poll_message = update.message.reply_to_message
    try:
        # copy_message re-sends the poll as a new message, removing the header
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=poll_message.chat.id,
            message_id=poll_message.message_id
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    Thread(target=run_flask, daemon=True).start()

    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        print("Error: BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pollquiz", pollquiz_command))
    application.add_handler(CommandHandler("forward", forward_without_header))
    
    # Message handler for plain text
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
