import re
import os
import logging
import uuid
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import motor.motor_asyncio

# --- Keep Alive Server for Render ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and modern!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- MongoDB Setup ---
MONGO_URI = os.environ.get("MONGO_URI")
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI) if MONGO_URI else None
db = client['quiz_bot_db'] if client else None
quizzes_collection = db['saved_quizzes'] if db else None

async def save_quiz_to_db(quiz_data: str, user_id: int) -> str:
    quiz_id = str(uuid.uuid4())[:6].upper() # Short 6-char ID
    if quizzes_collection is not None:
        await quizzes_collection.insert_one({
            "id": quiz_id, 
            "user_id": user_id, # Save the owner's ID
            "data": quiz_data
        })
    return quiz_id

async def get_quiz_from_db(quiz_id: str) -> str:
    if quizzes_collection is not None:
        result = await quizzes_collection.find_one({"id": quiz_id})
        return result["data"] if result else None
    return None

async def get_user_quizzes(user_id: int):
    if quizzes_collection is not None:
        cursor = quizzes_collection.find({"user_id": user_id})
        return await cursor.to_list(length=100) # Limit to 100 for safety
    return []

# --- Quiz Parsing Logic ---
def parse_multiple_quizzes(text: str):
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
            if re.match(r'^[a-zA-Z]\)', line):
                option_text = re.sub(r'^[a-zA-Z]\)\s*', '', line)
                if '✅' in option_text or '✔️' in option_text:
                    correct_option_id = len(options)
                    option_text = option_text.replace('✅', '').replace('✔️', '').strip()
                options.append(option_text)
            elif line.lower().startswith('explanation:') or line.lower().startswith('ex:'):
                explanation = re.sub(r'^(explanation|ex):\s*', '', line, flags=re.IGNORECASE).strip()
            else:
                if not options: 
                    question_lines.append(line)

        question = "\n".join(question_lines).strip()

        if question and 2 <= len(options) <= 10 and correct_option_id < len(options):
            parsed_quizzes.append({
                "question": question,
                "options": options,
                "correct_id": correct_option_id,
                "explanation": explanation or "No explanation provided."
            })

    return parsed_quizzes

async def send_polls(message, quizzes):
    for i, quiz in enumerate(quizzes):
        try:
            if len(quiz['question']) > 300:
                await message.reply_text(f"⚠️ Question {i+1} is too long (Max 300 chars). Skipped.")
                continue
                
            await message.reply_poll(
                question=quiz['question'],
                options=quiz['options'],
                type=Poll.QUIZ,
                correct_option_id=quiz['correct_id'],
                explanation=quiz['explanation'][:200],
                is_anonymous=False,
                protect_content=False 
            )
            await asyncio.sleep(1) 
        except Exception as e:
            await message.reply_text(f"❌ Error on Q{i+1}: {str(e)}")

# --- UI & Handlers ---

def get_main_menu():
    """Creates the main inline keyboard menu."""
    keyboard = [
        [InlineKeyboardButton("📚 My Quizzes", callback_data="my_quizzes"),
         InlineKeyboardButton("➕ Create Quiz Guide", callback_data="create_guide")],
        [InlineKeyboardButton("🌐 Support Channel", url="https://t.me/telegram")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sleek Start Menu."""
    text = (
        "👋 <b>Welcome to the Modern Quiz Maker!</b>\n\n"
        "I can convert your text into beautiful, forwardable Telegram Quiz Polls.\n\n"
        "👇 <i>Use the buttons below to navigate.</i>"
    )
    # If it's a callback query (button click), edit the message. Else, reply.
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=get_main_menu(), parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu(), parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button clicks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "create_guide":
        text = (
            "📝 <b>How to Create a Quiz</b>\n\n"
            "Simply send me your quiz text in this exact format. "
            "Separate multiple questions with a <b>blank line</b>.\n\n"
            "<code>What is 2+2?\n"
            "a) 3\n"
            "b) 4 ✅\n"
            "c) 5\n"
            "Ex: Basic math</code>\n\n"
            "✅ Use the tick emoji to mark the correct answer.\n"
            "🆔 I will save it and give you a unique ID!"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif query.data == "my_quizzes":
        quizzes = await get_user_quizzes(query.from_user.id)
        
        if not quizzes:
            text = "📭 <b>You haven't saved any quizzes yet!</b>\n\nSend me a quiz in our DM to get started."
        else:
            text = f"📚 <b>Your Saved Quizzes ({len(quizzes)})</b>\n\n"
            for i, q in enumerate(quizzes[:15]): # Show first 15 to avoid huge messages
                # Get a snippet of the first question
                first_line = q['data'].split('\n')[0][:30]
                text += f"🆔 <code>{q['id']}</code> - {first_line}...\n"
                
            if len(quizzes) > 15:
                text += f"\n<i>...and {len(quizzes)-15} more.</i>"
                
            text += "\n\n💡 <b>To use one in a group, type:</b>\n<code>/quizpoll &lt;ID&gt;</code>"
            
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif query.data == "back":
        await start(update, context) # Call start again to reset the menu

async def save_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user sends plain text in DM."""
    text = update.message.text
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        await update.message.reply_text("❌ <b>Format not recognized.</b>\nTap 'Create Quiz Guide' in the /start menu to see how.", parse_mode='HTML')
        return

    quiz_id = await save_quiz_to_db(text, update.effective_user.id)
    quizzes = parse_multiple_quizzes(text)
    
    if not quizzes:
        await update.message.reply_text("❌ Could not parse any valid questions. Check your formatting.")
        return

    # Success Message
    success_text = (
        f"✅ <b>Successfully Saved {len(quizzes)} Question(s)!</b>\n\n"
        f"🆔 <b>Quiz ID:</b> <code>{quiz_id}</code>\n"
        f"👉 <i>Copy this ID and use <code>/quizpoll {quiz_id}</code> in any group!</i>"
    )
    await update.message.reply_text(success_text, parse_mode='HTML')
    
    # Preview the first poll immediately
    await send_polls(update.message, quizzes[:1]) 

async def quizpoll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/quizpoll &lt;ID&gt;</code>", parse_mode='HTML')
        return

    quiz_id = context.args[0].upper()
    saved_data = await get_quiz_from_db(quiz_id)

    if not saved_data:
        await update.message.reply_text(f"❌ Quiz ID <code>{quiz_id}</code> not found.", parse_mode='HTML')
        return

    quizzes = parse_multiple_quizzes(saved_data)
    status_msg = await update.message.reply_text(f"🚀 <i>Deploying {len(quizzes)} questions...</i>", parse_mode='HTML')
    await send_polls(update.message, quizzes)
    await status_msg.delete()

async def pollquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/pollquiz &lt;text&gt;</code>", parse_mode='HTML')
        return
    text = " ".join(context.args)
    quizzes = parse_multiple_quizzes(text)
    await send_polls(update.message, quizzes)

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        return
    quizzes = parse_multiple_quizzes(text)
    await send_polls(update.message, quizzes)

def main():
    Thread(target=run_flask, daemon=True).start()

    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        print("Error: BOT_TOKEN not set.")
        return

    application = Application.builder().token(TOKEN).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("quizpoll", quizpoll_command))
    application.add_handler(CommandHandler("pollquiz", pollquiz_command))
    
    # Button Click Handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Message Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, save_quiz))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), handle_group_text))

    print("Modern Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
