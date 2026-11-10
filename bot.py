"""Premium Telegram Poll Quiz Bot.

Features: paragraph-to-quiz generation (AI optional), robust MCQ parsing,
inline dashboard, personal quiz library, export, group publishing and
persistent scheduled publishing.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from threading import Thread
from typing import Any

from flask import Flask, jsonify
from dotenv import load_dotenv
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
from pypdf import PdfReader
from docx import Document

from quiz_engine import (
    create_quiz_from_text, generate_with_ai, parse_structured_quiz,
    questions_to_text,
)
from storage import Storage

# ---------------------------------------------------------------------------
# Configuration and health endpoint
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("poll-quiz-bot")
load_dotenv()

web_app = Flask(__name__)
storage = Storage()


@web_app.get("/")
def home():
    return "Poll Quiz Bot is running. Open Telegram and use /start."


@web_app.get("/healthz")
def health():
    return jsonify({"status": "ok", "service": "poll-quiz-bot"}), 200


def run_web_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Create from paragraph", callback_data="create_paragraph")],
        [InlineKeyboardButton("📝 Import MCQ format", callback_data="create_mcq"), InlineKeyboardButton("📚 My library", callback_data="library")],
        [InlineKeyboardButton("🚀 Publish in a group", callback_data="group_help"), InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📖 Help & examples", callback_data="help"), InlineKeyboardButton("ℹ️ About", callback_data="about")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Main menu", callback_data="home")]])


def settings_menu(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    def state(key: str) -> str:
        return "ON ✅" if settings.get(key) else "OFF ❌"
    timer = int(settings.get("timer", 0) or 0)
    timer_text = "Off" if timer == 0 else f"{timer}s"
    count = int(settings.get("question_count", 10) or 10)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🕵️ Anonymous polls: {state('anonymous')}", callback_data="toggle_anonymous")],
        [InlineKeyboardButton(f"🎲 Shuffle questions: {state('shuffle_q')}", callback_data="toggle_shuffle_q")],
        [InlineKeyboardButton(f"🔀 Shuffle options: {state('shuffle_o')}", callback_data="toggle_shuffle_o")],
        [InlineKeyboardButton(f"⏱ Poll timer: {timer_text}", callback_data="timer_menu")],
        [InlineKeyboardButton(f"🔢 Paragraph question count: {count}", callback_data="count_menu")],
        [InlineKeyboardButton("↩️ Main menu", callback_data="home")],
    ])


def timer_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Off", callback_data="timer_0"), InlineKeyboardButton("10 sec", callback_data="timer_10"), InlineKeyboardButton("30 sec", callback_data="timer_30")],
        [InlineKeyboardButton("1 min", callback_data="timer_60"), InlineKeyboardButton("3 min", callback_data="timer_180"), InlineKeyboardButton("5 min", callback_data="timer_300")],
        [InlineKeyboardButton("10 min", callback_data="timer_600")],
        [InlineKeyboardButton("↩️ Settings", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(rows)


def count_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5", callback_data="count_5"), InlineKeyboardButton("10", callback_data="count_10"), InlineKeyboardButton("15", callback_data="count_15"), InlineKeyboardButton("20", callback_data="count_20")],
        [InlineKeyboardButton("↩️ Settings", callback_data="settings")],
    ])


def quiz_action_menu(quiz_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Publish here", callback_data=f"publish:{quiz_id}"), InlineKeyboardButton("👁 Preview", callback_data=f"preview:{quiz_id}")],
        [InlineKeyboardButton("📤 Export", callback_data=f"export:{quiz_id}"), InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{quiz_id}")],
        [InlineKeyboardButton("🗑 Delete quiz", callback_data=f"delete:{quiz_id}")],
        [InlineKeyboardButton("📚 My library", callback_data="library")],
    ])


def _safe_title(value: str) -> str:
    return html.escape(value or "Untitled Quiz")


def _get_ai_config() -> tuple[str, str, str]:
    return (
        os.getenv("AI_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip(),
        os.getenv("AI_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip(),
        os.getenv("AI_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip(),
    )


# ---------------------------------------------------------------------------
# Quiz validation, storage and paragraph pipeline
# ---------------------------------------------------------------------------
def _validate_questions(questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, source in enumerate(questions, 1):
        q = dict(source)
        question = str(q.get("question", "")).strip()
        options = q.get("options", [])
        correct_id = q.get("correct_id")
        if not question:
            errors.append(f"Question {idx}: empty question.")
            continue
        if len(question) > 300:
            errors.append(f"Question {idx}: exceeds Telegram's 300-character limit.")
            continue
        if not isinstance(options, list) or not 2 <= len(options) <= 10:
            errors.append(f"Question {idx}: must have 2 to 10 options.")
            continue
        options = [str(option).strip() for option in options]
        if any(not option or len(option) > 100 for option in options):
            errors.append(f"Question {idx}: an option is empty or exceeds 100 characters.")
            continue
        if len({re.sub(r"\s+", " ", x).casefold() for x in options}) != len(options):
            errors.append(f"Question {idx}: duplicate options.")
            continue
        try:
            correct_id = int(correct_id)
        except (TypeError, ValueError):
            errors.append(f"Question {idx}: correct answer is missing.")
            continue
        if not 0 <= correct_id < len(options):
            errors.append(f"Question {idx}: answer key is outside the options.")
            continue
        explanation = str(q.get("explanation", "")).strip() or "Compare each option with the facts in the source."
        valid.append({
            "question": question,
            "options": options,
            "correct_id": correct_id,
            "explanation": explanation[:200],
            "context": str(q.get("context", "")).strip(),
            **({"evidence": str(q["evidence"])} if q.get("evidence") else {}),
        })
    return valid, errors


async def build_quiz(text: str, user_id: int, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    """Prefer structured parsing, then AI, then cautious offline extraction."""
    structured, warnings, context = parse_structured_quiz(text)
    if structured:
        questions, validation_errors = _validate_questions(structured)
        return questions, "Imported MCQ Quiz", "structured", warnings + validation_errors

    api_key, base_url, model = _get_ai_config()
    count = int(settings.get("question_count", 10) or 10)
    if api_key:
        try:
            questions, title = await generate_with_ai(
                text=text,
                api_key=api_key,
                base_url=base_url,
                model=model,
                count=max(1, min(count, 20)),
            )
            questions, validation_errors = _validate_questions(questions)
            if questions:
                # Paragraph-generated items share the original source passage.
                # Keep the full passage available so it is posted once before
                # the related poll group, rather than showing isolated snippets.
                for question in questions:
                    question["context"] = text.strip()
                return questions, title, "ai_paragraph", warnings + validation_errors
        except Exception as exc:
            logger.warning("AI paragraph generation failed: %s", exc)
            warnings.append(f"AI generation was unavailable: {str(exc)[:180]}")

    offline, offline_warnings, passage, source_type = create_quiz_from_text(text)
    warnings.extend(offline_warnings)
    if offline:
        questions, validation_errors = _validate_questions(offline[:max(1, min(count, 20))])
        for question in questions:
            question["context"] = text.strip()
        return questions, "Paragraph Quiz (Extractive)", source_type, warnings + validation_errors

    return [], "", "unrecognized", warnings


async def process_source(update: Update, context: ContextTypes.DEFAULT_TYPE, source_text: str, source_name: str = "") -> None:
    if not source_text.strip():
        await update.effective_message.reply_text("I received an empty text. Send a paragraph or formatted MCQs.")
        return
    if len(source_text) > 24000:
        await update.effective_message.reply_text("This source is too long for one request. Please split it into smaller sections (maximum 24,000 characters per upload).")
        return

    status = await update.effective_message.reply_text("🔎 <b>Reading your source…</b>\nI’m detecting question blocks, answers, explanations and paragraph facts.", parse_mode=ParseMode.HTML)
    user_id = update.effective_user.id
    settings = await storage.get_settings(user_id)
    questions, title, source_type, warnings = await build_quiz(source_text, user_id, settings)
    if not questions:
        message = (
            "I couldn't safely create a quiz from this text.\n\n"
            "For a paragraph, configure an AI provider in <code>AI_API_KEY</code>, <code>AI_BASE_URL</code> and <code>AI_MODEL</code> for semantic question generation. "
            "Without AI, I only generate questions from clear definitions, dates, counts and explicit relationships.\n\n"
            "For ready-made MCQs, mark the right option with ✅ or add <code>Answer: B</code>."
        )
        if warnings:
            message += "\n\n<b>Parser notes:</b>\n" + "\n".join(f"• {html.escape(x)}" for x in warnings[:5])
        await status.edit_text(message, parse_mode=ParseMode.HTML)
        return

    default_title = title or (source_name.rsplit(".", 1)[0] if source_name else "My Quiz")
    try:
        quiz_id = await storage.save_quiz(user_id, default_title, source_text, questions, source_type)
    except Exception as exc:
        logger.exception("Could not save quiz")
        await status.edit_text(f"❌ I created {len(questions)} questions, but saving failed: {html.escape(str(exc)[:250])}", parse_mode=ParseMode.HTML)
        return

    mode_labels = {"structured": "Formatted MCQs", "ai_paragraph": "AI paragraph interpretation", "extractive": "Offline fact extraction"}
    mode = mode_labels.get(source_type, source_type)
    result = (
        f"✅ <b>Quiz created successfully!</b>\n\n"
        f"🆔 ID: <code>{quiz_id}</code>\n"
        f"🏷 Title: <b>{_safe_title(default_title)}</b>\n"
        f"❓ Questions: <b>{len(questions)}</b>\n"
        f"🧠 Method: {html.escape(mode)}\n\n"
        "Choose what to do next:"
    )
    if warnings:
        result += "\n\n<b>Import notes:</b>\n" + "\n".join(f"• {html.escape(item[:220])}" for item in warnings[:3])
        result += "\n\nCheck the preview before publishing."
    await status.edit_text(result, parse_mode=ParseMode.HTML, reply_markup=quiz_action_menu(quiz_id))
    context.user_data.pop("input_mode", None)


# ---------------------------------------------------------------------------
# Poll publishing
# ---------------------------------------------------------------------------
def _cancel_markup(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancel publishing", callback_data=f"cancelrun:{run_id}")]])


async def publish_questions(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    quiz: dict[str, Any],
    settings: dict[str, Any],
    timer: int,
    thread_id: int | None,
    run_id: str,
    owner_id: int,
    status_message,
) -> None:
    questions = [dict(q) for q in quiz.get("questions", [])]
    if settings.get("shuffle_q"):
        import random
        if any(str(q.get("context", "")).strip() for q in questions):
            # Keep passage groups in source order. Shuffle only within each
            # group so a shared Directions paragraph always stays above the
            # questions that depend on it.
            grouped_questions: list[list[dict[str, Any]]] = []
            current_group: list[dict[str, Any]] = []
            current_context: str | None = None
            for item in questions:
                item_context = str(item.get("context", "")).strip()
                if current_group and item_context != current_context:
                    random.shuffle(current_group)
                    grouped_questions.append(current_group)
                    current_group = []
                current_context = item_context
                current_group.append(item)
            if current_group:
                random.shuffle(current_group)
                grouped_questions.append(current_group)
            questions = [item for group in grouped_questions for item in group]
        else:
            random.shuffle(questions)
    active_runs = context.application.bot_data.setdefault("active_runs", {})
    run_data = active_runs.get(run_id, {})
    cancel_event: asyncio.Event = run_data.get("cancel_event", asyncio.Event())
    title = str(quiz.get("title", "Quiz"))
    sent = 0
    failed = 0
    last_error = ""
    permission_blocked = False

    try:
        intro = f"🎯 <b>{_safe_title(title)}</b>\n\n📚 {len(questions)} questions are coming up. Good luck!"
        await context.bot.send_message(chat_id=chat_id, text=intro, parse_mode=ParseMode.HTML, message_thread_id=thread_id)

        await status_message.edit_text(
            f"🚀 Publishing <b>{len(questions)}</b> questions…\nSent: 0/{len(questions)}",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_markup(run_id),
        )
        last_context = ""
        for index, source_question in enumerate(questions, 1):
            if cancel_event.is_set():
                break
            q = dict(source_question)
            question_context = str(q.get("context", "")).strip()
            if question_context and question_context != last_context:
                for start in range(0, len(question_context), 3800):
                    chunk = question_context[start:start + 3800]
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📖 <b>Read this information for the next questions</b>\n\n{html.escape(chunk)}",
                        parse_mode=ParseMode.HTML,
                        message_thread_id=thread_id,
                    )
            last_context = question_context
            options = list(q["options"])
            correct_id = int(q["correct_id"])
            if settings.get("shuffle_o"):
                import random
                paired = [(option, i == correct_id) for i, option in enumerate(options)]
                random.shuffle(paired)
                options = [pair[0] for pair in paired]
                correct_id = next(i for i, pair in enumerate(paired) if pair[1])

            try:
                await context.bot.send_poll(
                    chat_id=chat_id,
                    question=q["question"][:300],
                    options=options,
                    type=Poll.QUIZ,
                    correct_option_id=correct_id,
                    explanation=(q.get("explanation") or "Review the source text.")[:200],
                    is_anonymous=bool(settings.get("anonymous", False)),
                    open_period=timer if 5 <= timer <= 600 else None,
                    allows_multiple_answers=False,
                    protect_content=False,
                    message_thread_id=thread_id,
                )
                sent += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                try:
                    await context.bot.send_poll(
                        chat_id=chat_id, question=q["question"][:300], options=options,
                        type=Poll.QUIZ, correct_option_id=correct_id,
                        explanation=(q.get("explanation") or "Review the source text.")[:200],
                        is_anonymous=bool(settings.get("anonymous", False)),
                        open_period=timer if 5 <= timer <= 600 else None,
                        allows_multiple_answers=False, protect_content=False,
                        message_thread_id=thread_id,
                    )
                    sent += 1
                except TelegramError as retry_exc:
                    failed += 1
                    last_error = str(retry_exc)
                    logger.warning("Poll %s failed after retry: %s", index, retry_exc)
                    permission_blocked = not sent and any(token in last_error.casefold() for token in ("not enough rights", "chat_write_forbidden", "polls are not allowed", "bot was kicked", "have no rights"))
            except TelegramError as exc:
                failed += 1
                last_error = str(exc)
                logger.warning("Poll %s failed: %s", index, exc)
                permission_blocked = not sent and any(token in last_error.casefold() for token in ("not enough rights", "chat_write_forbidden", "polls are not allowed", "bot was kicked", "have no rights"))

            if permission_blocked:
                break
            if index % 5 == 0 or index == len(questions):
                if not cancel_event.is_set():
                    try:
                        await status_message.edit_text(
                            f"🚀 Publishing quiz…\n✅ Sent: {sent}/{len(questions)}\n⚠️ Failed: {failed}",
                            reply_markup=_cancel_markup(run_id),
                        )
                    except TelegramError:
                        pass
            # Stay below Telegram group message throughput limits.
            if index < len(questions) and not cancel_event.is_set():
                await asyncio.sleep(3.1)

        if cancel_event.is_set():
            final_text = f"⛔ Publishing cancelled.\n✅ Sent: {sent}\n⚠️ Failed: {failed}"
        elif permission_blocked:
            final_text = "❌ The bot cannot publish polls in this group. Make sure it is a member and the group allows ordinary members to send polls."
        else:
            final_text = f"🏁 <b>Publishing completed!</b>\n✅ Sent: {sent}/{len(questions)}\n⚠️ Failed: {failed}"
        if last_error:
            final_text += f"\n\nDetails: {html.escape(last_error[:180])}"
        try:
            await status_message.edit_text(final_text, parse_mode=ParseMode.HTML)
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode=ParseMode.HTML, message_thread_id=thread_id)
    except TelegramError as exc:
        logger.exception("Publish run failed")
        try:
            await status_message.edit_text(
                "❌ Telegram could not publish this quiz. Make sure the bot is a member of the group and ordinary members are allowed to send polls.\n\n"
                f"Details: {html.escape(str(exc)[:220])}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    except Exception:
        logger.exception("Unexpected publishing error")
        try:
            await status_message.edit_text("❌ An unexpected error interrupted publishing. Check the Railway logs.")
        except TelegramError:
            pass
    finally:
        active_runs.pop(run_id, None)


async def start_publish(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    quiz: dict[str, Any],
    settings: dict[str, Any],
    timer: int | None = None,
    thread_id: int | None = None,
    owner_id: int = 0,
) -> str:
    questions, errors = _validate_questions(quiz.get("questions", []))
    if not questions:
        raise ValueError("This quiz has no valid questions. Re-import it or review the source.")
    quiz_copy = dict(quiz)
    quiz_copy["questions"] = questions
    run_id = uuid.uuid4().hex[:8]
    effective_timer = int(settings.get("timer", 0) if timer is None else timer)
    status_message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚀 Preparing <b>{html.escape(str(quiz.get('title', 'Quiz')))}</b>…\n{len(questions)} questions",
        parse_mode=ParseMode.HTML,
        message_thread_id=thread_id,
    )
    active_runs = context.application.bot_data.setdefault("active_runs", {})
    active_runs[run_id] = {"owner_id": owner_id, "cancel_event": asyncio.Event()}
    task = context.application.create_task(
        publish_questions(context, chat_id, quiz_copy, settings, effective_timer, thread_id, run_id, owner_id, status_message),
        update=None,
        name=f"quiz-publish-{run_id}",
    )
    active_runs[run_id]["task"] = task
    return run_id


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("input_mode", None)
    message = (
        "👋 <b>Welcome to Poll Quiz Studio</b>\n\n"
        "Turn formatted MCQs or study paragraphs into Telegram quiz polls. "
        "Save quizzes in your personal library, preview them, export backups, and publish them in groups.\n\n"
        "Choose an action below."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(message, parse_mode=ParseMode.HTML, reply_markup=main_menu())
    elif update.effective_message:
        await update.effective_message.reply_text(message, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 <b>How to use Poll Quiz Studio</b>\n\n"
        "<b>1. Create from a paragraph</b>\nSend a textbook passage, article extract or notes. With an AI provider configured, I create source-grounded questions and check their evidence.\n\n"
        "<b>2. Import formatted MCQs</b>\nMark the correct option with ✅, or add <code>Answer: B</code>. Explanations are optional.\n\n"
        "<b>Example format</b>\n<pre>1. What is 2 + 2?\nA) 3\nB) 4 ✅\nC) 5\nD) 6\nExplanation: Two plus two equals four.\n\n2. Which is a primary colour?\nA) Green\nB) Purple\nC) Red ✅\nD) Pink</pre>\n"
        "<b>3. Publish in a group</b>\nAdd the bot to the group as a member, then use <code>/publish QUIZID</code> or <code>/quizpoll QUIZID</code>. Admin rights are not required when regular members are allowed to send polls.\n\n"
        "<b>Useful commands</b>\n"
        "<code>/start</code> — Dashboard\n<code>/library</code> — Saved quizzes\n<code>/publish ID</code> — Publish in this chat\n<code>/quizpoll ID</code> — Publish in this chat (alias)\n"
        "<code>/pollquiz</code> — Create and publish from command text\n<code>/export ID</code> — Download backup\n<code>/settitle ID Title</code> — Rename\n"
        "<code>/schedule ID 15m</code> — Schedule\n<code>/myschedules</code> — List schedules\n<code>/cancelschedule ID</code> — Cancel schedule\n<code>/chatid</code> — Show group/topic details"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=back_menu())


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    quizzes = await storage.list_quizzes(user_id, limit=20)
    if not quizzes:
        await update.effective_message.reply_text("📭 Your library is empty. Use /start to create your first quiz.", reply_markup=main_menu())
        return
    keyboard = []
    for q in quizzes[:15]:
        keyboard.append([InlineKeyboardButton(f"📘 {q['id']} · {str(q.get('title', 'Untitled'))[:35]}", callback_data=f"open:{q['id']}")])
    keyboard.append([InlineKeyboardButton("↩️ Main menu", callback_data="home")])
    await update.effective_message.reply_text(f"📚 <b>Your quiz library</b>\n{len(quizzes)} recent quizzes", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = await storage.get_settings(update.effective_user.id)
    await update.effective_message.reply_text("⚙️ <b>Personal publishing settings</b>", parse_mode=ParseMode.HTML, reply_markup=settings_menu(settings))


async def save_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    await process_source(update, context, message.text)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.effective_message.document
    filename = doc.file_name or "source.txt"
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension not in {"txt", "md", "pdf", "docx"}:
        await update.effective_message.reply_text("Supported files: .txt, .md, .pdf and .docx.")
        return
    if doc.file_size and doc.file_size > 10_000_000:
        await update.effective_message.reply_text("File is too large. Maximum supported size is 10 MB.")
        return
    status = await update.effective_message.reply_text("⏳ Downloading and extracting your source file…")
    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await telegram_file.download_as_bytearray())
        if extension in {"txt", "md"}:
            try:
                text = file_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = file_bytes.decode("utf-8", errors="replace")
                if "\ufffd" in text:
                    await status.edit_text("❌ I couldn't decode this file as UTF-8. Save it as UTF-8 and upload again.")
                    return
        elif extension == "pdf":
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted:
                await status.edit_text("❌ This PDF is password-protected. Upload an unlocked copy.")
                return
            page_texts = []
            for page_number, page in enumerate(reader.pages, 1):
                if page_number > 100:
                    break
                extracted = (page.extract_text() or "").strip()
                if extracted:
                    page_texts.append(f"[Page {page_number}]\n{extracted}")
            text = "\n\n".join(page_texts)
            if not text.strip():
                await status.edit_text("❌ I couldn't extract selectable text from this PDF. Scanned/image-only PDFs need OCR before upload.")
                return
        else:
            document = Document(io.BytesIO(file_bytes))
            blocks = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    cell_values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cell_values:
                        blocks.append(" | ".join(cell_values))
            text = "\n\n".join(blocks)
            if not text.strip():
                await status.edit_text("❌ No readable text was found in this Word document.")
                return
        await status.delete()
        await process_source(update, context, text, source_name=filename)
    except TelegramError as exc:
        await status.edit_text(f"❌ File download failed: {html.escape(str(exc)[:180])}", parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("Document extraction failed")
        await status.edit_text(f"❌ I couldn't read this file: {html.escape(str(exc)[:180])}", parse_mode=ParseMode.HTML)


async def settitle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: <code>/settitle QUIZID New title</code>", parse_mode=ParseMode.HTML)
        return
    quiz_id = context.args[0].upper()
    title = " ".join(context.args[1:]).strip()
    success = await storage.set_title(quiz_id, title, update.effective_user.id)
    if success:
        await update.effective_message.reply_text(f"✅ Title updated for <code>{quiz_id}</code>.", parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text("Quiz not found or you do not own it.")


async def export_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict[str, Any]) -> None:
    content = questions_to_text(quiz.get("questions", []))
    file = io.BytesIO(content.encode("utf-8"))
    file.name = f"{quiz['id']}_{re.sub(r'[^A-Za-z0-9_-]+', '_', quiz.get('title', 'quiz'))[:35]}.txt"
    await context.bot.send_document(
        chat_id=chat_id,
        document=file,
        caption=f"📤 Quiz backup · {quiz['id']} · {html.escape(str(quiz.get('title', 'Quiz')))}",
        parse_mode=ParseMode.HTML,
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /export QUIZID")
        return
    quiz = await storage.get_quiz(context.args[0].upper(), update.effective_user.id)
    if not quiz:
        await update.effective_message.reply_text("Quiz not found or you do not own it.")
        return
    if not quiz.get("questions") and quiz.get("source_text"):
        questions, _, _, _ = create_quiz_from_text(quiz["source_text"])
        quiz["questions"] = questions
    await export_quiz(context, update.effective_chat.id, quiz)


async def delete_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /delete QUIZID")
        return
    quiz_id = context.args[0].upper()
    removed = await storage.delete_quiz(quiz_id, update.effective_user.id)
    await update.effective_message.reply_text("✅ Quiz deleted from your library." if removed else "Quiz not found or you do not own it.")


async def _load_owned_quiz(user_id: int, quiz_id: str) -> dict[str, Any] | None:
    quiz = await storage.get_quiz(quiz_id.upper(), user_id)
    if not quiz:
        return None
    if not quiz.get("questions") and quiz.get("source_text"):
        parsed, _, _, _ = create_quiz_from_text(quiz["source_text"])
        quiz["questions"] = parsed
    return quiz


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: <code>/publish QUIZID</code>\nRun this command in the group where you want the quiz to appear.",
            parse_mode=ParseMode.HTML,
        )
        return
    quiz_id = context.args[0].upper()
    quiz = await _load_owned_quiz(update.effective_user.id, quiz_id)
    if not quiz:
        await update.effective_message.reply_text("Quiz not found or you do not own it. Open /library to see your IDs.")
        return
    settings = await storage.get_settings(update.effective_user.id)
    try:
        await start_publish(
            context, update.effective_chat.id, quiz, settings,
            thread_id=getattr(update.effective_message, "message_thread_id", None),
            owner_id=update.effective_user.id,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not start publishing: {html.escape(str(exc)[:200])}", parse_mode=ParseMode.HTML)


async def pollquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias: /quizpoll ID publishes; /pollquiz TEXT parses and publishes immediately."""
    full_text = update.effective_message.text or ""
    parts = full_text.split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw and update.effective_message.reply_to_message:
        replied = update.effective_message.reply_to_message
        raw = (replied.text or replied.caption or "").strip()
    if not raw:
        await update.effective_message.reply_text(
            "Reply to a formatted MCQ message with /pollquiz, or use /quizpoll QUIZID to publish a saved quiz.\n"
            "For long paragraphs, create and review the quiz in private chat first.",
        )
        return
    if len(context.args) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{4,12}", raw):
        quiz = await _load_owned_quiz(update.effective_user.id, raw.upper())
        if not quiz:
            await update.effective_message.reply_text("Quiz not found or you do not own it.")
            return
        settings = await storage.get_settings(update.effective_user.id)
        try:
            await start_publish(context, update.effective_chat.id, quiz, settings,
                                thread_id=getattr(update.effective_message, "message_thread_id", None),
                                owner_id=update.effective_user.id)
        except Exception as exc:
            await update.effective_message.reply_text(f"Could not start publishing: {html.escape(str(exc)[:200])}", parse_mode=ParseMode.HTML)
        return
    settings = await storage.get_settings(update.effective_user.id)
    questions, warnings, _combined_context = parse_structured_quiz(raw)
    if not questions:
        await update.effective_message.reply_text("I couldn't parse this command as complete MCQs. Create the quiz in private chat with /start so I can show you the preview first.")
        return
    title = "Instant Quiz"
    quiz = {"id": "INSTANT", "title": title, "questions": questions}
    try:
        await start_publish(context, update.effective_chat.id, quiz, settings,
                            thread_id=getattr(update.effective_message, "message_thread_id", None),
                            owner_id=update.effective_user.id)
    except Exception as exc:
        await update.effective_message.reply_text(f"Could not start publishing: {html.escape(str(exc)[:200])}", parse_mode=ParseMode.HTML)


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    thread_id = getattr(update.effective_message, "message_thread_id", None)
    await update.effective_message.reply_text(
        f"<b>Chat:</b> {html.escape(chat.title or chat.type)}\n<b>Chat ID:</b> <code>{chat.id}</code>\n<b>Topic ID:</b> <code>{thread_id if thread_id else 'General / none'}</code>",
        parse_mode=ParseMode.HTML,
    )


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: <code>/schedule QUIZID 15m</code> (minutes m, hours h, days d)", parse_mode=ParseMode.HTML)
        return
    quiz_id, raw_delay = context.args[0].upper(), context.args[1].lower()
    match = re.fullmatch(r"(\d{1,4})([mhd])", raw_delay)
    if not match:
        await update.effective_message.reply_text("Invalid time. Examples: 15m, 2h, 1d")
        return
    amount, unit = int(match.group(1)), match.group(2)
    delay = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    if delay < 60 or delay > 30 * 86400:
        await update.effective_message.reply_text("Choose a schedule between 1 minute and 30 days.")
        return
    quiz = await _load_owned_quiz(update.effective_user.id, quiz_id)
    if not quiz:
        await update.effective_message.reply_text("Quiz not found or you do not own it.")
        return
    settings = await storage.get_settings(update.effective_user.id)
    run_at = time.time() + delay
    thread_id = getattr(update.effective_message, "message_thread_id", None)
    schedule_id = await storage.add_schedule(quiz_id, update.effective_user.id, update.effective_chat.id, thread_id, run_at, int(settings.get("timer", 0)))
    local_time = datetime.fromtimestamp(run_at).astimezone().strftime("%d %b %Y, %I:%M %p %Z")
    await update.effective_message.reply_text(
        f"✅ <b>Quiz scheduled</b>\nID: <code>{quiz_id}</code>\nSchedule ID: <code>{schedule_id}</code>\nRun time: <b>{local_time}</b>\n\nUse /myschedules to view or /cancelschedule {schedule_id} to cancel.",
        parse_mode=ParseMode.HTML,
    )


async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    due = await storage.due_schedules(time.time())
    for scheduled in due:
        # Remove first to provide at-most-once behavior if a process crashes during delivery.
        await storage.remove_schedule(scheduled["id"])
        quiz = await _load_owned_quiz(int(scheduled["user_id"]), scheduled["quiz_id"])
        if not quiz:
            try:
                await context.bot.send_message(chat_id=scheduled["chat_id"], text=f"⚠️ Scheduled quiz {scheduled['quiz_id']} no longer exists.", message_thread_id=scheduled.get("thread_id"))
            except TelegramError:
                pass
            continue
        settings = await storage.get_settings(int(scheduled["user_id"]))
        try:
            await start_publish(
                context, int(scheduled["chat_id"]), quiz, settings,
                timer=int(scheduled.get("timer", 0)),
                thread_id=scheduled.get("thread_id"),
                owner_id=int(scheduled["user_id"]),
            )
        except Exception:
            logger.exception("Scheduled quiz could not start: %s", scheduled.get("id"))


async def cancelschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /cancelschedule SCHEDULE_ID")
        return
    schedule_id = context.args[0].upper()
    removed = await storage.remove_schedule(schedule_id, update.effective_user.id)
    await update.effective_message.reply_text("✅ Schedule cancelled." if removed else "No matching schedule found for your account.")


async def myschedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedules = await storage.list_schedules(update.effective_chat.id, update.effective_user.id)
    if not schedules:
        await update.effective_message.reply_text("📭 No pending schedules in this chat.")
        return
    lines = ["⏰ <b>Scheduled quizzes</b>"]
    for item in schedules[:20]:
        when = datetime.fromtimestamp(float(item["run_at"])).astimezone().strftime("%d %b, %I:%M %p")
        lines.append(f"• <code>{item['id']}</code> · quiz <code>{item['quiz_id']}</code> · {when}")
    lines.append("\nCancel with /cancelschedule SCHEDULE_ID")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_runs = context.application.bot_data.setdefault("active_runs", {})
    owner_id = update.effective_user.id
    matches = [(run_id, run) for run_id, run in active_runs.items() if run.get("owner_id") == owner_id]
    if not matches:
        await update.effective_message.reply_text("You do not have an active publishing task.")
        return
    for _, run in matches:
        run["cancel_event"].set()
    await update.effective_message.reply_text("⛔ Cancellation requested. The current poll may finish before the bot stops.")


# ---------------------------------------------------------------------------
# Inline callbacks
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    if data in ("home", "back"):
        await start(update, context)
        return
    if data == "create_paragraph":
        if query.message.chat.type != ChatType.PRIVATE:
            await query.edit_message_text("For privacy and safe quiz review, open my private chat and use /start to create a quiz. Publishing in this group is still available with /publish QUIZID.", reply_markup=back_menu())
            return
        context.user_data["input_mode"] = "paragraph"
        await query.edit_message_text(
            "🧠 <b>Paragraph-to-quiz mode</b>\n\nSend a paragraph, chapter extract or study notes. I will identify distinct facts and create source-grounded MCQs.\n\nFor high-quality semantic generation, configure an OpenAI-compatible provider in the environment variables. Without one, the bot uses a conservative offline extractor.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return
    if data == "create_mcq":
        if query.message.chat.type != ChatType.PRIVATE:
            await query.edit_message_text("Please import your MCQ text in my private chat. You can publish saved quizzes here using /publish QUIZID.", reply_markup=back_menu())
            return
        context.user_data["input_mode"] = "structured"
        await query.edit_message_text(
            "📝 <b>Import MCQs</b>\n\nSend your question set or upload a .txt/.md file. Use ✅ after the correct answer, or add <code>Answer: B</code>. Explanations and shared passages are supported.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return
    if data == "group_help":
        await query.edit_message_text(
            "🚀 <b>Publish in a group</b>\n\n1. Add this bot to your group.\n2. Normal member access is enough when the group allows members to send polls.\n3. Run <code>/publish QUIZID</code> in the group.\n4. In a forum topic, run the command inside that topic.\n\nAdmin permissions are not required for ordinary group posting. The bot cannot bypass group restrictions that prohibit members from sending polls.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return
    if data == "help":
        await query.edit_message_text(
            "<b>Quick format</b>\n<pre>1. What is 2 + 2?\nA) 3\nB) 4 ✅\nC) 5\nD) 6\nExplanation: Basic addition.</pre>\n\nYou can also use <code>Answer: B</code> after the choices. I understand numbered questions, A–J labels, wrapped question text, multi-line explanations and a shared passage before the questions.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return
    if data == "about":
        await query.edit_message_text(
            "<b>Poll Quiz Studio</b>\nA Telegram quiz-poll builder with structured parsing, optional AI-assisted paragraph interpretation, personal storage, exports, group publishing and persistent schedules.\n\nYour saved quizzes are private to your Telegram account.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return
    if data == "library":
        quizzes = await storage.list_quizzes(user_id, limit=20)
        if not quizzes:
            await query.edit_message_text("📭 Your quiz library is empty. Create a quiz from the main menu.", reply_markup=back_menu())
            return
        keyboard = [[InlineKeyboardButton(f"📘 {q['id']} · {str(q.get('title', 'Untitled'))[:30]}", callback_data=f"open:{q['id']}")] for q in quizzes[:15]]
        keyboard.append([InlineKeyboardButton("↩️ Main menu", callback_data="home")])
        await query.edit_message_text(f"📚 <b>My quiz library</b>\nShowing {min(len(quizzes), 15)} recent quizzes.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if data == "settings":
        settings = await storage.get_settings(user_id)
        await query.edit_message_text("⚙️ <b>Personal publishing settings</b>", parse_mode=ParseMode.HTML, reply_markup=settings_menu(settings))
        return
    if data.startswith("toggle_"):
        setting_map = {"toggle_anonymous": "anonymous", "toggle_shuffle_q": "shuffle_q", "toggle_shuffle_o": "shuffle_o"}
        key = setting_map.get(data)
        if key:
            settings = await storage.get_settings(user_id)
            await storage.update_setting(user_id, key, not bool(settings.get(key)))
            await query.edit_message_reply_markup(reply_markup=settings_menu(await storage.get_settings(user_id)))
        return
    if data == "timer_menu":
        await query.edit_message_text("⏱ <b>Select the default poll timer</b>", parse_mode=ParseMode.HTML, reply_markup=timer_menu())
        return
    if data.startswith("timer_"):
        try:
            value = int(data.split("_", 1)[1])
            await storage.update_setting(user_id, "timer", max(0, min(value, 600)))
            await query.edit_message_text("✅ Timer updated.", reply_markup=settings_menu(await storage.get_settings(user_id)))
        except ValueError:
            pass
        return
    if data == "count_menu":
        await query.edit_message_text("🔢 <b>How many questions should paragraph mode create?</b>", parse_mode=ParseMode.HTML, reply_markup=count_menu())
        return
    if data.startswith("count_"):
        try:
            value = int(data.split("_", 1)[1])
            await storage.update_setting(user_id, "question_count", max(5, min(value, 20)))
            await query.edit_message_text("✅ Question count updated.", reply_markup=settings_menu(await storage.get_settings(user_id)))
        except ValueError:
            pass
        return
    if data.startswith("open:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found or you do not own it.", reply_markup=back_menu())
            return
        await query.edit_message_text(
            f"📘 <b>{_safe_title(quiz.get('title', 'Quiz'))}</b>\n\nID: <code>{quiz_id}</code>\nQuestions: <b>{len(quiz.get('questions', []))}</b>\nSource: {html.escape(str(quiz.get('source_type', 'imported')))}\n\nChoose an action:",
            parse_mode=ParseMode.HTML,
            reply_markup=quiz_action_menu(quiz_id),
        )
        return
    if data.startswith("publish:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found or you do not own it.", reply_markup=back_menu())
            return
        settings = await storage.get_settings(user_id)
        try:
            # The destination is the chat where the action button was pressed.
            await start_publish(context, query.message.chat_id, quiz, settings,
                                thread_id=getattr(query.message, "message_thread_id", None), owner_id=user_id)
            await query.edit_message_text("🚀 Publishing has started in this chat. Use /cancel to stop your active run.", reply_markup=back_menu())
        except Exception as exc:
            await query.edit_message_text(f"Could not publish: {html.escape(str(exc)[:180])}", parse_mode=ParseMode.HTML, reply_markup=back_menu())
        return
    if data.startswith("preview:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found.", reply_markup=back_menu())
            return
        questions = quiz.get("questions", [])
        if not questions:
            await query.edit_message_text("No valid questions are saved in this quiz.", reply_markup=back_menu())
            return
        sample = questions[0]
        options_text = "\n".join(f"{'✅' if i == sample['correct_id'] else '▫️'} {html.escape(opt)}" for i, opt in enumerate(sample["options"]))
        sample_context = str(sample.get("context", "")).strip()
        context_preview = (f"📖 <b>Shared passage</b>\n{html.escape(sample_context[:900])}\n\n" if sample_context else "")
        await query.edit_message_text(
            f"👁 <b>Preview · {html.escape(str(quiz.get('title', 'Quiz')))}</b>\n❓ {len(questions)} questions\n\n{context_preview}<b>Q1.</b> {html.escape(sample['question'])}\n\n{options_text}\n\n<b>Explanation:</b> {html.escape(sample.get('explanation', ''))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Publish", callback_data=f"publish:{quiz_id}")], [InlineKeyboardButton("↩️ Quiz details", callback_data=f"open:{quiz_id}")]]),
        )
        return
    if data.startswith("export:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found.", reply_markup=back_menu())
            return
        await export_quiz(context, query.message.chat_id, quiz)
        return
    if data.startswith("rename:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found.", reply_markup=back_menu())
            return
        context.user_data["rename_quiz_id"] = quiz_id
        await query.edit_message_text(f"Send the new title for <code>{quiz_id}</code> as your next message.", parse_mode=ParseMode.HTML, reply_markup=back_menu())
        return
    if data.startswith("delete:"):
        quiz_id = data.split(":", 1)[1]
        quiz = await _load_owned_quiz(user_id, quiz_id)
        if not quiz:
            await query.edit_message_text("Quiz not found or you do not own it.", reply_markup=back_menu())
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, delete it", callback_data=f"deleteconfirm:{quiz_id}"), InlineKeyboardButton("Keep quiz", callback_data=f"open:{quiz_id}")],
        ])
        await query.edit_message_text(f"Are you sure you want to permanently delete <b>{_safe_title(quiz.get('title', quiz_id))}</b> (<code>{quiz_id}</code>)?", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return
    if data.startswith("deleteconfirm:"):
        quiz_id = data.split(":", 1)[1]
        removed = await storage.delete_quiz(quiz_id, user_id)
        await query.edit_message_text("✅ Quiz deleted." if removed else "Quiz not found.", reply_markup=back_menu())
        return
    if data.startswith("cancelrun:"):
        run_id = data.split(":", 1)[1]
        run = context.application.bot_data.setdefault("active_runs", {}).get(run_id)
        if not run:
            await context.bot.send_message(chat_id=query.message.chat_id, text="This publishing run has already finished.")
            return
        if run.get("owner_id") != user_id:
            await context.bot.send_message(chat_id=query.message.chat_id, text="Only the person who started this publishing run can cancel it.")
            return
        run["cancel_event"].set()
        return


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    # Rename workflow has priority over source parsing.
    rename_id = context.user_data.pop("rename_quiz_id", None)
    if rename_id:
        success = await storage.set_title(rename_id, message.text.strip(), update.effective_user.id)
        await message.reply_text("✅ Quiz renamed." if success else "Could not rename this quiz.")
        return
    await process_source(update, context, message.text)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    await storage.init()
    application.bot_data.setdefault("active_runs", {})
    if application.job_queue is not None:
        application.job_queue.run_repeating(scheduler_tick, interval=15, first=10, name="persistent-schedule-checker")
    logger.info("Storage initialized. Poll Quiz Studio is ready.")


async def post_shutdown(application: Application) -> None:
    await storage.close()


def main() -> None:
    Thread(target=run_web_server, daemon=True).start()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your deployment environment variables.")

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(16)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands are available in private chats and groups. The bot does not auto-post
    # every message in a group; publishing is always explicit.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("library", library_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("settitle", settitle_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("delete", delete_quiz_command))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CommandHandler("quizpoll", publish_command))
    application.add_handler(CommandHandler("pollquiz", pollquiz_command))
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("cancelschedule", cancelschedule_command))
    application.add_handler(CommandHandler("myschedules", myschedules_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_private_text))

    logger.info("Poll Quiz Studio is starting…")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
