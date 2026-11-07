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

# --- Keep Alive Server ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and modern!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- MongoDB Setup ---
MONGO_URI = os.environ.get("MONGO_URI")
if MONGO_URI:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client['quiz_bot_db']
    quizzes_collection = db['saved_quizzes']
    settings_collection = db['user_settings']
else:
    client = db = quizzes_collection = settings_collection = None

# --- Database Functions ---
async def save_quiz_to_db(quiz_data: str, user_id: int) -> str:
    quiz_id = str(uuid.uuid4())[:6].upper()
    if quizzes_collection is not None:
        await quizzes_collection.insert_one({"id": quiz_id, "user_id": user_id, "data": quiz_data})
    return quiz_id

async def get_quiz_from_db(quiz_id: str) -> str:
    if quizzes_collection is not None:
        result = await quizzes_collection.find_one({"id": quiz_id})
        return result["data"] if result else None
    return None

async def delete_quiz_from_db(quiz_id: str, user_id: int) -> bool:
    if quizzes_collection is not None:
        result = await quizzes_collection.delete_one({"id": quiz_id, "user_id": user_id})
        return result.deleted_count > 0
    return False

async def get_user_quizzes(user_id: int):
    if quizzes_collection is not None:
        cursor = quizzes_collection.find({"user_id": user_id})
        return await cursor.to_list(length=100)
    return []

async def get_user_settings(user_id: int):
    if settings_collection is not None:
        user = await settings_collection.find_one({"user_id": user_id})
        if not user:
            default_settings = {"user_id": user_id, "anonymous": False, "timer": 0}
            await settings_collection.insert_one(default_settings)
            return default_settings
        return user
    return {"anonymous": False, "timer": 0}

async def update_user_setting(user_id: int, key: str, value):
    if settings_collection is not None:
        await settings_collection.update_one({"user_id": user_id}, {"$set": {key: value}})

# --- SMART Quiz Parsing Logic ---
def parse_multiple_quizzes(text: str):
    """
    Iterates line-by-line. It ignores blank lines entirely.
    When it sees a line after options have started, it assumes a new question began.
    """
    lines = text.strip().split('\n')
    quizzes = []
    
    current_question_lines = []
    current_options = []
    current_correct_id = 0
    current_explanation = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if re.match(r'^[a-zA-Z]\)', line):
            # It's an option
            if not current_question_lines: 
                continue 
            option_text = re.sub(r'^[a-zA-Z]\)\s*', '', line)
            if '✅' in option_text or '✔️' in option_text:
                current_correct_id = len(current_options)
                option_text = option_text.replace('✅', '').replace('✔️', '').strip()
            current_options.append(option_text)
            
        elif line.lower().startswith('explanation:') or line.lower().startswith('ex:'):
            if current_question_lines:
                current_explanation = re.sub(r'^(explanation|ex):\s*', '', line, flags=re.IGNORECASE).strip()
                
        else:
            # It's a question line
            if current_options:
                # We already have options for the previous question. This is a new question.
                q_text = "\n".join(current_question_lines).strip()
                if q_text and 2 <= len(current_options) <= 10 and current_correct_id < len(current_options):
                    quizzes.append({
                        "question": q_text,
                        "options": current_options,
                        "correct_id": current_correct_id,
                        "explanation": current_explanation or "No explanation provided."
                    })
                # Reset for the new question
                current_question_lines = [line]
                current_options = []
                current_correct_id = 0
                current_explanation = ""
            else:
                # Still reading the question stem
                current_question_lines.append(line)

    # Save the very last quiz
    q_text = "\n".join(current_question_lines).strip()
    if q_text and 2 <= len(current_options) <= 10 and current_correct_id < len(current_options):
        quizzes.append({
            "question": q_text,
            "options": current_options,
            "correct_id": current_correct_id,
            "explanation": current_explanation or "No explanation provided."
        })

    return quizzes

# --- Poll Sending Logic ---
async def send_polls(context: ContextTypes.DEFAULT_TYPE, chat_id: int, quizzes: list, anonymous: bool, timer: int, message_thread_id: int = None):
    for i, quiz in enumerate(quizzes):
        try:
            if len(quiz['question']) > 300:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Question {i+1} is too long (Max 300 chars). Skipped.", message_thread_id=message_thread_id)
                continue
                
            await context.bot.send_poll(
                chat_id=chat_id, question=quiz['question'], options=quiz['options'], type=Poll.QUIZ,
                correct_option_id=quiz['correct_id'], explanation=quiz['explanation'][:200],
                is_anonymous=anonymous, open_period=timer if timer >= 5 else None, 
                protect_content=False, message_thread_id=message_thread_id
            )
            await asyncio.sleep(1) 
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Error on Q{i+1}: {str(e)}", message_thread_id=message_thread_id)

# --- UI Menus ---
def get_main_menu():
    keyboard = [
        [InlineKeyboardButton("📚 My Quizzes", callback_data="my_quizzes"), InlineKeyboardButton("➕ Create Guide", callback_data="create_guide")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"), InlineKeyboardButton("🌐 Support", url="https://t.me/telegram")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_menu(settings):
    anon_status = "✅ ON" if settings['anonymous'] else "❌ OFF"
    keyboard = [
        [InlineKeyboardButton(f"🕵️ Anonymous: {anon_status}", callback_data="toggle_anon")],
        [InlineKeyboardButton("⏱ Poll Timer", callback_data="timer_menu")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_timer_menu():
    keyboard = [
        [InlineKeyboardButton("OFF", callback_data="set_timer_0"), InlineKeyboardButton("10s", callback_data="set_timer_10"), InlineKeyboardButton("30s", callback_data="set_timer_30")],
        [InlineKeyboardButton("1m", callback_data="set_timer_60"), InlineKeyboardButton("3m", callback_data="set_timer_180"), InlineKeyboardButton("5m", callback_data="set_timer_300")],
        [InlineKeyboardButton("🔙 Back to Settings", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "👋 <b>Welcome to the Ultimate Quiz Maker!</b>\n\nI turn your text into beautiful, forwardable Telegram Quiz Polls.\n\n💡 <i>Use the buttons below to navigate.</i>"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=get_main_menu(), parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu(), parse_mode='HTML')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "create_guide":
        text = (
            "📝 <b>How to Create a Quiz</b>\n\n"
            "Send me your quiz text directly, OR upload a <b>.txt file</b> (recommended for 50+ questions).\n\n"
            "<code>What is 2+2?\n"
            "a) 3\n"
            "b) 4 ✅\n"
            "c) 5\n"
            "Ex: Basic math</code>\n\n"
            "✅ Mark correct answer with a tick.\n"
            "🆔 I'll save it and give you a unique ID!"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif query.data == "my_quizzes":
        quizzes = await get_user_quizzes(user_id)
        if not quizzes:
            text = "📭 <b>You haven't saved any quizzes yet!</b>"
        else:
            text = f"📚 <b>Your Saved Quizzes ({len(quizzes)})</b>\n\n"
            for q in quizzes[:15]:
                first_line = q['data'].split('\n')[0][:30]
                text += f"🆔 <code>{q['id']}</code> - {first_line}...\n"
            text += "\n💡 <b>To deploy:</b> <code>/quizpoll &lt;ID&gt;</code>"
        
        keyboard = []
        if quizzes:
            keyboard.append([InlineKeyboardButton("🗑 Delete a Quiz", callback_data="delete_menu")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif query.data == "delete_menu":
        quizzes = await get_user_quizzes(user_id)
        keyboard = [[InlineKeyboardButton(f"🗑 {q['id']}", callback_data=f"del_{q['id']}")] for q in quizzes[:10]]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="my_quizzes")])
        await query.edit_message_text("Select a quiz to delete:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data.startswith("del_"):
        quiz_id = query.data.split("_")[1]
        success = await delete_quiz_from_db(quiz_id, user_id)
        msg = f"✅ Quiz <code>{quiz_id}</code> deleted!" if success else "❌ Failed to delete."
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_quizzes")]]))

    elif query.data == "settings":
        settings = await get_user_settings(user_id)
        await query.edit_message_text("⚙️ <b>Bot Settings</b>\n\nCustomize how your quizzes are sent.", reply_markup=get_settings_menu(settings), parse_mode='HTML')
        
    elif query.data == "toggle_anon":
        settings = await get_user_settings(user_id)
        await update_user_setting(user_id, "anonymous", not settings['anonymous'])
        settings['anonymous'] = not settings['anonymous']
        await query.edit_message_reply_markup(reply_markup=get_settings_menu(settings))
        
    elif query.data == "timer_menu":
        await query.edit_message_text("⏱ <b>Select Default Poll Timer:</b>\n<i>Override in chat with /quizpoll ID seconds</i>", reply_markup=get_timer_menu(), parse_mode='HTML')
        
    elif query.data.startswith("set_timer_"):
        timer = int(query.data.split("_")[2])
        await update_user_setting(user_id, "timer", timer)
        settings = await get_user_settings(user_id)
        await query.edit_message_text(f"✅ Timer set to {timer} seconds!\n\n⚙️ <b>Bot Settings</b>", reply_markup=get_settings_menu(settings), parse_mode='HTML')

    elif query.data == "back":
        await start(update, context)

async def process_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    quiz_id = await save_quiz_to_db(text, update.effective_user.id)
    quizzes = parse_multiple_quizzes(text)
    
    if not quizzes:
        await update.message.reply_text("❌ Could not parse any valid questions. Check your formatting.")
        return

    success_text = f"✅ <b>Successfully Saved {len(quizzes)} Question(s)!</b>\n\n🆔 <b>Quiz ID:</b> <code>{quiz_id}</code>\n👉 <i>Use <code>/quizpoll {quiz_id}</code> in any group!</i>"
    await update.message.reply_text(success_text, parse_mode='HTML')
    
    settings = await get_user_settings(update.effective_user.id)
    await send_polls(context, update.effective_chat.id, quizzes[:1], settings['anonymous'], settings['timer'])

async def save_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        await update.message.reply_text("❌ <b>Format not recognized.</b>\nTap 'Create Guide' in the /start menu.", parse_mode='HTML')
        return
    await process_and_save(update, context, text)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles .txt file uploads to bypass 4096 char limits."""
    doc = update.message.document
    if not doc.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please upload a standard `.txt` file.")
        return
    
    msg = await update.message.reply_text("⏳ Downloading and parsing file...")
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    
    try:
        text = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        await msg.edit_text("❌ Error: File must be UTF-8 encoded text.")
        return
        
    await msg.delete()
    await process_and_save(update, context, text)

async def quizpoll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/quizpoll &lt;ID&gt; [timer_seconds]</code>", parse_mode='HTML')
        return

    quiz_id = context.args[0].upper()
    saved_data = await get_quiz_from_db(quiz_id)
    if not saved_data:
        await update.message.reply_text(f"❌ Quiz ID <code>{quiz_id}</code> not found.", parse_mode='HTML')
        return

    settings = await get_user_settings(update.effective_user.id)
    timer = settings['timer']
    if len(context.args) > 1:
        try: timer = int(context.args[1])
        except ValueError: pass

    quizzes = parse_multiple_quizzes(saved_data)
    thread_id = getattr(update.effective_message, 'message_thread_id', None)
    
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚀 <i>Deploying {len(quizzes)} questions...</i>", parse_mode='HTML', message_thread_id=thread_id)
    await send_polls(context, update.effective_chat.id, quizzes, settings['anonymous'], timer, message_thread_id=thread_id)
    await status_msg.delete()

async def pollquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/pollquiz &lt;text&gt;</code>", parse_mode='HTML')
        return
    text = " ".join(context.args)
    quizzes = parse_multiple_quizzes(text)
    settings = await get_user_settings(update.effective_user.id)
    thread_id = getattr(update.effective_message, 'message_thread_id', None)
    await send_polls(context, update.effective_chat.id, quizzes, settings['anonymous'], settings['timer'], message_thread_id=thread_id)

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not re.search(r'^[a-zA-Z]\)', text, re.MULTILINE):
        return
    quizzes = parse_multiple_quizzes(text)
    settings = await get_user_settings(update.effective_user.id)
    thread_id = getattr(update.effective_message, 'message_thread_id', None)
    await send_polls(context, update.effective_chat.id, quizzes, settings['anonymous'], settings['timer'], message_thread_id=thread_id)

def main():
    Thread(target=run_flask, daemon=True).start()

    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("quizpoll", quizpoll_command))
    application.add_handler(CommandHandler("pollquiz", pollquiz_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Handlers for DMs
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, save_quiz))
    application.add_handler(MessageHandler(filters.Document.MimeType("text/plain") & filters.ChatType.PRIVATE, handle_document))
    
    # Handlers for Groups
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), handle_group_text))

    print("Ultimate Bot is running with File Upload support...")
    application.run_polling()

if __name__ == "__main__":
    main()
