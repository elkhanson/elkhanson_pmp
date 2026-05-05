"""
PMP Prep Telegram Bot
=====================

Features:
- /start menu with: Random Question, My Stats, Marked Questions, Help
- Random question across all 7 tests
- Single-answer questions use Telegram native quiz polls (clean UX)
- Multi-answer questions use inline buttons (toggle + Submit)
- After every answer: shows correct answer + full explanation from the answer PDF
- Inline buttons after each answer: 🤖 Ask Claude | 🔖 Mark | ➡️ Next
- Exam mode: timer per question (default 90s), auto-grades on timeout
- Score tracking: overall and per-test accuracy
- Mark-for-review with /marked command
- "Ask Claude" — follow-up explanation of any concept in the question

Environment variables required:
  TELEGRAM_BOT_TOKEN     — from @BotFather on Telegram
  ANTHROPIC_API_KEY      — optional, only needed for the Ask Claude feature
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from pathlib import Path

from anthropic import Anthropic
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Poll,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

import database as db

# ---- Setup --------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# Quiet down the very chatty libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
log = logging.getLogger("pmp-bot")

ROOT = Path(__file__).parent
QUESTIONS_FILE = ROOT / "questions.json"
EXAM_TIMER_SECONDS = 90  # per question in exam mode

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6").strip()

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required")

# ---- Question store -----------------------------------------------------

with QUESTIONS_FILE.open() as f:
    QUESTIONS: list[dict] = json.load(f)
QUESTIONS_BY_ID: dict[str, dict] = {q["id"]: q for q in QUESTIONS}
log.info("Loaded %d questions", len(QUESTIONS))

anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
if not anthropic_client:
    log.warning("ANTHROPIC_API_KEY not set — 'Ask Claude' button will be disabled")


# ---- Helpers ------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random Question",  callback_data="menu:random")],
        [InlineKeyboardButton("⏱  Exam Mode (timed)", callback_data="menu:exam")],
        [InlineKeyboardButton("📊 My Stats",          callback_data="menu:stats"),
         InlineKeyboardButton("🔖 Marked",            callback_data="menu:marked")],
        [InlineKeyboardButton("ℹ️  Help",             callback_data="menu:help")],
    ])


def truncate(text: str, limit: int) -> str:
    """Telegram has hard limits: 300 chars for poll question, 100 per option, 200 explanation, 4096 message."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pick_random_question() -> dict:
    return random.choice(QUESTIONS)


def format_correct_label(q: dict) -> str:
    """For multi-answer: 'A, C, D'. Single: 'B'."""
    return ", ".join(q["correct"])


def format_explanation_message(q: dict, user_correct: bool, user_choice: str | None = None) -> str:
    """Prepare the post-answer message. Telegram messages cap at 4096 chars."""
    header = "✅ *Correct!*" if user_correct else "❌ *Incorrect.*"
    body = []
    body.append(header)
    if user_choice is not None:
        body.append(f"Your answer: *{user_choice}*")
    body.append(f"Correct answer: *{format_correct_label(q)}*")
    body.append("")
    body.append("*Explanation:*")
    body.append(q["explanation"])
    body.append("")
    body.append(f"_Source: Test {q['test_number']}, Q{q['question_number']}_")
    msg = "\n".join(body)
    # Markdown special chars in explanation could break parsing — use HTML instead
    return msg


def format_explanation_html(q: dict, user_correct: bool, user_choice: str | None = None) -> str:
    from html import escape
    header = "✅ <b>Correct!</b>" if user_correct else "❌ <b>Incorrect.</b>"
    parts = [header]
    if user_choice is not None:
        parts.append(f"Your answer: <b>{escape(user_choice)}</b>")
    parts.append(f"Correct answer: <b>{escape(format_correct_label(q))}</b>")
    parts.append("")
    parts.append("<b>Explanation:</b>")
    parts.append(escape(q["explanation"]))
    parts.append("")
    parts.append(f"<i>Source: Test {q['test_number']}, Q{q['question_number']}</i>")
    full = "\n".join(parts)
    # Hard cap at 4000 to leave headroom for any character expansion
    if len(full) > 4000:
        full = full[:3990] + "…"
    return full


def post_answer_keyboard(question_id: str, marked: bool, exam_mode: bool) -> InlineKeyboardMarkup:
    rows = []
    if anthropic_client:
        rows.append([InlineKeyboardButton("🤖 Ask Claude about this", callback_data=f"ask:{question_id}")])
    mark_label = "🔖 Unmark" if marked else "🔖 Mark for review"
    next_callback = "next:exam" if exam_mode else "next:random"
    rows.append([
        InlineKeyboardButton(mark_label, callback_data=f"mark:{question_id}"),
        InlineKeyboardButton("➡️ Next question", callback_data=next_callback),
    ])
    rows.append([InlineKeyboardButton("🏠 Main menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def selection_keyboard(question_id: str, options: dict[str, str], selected: list[str]) -> InlineKeyboardMarkup:
    """Inline keyboard for multi-answer questions: each option toggles, Submit confirms."""
    rows = []
    for letter in sorted(options.keys()):
        marker = "🟢" if letter in selected else "⚪️"
        # Truncate option text on the button (Telegram caps button text at 64 bytes)
        label = f"{marker} {letter}. {options[letter]}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"toggle:{question_id}:{letter}")])
    rows.append([InlineKeyboardButton("✅ Submit answer", callback_data=f"submit:{question_id}")])
    return InlineKeyboardMarkup(rows)


# ---- Command handlers ---------------------------------------------------

WELCOME = (
    "👋 <b>Welcome to your PMP prep bot!</b>\n\n"
    "I'll fire random questions from your 7 mock tests and explain every answer.\n\n"
    "<b>Commands:</b>\n"
    "/random  — get a random question\n"
    "/exam    — start exam mode (timed)\n"
    "/stats   — see your accuracy\n"
    "/marked  — review questions you've flagged\n"
    "/help    — show this menu again\n\n"
    f"📚 <b>{len(QUESTIONS):,}</b> questions loaded across all 7 mock tests."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(),
    )


async def cmd_random(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_question(update, context, exam_mode=False)


async def cmd_exam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"⏱ <b>Exam mode</b>: you have <b>{EXAM_TIMER_SECONDS} seconds</b> per question.\n"
        "If time runs out, the question is marked incorrect automatically.\n\n"
        "Sending your first question now…",
        parse_mode=ParseMode.HTML,
    )
    await send_question(update, context, exam_mode=True)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_stats(update.effective_user.id, update.effective_chat.id, context)


async def cmd_marked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    marked_ids = db.list_marked(user_id)
    if not marked_ids:
        await update.message.reply_text(
            "You haven't marked any questions yet. Use the 🔖 button after answering a question to flag it.",
        )
        return
    # Show a quick list, then ask if they want to drill one randomly
    msg = f"🔖 You have <b>{len(marked_ids)}</b> marked question{'s' if len(marked_ids)!=1 else ''}.\n\n"
    msg += "Tap below to get a random one to review:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random marked question", callback_data="marked:random")],
        [InlineKeyboardButton("🏠 Main menu", callback_data="menu:home")],
    ])
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---- Sending a question -------------------------------------------------

async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        exam_mode: bool, question: dict | None = None) -> None:
    """Send a question to the user. If question=None, picks a random one."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if question is None:
        question = pick_random_question()

    # Cancel any pending exam-mode timeout job from a previous question
    _cancel_user_jobs(context, user_id)

    if question["is_multi"]:
        await _send_multi_answer_question(context, chat_id, user_id, question, exam_mode)
    else:
        await _send_single_answer_question(context, chat_id, user_id, question, exam_mode)


async def _send_single_answer_question(context, chat_id: int, user_id: int,
                                       question: dict, exam_mode: bool) -> None:
    """For single-answer questions:
    - If question + every option fits Telegram's poll limits (300 + 100 chars), use a quiz poll
    - Otherwise, fall back to the same inline-button flow we use for multi-answer
      so the full text is visible.
    """
    options_letters = sorted(question["options"].keys())  # ['A','B','C','D'] usually
    suffix = " ⏱" if exam_mode else ""
    full_question = question["question_text"]

    # Telegram limits: poll question 300, each option 100. If anything exceeds → use inline
    options_fit = all(
        len(f"{ltr}. {question['options'][ltr]}") <= 100
        for ltr in options_letters
    )
    question_fits = len(full_question) + len(suffix) <= 300

    if not options_fit:
        # Fall through to inline-button flow so the full option text is visible
        await _send_inline_button_question(context, chat_id, user_id, question, exam_mode)
        return

    # Quiz-poll path
    options_texts = [f"{ltr}. {question['options'][ltr]}" for ltr in options_letters]
    correct_letter = question["correct"][0]
    correct_index = options_letters.index(correct_letter)

    if not question_fits:
        # Send full question as a normal message first
        from html import escape
        timer_note = " ⏱" if exam_mode else ""
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<b>📋 Question:</b>{timer_note}\n\n{escape(full_question)}",
            parse_mode=ParseMode.HTML,
        )
        q_text = "👆 See question above. Pick your answer:"
    else:
        q_text = full_question + suffix

    poll_explanation = truncate(
        f"Correct: {correct_letter}. See full explanation below.", 200,
    )

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=q_text,
        options=options_texts,
        type=Poll.QUIZ,
        correct_option_id=correct_index,
        explanation=poll_explanation,
        is_anonymous=False,
        open_period=EXAM_TIMER_SECONDS if exam_mode else None,
    )

    # Remember which question this poll corresponds to so we can grade it on receipt
    poll_id = msg.poll.id
    context.bot_data.setdefault("polls", {})[poll_id] = {
        "question_id": question["id"],
        "user_id": user_id,
        "chat_id": chat_id,
        "exam_mode": exam_mode,
        "message_id": msg.message_id,
    }

    if exam_mode:
        # Telegram closes the poll itself, but we still want a fallback timer to grade
        # if the user didn't answer.
        context.job_queue.run_once(
            _exam_timeout_single,
            EXAM_TIMER_SECONDS + 2,  # small buffer after Telegram closes the poll
            data={"poll_id": poll_id},
            name=f"exam:{user_id}:{question['id']}",
        )


async def _send_inline_button_question(context, chat_id: int, user_id: int,
                                        question: dict, exam_mode: bool) -> None:
    """Single-answer question rendered with inline buttons (used when options are too long for a quiz poll)."""
    options_block = "\n".join(
        f"<b>{ltr}.</b> {question['options'][ltr]}"
        for ltr in sorted(question["options"].keys())
    )
    instruction = "📝 <b>Pick one answer</b>"
    if exam_mode:
        instruction += f" ⏱ <i>{EXAM_TIMER_SECONDS}s</i>"

    text = (
        f"{instruction}\n\n"
        f"{question['question_text']}\n\n"
        f"{options_block}"
    )

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=selection_keyboard(question["id"], question["options"], selected=[]),
    )

    context.user_data.setdefault("multi_state", {})[question["id"]] = {
        "selected": [],
        "message_id": msg.message_id,
        "chat_id": chat_id,
        "exam_mode": exam_mode,
        "single_choice": True,  # behave like radio buttons, not checkboxes
    }

    if exam_mode:
        context.job_queue.run_once(
            _exam_timeout_multi,
            EXAM_TIMER_SECONDS,
            data={"user_id": user_id, "chat_id": chat_id,
                  "question_id": question["id"], "message_id": msg.message_id},
            name=f"exam:{user_id}:{question['id']}",
        )


async def _send_multi_answer_question(context, chat_id: int, user_id: int,
                                      question: dict, exam_mode: bool) -> None:
    """Use inline keyboard with toggle buttons for multi-answer questions."""
    options_block = "\n".join(
        f"<b>{ltr}.</b> {question['options'][ltr]}" for ltr in sorted(question["options"].keys())
    )
    n_correct = len(question["correct"])
    instruction = f"📝 <b>Choose {n_correct} answer{'s' if n_correct != 1 else ''}</b>"
    if exam_mode:
        instruction += f" ⏱ <i>{EXAM_TIMER_SECONDS}s</i>"

    text = (
        f"{instruction}\n\n"
        f"{question['question_text']}\n\n"
        f"{options_block}"
    )

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=selection_keyboard(question["id"], question["options"], selected=[]),
    )

    # Stash per-message state
    context.user_data.setdefault("multi_state", {})[question["id"]] = {
        "selected": [],
        "message_id": msg.message_id,
        "chat_id": chat_id,
        "exam_mode": exam_mode,
    }

    if exam_mode:
        context.job_queue.run_once(
            _exam_timeout_multi,
            EXAM_TIMER_SECONDS,
            data={"user_id": user_id, "chat_id": chat_id,
                  "question_id": question["id"], "message_id": msg.message_id},
            name=f"exam:{user_id}:{question['id']}",
        )


def _cancel_user_jobs(context, user_id: int) -> None:
    if not context.job_queue:
        return
    for job in context.job_queue.jobs():
        if job.name and job.name.startswith(f"exam:{user_id}:"):
            job.schedule_removal()


# ---- Receiving answers --------------------------------------------------

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered when the user picks an option in a single-answer quiz poll."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user_id = answer.user.id
    polls = context.bot_data.get("polls", {})
    info = polls.pop(poll_id, None)
    if not info:
        return

    question = QUESTIONS_BY_ID.get(info["question_id"])
    if not question:
        return

    # Cancel any pending timeout
    if context.job_queue:
        for job in context.job_queue.jobs():
            if job.name == f"exam:{user_id}:{question['id']}":
                job.schedule_removal()

    chosen_indices = answer.option_ids
    options_letters = sorted(question["options"].keys())
    chosen_letter = options_letters[chosen_indices[0]] if chosen_indices else None
    is_correct = chosen_letter in question["correct"]

    db.record_attempt(
        user_id=user_id, question_id=question["id"],
        test_number=question["test_number"], is_correct=is_correct,
        exam_mode=info.get("exam_mode", False),
    )
    db.set_active_question(user_id, question["id"], is_correct)

    await context.bot.send_message(
        chat_id=info["chat_id"],
        text=format_explanation_html(question, is_correct, chosen_letter),
        parse_mode=ParseMode.HTML,
        reply_markup=post_answer_keyboard(
            question["id"],
            marked=db.is_marked(user_id, question["id"]),
            exam_mode=info.get("exam_mode", False),
        ),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button taps."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if data == "menu:home":
        await query.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
        return

    if data == "menu:random" or data == "next:random":
        await send_question(update, context, exam_mode=False)
        return

    if data == "menu:exam" or data == "next:exam":
        await send_question(update, context, exam_mode=True)
        return

    if data == "menu:stats":
        await show_stats(user_id, chat_id, context)
        return

    if data == "menu:marked":
        marked_ids = db.list_marked(user_id)
        if not marked_ids:
            await query.message.reply_text("You haven't marked any questions yet.")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Random marked question", callback_data="marked:random")],
            [InlineKeyboardButton("🏠 Main menu", callback_data="menu:home")],
        ])
        await query.message.reply_text(
            f"🔖 You have <b>{len(marked_ids)}</b> marked question{'s' if len(marked_ids)!=1 else ''}.",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return

    if data == "menu:help":
        await query.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
        return

    if data == "marked:random":
        marked_ids = db.list_marked(user_id)
        if not marked_ids:
            await query.message.reply_text("No marked questions left.")
            return
        qid = random.choice(marked_ids)
        question = QUESTIONS_BY_ID.get(qid)
        if not question:
            await query.message.reply_text("That question is no longer available.")
            return
        await send_question(update, context, exam_mode=False, question=question)
        return

    if data.startswith("toggle:"):
        _, qid, letter = data.split(":", 2)
        await _toggle_multi_option(update, context, qid, letter)
        return

    if data.startswith("submit:"):
        _, qid = data.split(":", 1)
        await _submit_multi_answer(update, context, qid)
        return

    if data.startswith("mark:"):
        _, qid = data.split(":", 1)
        if db.is_marked(user_id, qid):
            db.unmark_question(user_id, qid)
            await query.answer("Unmarked", show_alert=False)
            new_label = "🔖 Mark for review"
        else:
            db.mark_question(user_id, qid)
            await query.answer("Marked for review ✅", show_alert=False)
            new_label = "🔖 Unmark"
        # Update the keyboard in place to flip the label
        try:
            old_kb = query.message.reply_markup
            new_rows = []
            for row in old_kb.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("mark:"):
                        new_row.append(InlineKeyboardButton(new_label, callback_data=btn.callback_data))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except Exception:
            pass  # not critical if the keyboard can't be edited
        return

    if data.startswith("ask:"):
        _, qid = data.split(":", 1)
        await _ask_claude_followup_prompt(update, context, qid)
        return

    log.warning("Unhandled callback data: %s", data)


async def _toggle_multi_option(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               qid: str, letter: str) -> None:
    state = context.user_data.get("multi_state", {}).get(qid)
    if not state:
        await update.callback_query.answer("This question has expired. Try /random.", show_alert=True)
        return
    question = QUESTIONS_BY_ID.get(qid)
    if not question:
        return

    selected = state["selected"]
    if state.get("single_choice"):
        # Radio-button behaviour: tapping selects only that letter (replacing any prior choice).
        # Tapping the already-selected letter de-selects it.
        if selected == [letter]:
            selected = []
        else:
            selected = [letter]
    else:
        if letter in selected:
            selected.remove(letter)
        else:
            selected.append(letter)
    state["selected"] = selected

    try:
        await update.callback_query.edit_message_reply_markup(
            reply_markup=selection_keyboard(qid, question["options"], selected),
        )
    except Exception as e:
        log.warning("toggle edit failed: %s", e)


async def _submit_multi_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, qid: str) -> None:
    state = context.user_data.get("multi_state", {}).get(qid)
    if not state:
        await update.callback_query.answer("This question has expired. Try /random.", show_alert=True)
        return
    question = QUESTIONS_BY_ID.get(qid)
    if not question:
        return

    user_id = update.effective_user.id
    selected = sorted(state["selected"])
    correct = sorted(question["correct"])
    is_correct = selected == correct
    n_required = len(correct)

    if len(selected) != n_required:
        await update.callback_query.answer(
            f"Please pick exactly {n_required} option{'s' if n_required != 1 else ''}.",
            show_alert=True,
        )
        return

    # Cancel any pending exam timeout
    if context.job_queue:
        for job in context.job_queue.jobs():
            if job.name == f"exam:{user_id}:{qid}":
                job.schedule_removal()

    db.record_attempt(
        user_id=user_id, question_id=qid, test_number=question["test_number"],
        is_correct=is_correct, exam_mode=state.get("exam_mode", False),
    )
    db.set_active_question(user_id, qid, is_correct)

    # Lock the original message's keyboard so they can't change it
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    chosen_label = ", ".join(selected)
    await context.bot.send_message(
        chat_id=state["chat_id"],
        text=format_explanation_html(question, is_correct, chosen_label),
        parse_mode=ParseMode.HTML,
        reply_markup=post_answer_keyboard(
            qid, marked=db.is_marked(user_id, qid),
            exam_mode=state.get("exam_mode", False),
        ),
    )

    # Cleanup state for this question
    context.user_data["multi_state"].pop(qid, None)


# ---- Exam mode timeouts -------------------------------------------------

async def _exam_timeout_single(context: ContextTypes.DEFAULT_TYPE) -> None:
    """If user didn't answer a single-choice quiz in time, grade as incorrect."""
    poll_id = context.job.data["poll_id"]
    polls = context.bot_data.get("polls", {})
    info = polls.pop(poll_id, None)
    if not info:
        return  # already answered, nothing to do
    question = QUESTIONS_BY_ID.get(info["question_id"])
    if not question:
        return

    db.record_attempt(
        user_id=info["user_id"], question_id=question["id"],
        test_number=question["test_number"], is_correct=False, exam_mode=True,
    )
    db.set_active_question(info["user_id"], question["id"], False)

    await context.bot.send_message(
        chat_id=info["chat_id"],
        text=("⏰ <b>Time's up!</b>\n\n" + format_explanation_html(question, False, None)),
        parse_mode=ParseMode.HTML,
        reply_markup=post_answer_keyboard(question["id"],
                                          marked=db.is_marked(info["user_id"], question["id"]),
                                          exam_mode=True),
    )


async def _exam_timeout_multi(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    user_id = data["user_id"]
    qid = data["question_id"]
    chat_id = data["chat_id"]
    question = QUESTIONS_BY_ID.get(qid)
    if not question:
        return

    # Was it already submitted? user_data is per-user but the job runs in chat context;
    # we work around this by reading bot_data instead. Simpler: we just check if state exists.
    # context.user_data here is the bot-level dict; for jobs we have to look it up explicitly.
    user_dict = context.application.user_data.get(user_id, {})
    state = user_dict.get("multi_state", {}).get(qid)
    if not state:
        return  # already submitted

    db.record_attempt(
        user_id=user_id, question_id=qid, test_number=question["test_number"],
        is_correct=False, exam_mode=True,
    )
    db.set_active_question(user_id, qid, False)
    user_dict.get("multi_state", {}).pop(qid, None)

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=data["message_id"], reply_markup=None,
        )
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ <b>Time's up!</b>\n\n" + format_explanation_html(question, False, None),
        parse_mode=ParseMode.HTML,
        reply_markup=post_answer_keyboard(qid, marked=db.is_marked(user_id, qid), exam_mode=True),
    )


# ---- Stats --------------------------------------------------------------

async def show_stats(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    overall = db.get_overall_stats(user_id)
    per_test = db.get_per_test_stats(user_id)
    marked_count = len(db.list_marked(user_id))

    if overall["total"] == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No attempts yet. Use /random to get your first question!",
        )
        return

    lines = ["📊 <b>Your stats</b>", ""]
    lines.append(
        f"<b>Overall:</b> {overall['correct']} / {overall['total']} "
        f"= <b>{overall['accuracy']:.1f}%</b>"
    )
    lines.append("")
    lines.append("<b>Per test:</b>")
    if per_test:
        for s in per_test:
            lines.append(
                f"  • Test {s['test_number']}: {s['correct']}/{s['total']} "
                f"({s['accuracy']:.1f}%)"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"🔖 Marked for review: <b>{marked_count}</b>")
    await context.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


# ---- Ask Claude follow-up ----------------------------------------------

ASK_CLAUDE_PENDING_FLAG = "awaiting_claude_question"


async def _ask_claude_followup_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      qid: str) -> None:
    if not anthropic_client:
        await update.callback_query.message.reply_text(
            "⚠️ The Ask Claude feature isn't enabled (no API key configured)."
        )
        return
    user_id = update.effective_user.id
    db.set_active_question(user_id, qid, last_correct=False)  # last_correct not used here
    context.user_data[ASK_CLAUDE_PENDING_FLAG] = qid
    await update.callback_query.message.reply_text(
        "🤖 What would you like Claude to clarify? Type your question — "
        "for example: <i>“Why is C wrong?”</i> or <i>“Explain the change control process more.”</i>",
        parse_mode=ParseMode.HTML,
    )


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If the user is in 'ask Claude' mode, route their text to Claude."""
    pending_qid = context.user_data.get(ASK_CLAUDE_PENDING_FLAG)
    if not pending_qid:
        return  # ignore stray text
    context.user_data.pop(ASK_CLAUDE_PENDING_FLAG, None)

    question = QUESTIONS_BY_ID.get(pending_qid)
    if not question:
        await update.message.reply_text("That question expired — try /random for a new one.")
        return

    if not anthropic_client:
        await update.message.reply_text("Ask Claude isn't configured (no API key).")
        return

    user_question = update.message.text.strip()
    if not user_question:
        return

    await update.message.chat.send_action("typing")
    try:
        answer_text = await asyncio.to_thread(_call_claude, question, user_question)
    except Exception as e:
        log.exception("Claude API call failed")
        await update.message.reply_text(f"⚠️ Couldn't reach Claude: {e}")
        return

    # Telegram has a 4096-char limit; chunk if needed
    for chunk in _chunk(answer_text, 3900):
        await update.message.reply_text(chunk)


def _call_claude(question: dict, user_question: str) -> str:
    options_block = "\n".join(
        f"  {ltr}. {question['options'][ltr]}" for ltr in sorted(question["options"].keys())
    )
    correct_block = ", ".join(question["correct"])

    system_prompt = (
        "You are a helpful PMP exam tutor. The user is studying for the PMP certification. "
        "Answer their follow-up question about the PMP practice question shown below. "
        "Be concise, accurate, and reference PMI's PMBOK Guide / Agile Practice Guide where "
        "relevant. If they ask why a wrong option is wrong, address that specifically. "
        "Do not invent facts."
    )

    user_prompt = (
        f"PMP practice question:\n\n"
        f"{question['question_text']}\n\n"
        f"Options:\n{options_block}\n\n"
        f"Correct answer: {correct_block}\n\n"
        f"Original explanation from the answer key:\n{question['explanation']}\n\n"
        f"---\n\n"
        f"My follow-up question: {user_question}"
    )

    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip() or "(Claude returned no text)"


def _chunk(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i:i + n]


# ---- Wire it all up -----------------------------------------------------

def main() -> None:
    db.init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("random", cmd_random))
    app.add_handler(CommandHandler("exam", cmd_exam))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("marked", cmd_marked))

    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
