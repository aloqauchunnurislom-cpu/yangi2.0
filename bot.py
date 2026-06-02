import os
import re
import logging
import json
import random
import copy
import asyncio
import threading
from typing import Dict, List, Any
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest

# ----------------------------------------------------------
# 0. DATABASE PATHS & PERSISTENCE WITH MIGRATION
# ----------------------------------------------------------
# Light Bot 2.0 o'zining data.json faylidan foydalanadi, lekin eski ma'lumotlar yo'qolmasligi uchun
# ota-papkadagi data.json mavjud bo'lsa, avtomatik ravishda undan nusxa ko'chirib oladi.
DATA_FILE = "data.json"
PARENT_DATA_FILE = "../data.json"

def migrate_and_load_data() -> dict:
    # Mahalliy data.json yo'q bo'lsa va ota-papkalarda mavjud bo'lsa, ko'chirib olish
    if not os.path.exists(DATA_FILE) and os.path.exists(PARENT_DATA_FILE):
        try:
            with open(PARENT_DATA_FILE, 'r', encoding='utf-8') as src:
                data = json.load(src)
            with open(DATA_FILE, 'w', encoding='utf-8') as dest:
                json.dump(data, dest, ensure_ascii=False, indent=2)
            logging.info("Eski data.json muvaffaqiyatli Light Bot 2.0 ga migratsiya qilindi.")
        except Exception as e:
            logging.error(f"Migratsiya jarayonida xato: {e}")

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_data(data: dict):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ----------------------------------------------------------
# 1. SOZLAMALAR VA DIZAYN ELEMENTLARI
# ----------------------------------------------------------
# .env faylini ota-papka yoki joriy papkadan yuklash
if os.path.exists(".env"):
    load_dotenv(".env")
elif os.path.exists("../.env"):
    load_dotenv("../.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = "@sharofiddinovnurislom"
CHANNEL_URL = "https://t.me/sharofiddinovnurislom"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

raw_data = migrate_and_load_data()
user_states: Dict[int, Any] = {int(k): v for k, v in raw_data.items()}
group_quiz_states: Dict[int, Any] = {}  # chat_id -> guruh quiz holati

# ----------------------------------------------------------
# 2. REGEX (Kuchaytirilgan parser)
# ----------------------------------------------------------
QUESTION_REGEX = re.compile(
    r"(?P<num>\d+)[.)]\s*(?P<question>[^\n]+)\n+"
    r"\s*A[.)]\s*(?P<a>[^\n]+)\n+"
    r"\s*B[.)]\s*(?P<b>[^\n]+)\n+"
    r"\s*C[.)]\s*(?P<c>[^\n]+)\n+"
    r"\s*D[.)]\s*(?P<d>[^\n]+)\n+"
    r"\s*Javob:\s*(?P<ans>[A-Da-d])",
    re.IGNORECASE | re.MULTILINE
)

EXAMPLE_FORMAT = (
    "📋 *To'g'ri format namunasi:*\n\n"
    "```\n"
    "1. Dunyoning eng baland cho'qqisi qaysi?\n"
    "A) Everest\n"
    "B) K2\n"
    "C) Kanchendjanga\n"
    "D) Lhotse\n"
    "Javob: A\n\n"
    "2. O'zbekiston poytaxti qaysi shahar?\n"
    "A) Samarqand\n"
    "B) Toshkent\n"
    "C) Buxoro\n"
    "D) Andijon\n"
    "Javob: B\n"
    "```"
)

# ----------------------------------------------------------
# 3. MUKAMMAL OBUNA TEKSHIRUVCHISI
# ----------------------------------------------------------
def safe_md(text: str) -> str:
    """Markdown v1 formatlash uchun belgilarni tozalash."""
    for ch in ['_', '*', '[', '`']:
        text = text.replace(ch, f'\\{ch}')
    return text

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except BadRequest as e:
        logger.error(f"Kanal topilmadi yoki bot kanalda admin emas: {CHANNEL_ID} — {e}")
        return False
    except Exception as e:
        logger.error(f"Obuna tekshirishda kutilmagan xato: {e}")
        return False

async def require_subscription(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Obunani qattiq talab qiladi. Obuna bo'lmasa, faqat obuna bo'lish tugmasini ko'rsatadi."""
    is_subscribed = await check_subscription(user_id, context)
    if not is_subscribed:
        keyboard = [
            [InlineKeyboardButton("📢 Kanalga obuna bo'lish", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="check_sub")]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ *Botdan to'liq foydalanish uchun kanalimizga a'zo bo'lishingiz shart!*\n\n"
                f"Kanal: {CHANNEL_URL}\n\n"
                "A'zo bo'lgach, quyidagi \"Obuna bo'ldim\" tugmasini bosing:"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return False
    return True

# ----------------------------------------------------------
# 4. PARSER VA DRAFT STATE MANTIQLARI
# ----------------------------------------------------------
def parse_text_to_questions(text: str) -> List[Dict]:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.strip() + "\n"
    matches = list(QUESTION_REGEX.finditer(text))
    questions = []
    for match in matches:
        d = match.groupdict()
        ans_letter = d["ans"].upper()
        correct_id = {"A": 0, "B": 1, "C": 2, "D": 3}[ans_letter]
        questions.append({
            "question": d["question"].strip(),
            "options": [
                d["a"].strip(),
                d["b"].strip(),
                d["c"].strip(),
                d["d"].strip()
            ],
            "correct_id": correct_id,
            "answer_letter": ans_letter
        })
    return questions

def get_user_state(user_id: int) -> Dict:
    if user_id not in user_states:
        user_states[user_id] = {
            "groups": [],
            "active_group_id": None,
            "active_questions": [],
            "current_index": 0,
            "score": 0,
            "is_active": False,
            "current_msg_id": None,
            "answered": False,
            # Draft rejim
            "draft_questions": [],
            "draft_name": None,
            "is_drafting": False,
        }
    else:
        state = user_states[user_id]
        state.setdefault("groups", [])
        state.setdefault("active_group_id", None)
        state.setdefault("active_questions", [])
        state.setdefault("current_index", 0)
        state.setdefault("score", 0)
        state.setdefault("is_active", False)
        state.setdefault("current_msg_id", None)
        state.setdefault("answered", False)
        state.setdefault("draft_questions", [])
        state.setdefault("draft_name", None)
        state.setdefault("is_drafting", False)
    return user_states[user_id]

def next_group_id(state: Dict) -> int:
    groups = state.get("groups", [])
    if not groups:
        return 1
    return max(g["id"] for g in groups) + 1

# ----------------------------------------------------------
# 5. DASHBOARD & NAVIGATSIYA
# ----------------------------------------------------------
async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    user = update.effective_user
    text = (
        f"👋 *Salom, {safe_md(user.first_name)}!*\n\n"
        "💡 *Light Bot 2.0* tizimiga xush kelibsiz!\n"
        "Barcha xizmatlar barqaror va xavfsiz ishlaydi. Kerakli bo'limni tanlang:"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Test yaratish", callback_data="dash_new")],
        [InlineKeyboardButton("🗂 Mening testlarim", callback_data="dash_myquizzes")],
        [InlineKeyboardButton("📢 Kanalimiz", url=CHANNEL_URL)]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if edit_message and update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass
    else:
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=markup)

def draft_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Saqlash", callback_data="draft_confirm_save")],
        [InlineKeyboardButton("🗑 Bekor qilish", callback_data="draft_cancel")]
    ])

DRAFT_MODE_TEXT = (
    "📝 *Yangi test yaratish rejimi faol!*\n\n"
    "Menga test savollarini yuboring. Bir nechta xabar yuborishingiz mumkin.\n"
    "⚠️ *Muhim:* Siz \"💾 Saqlash\" tugmasini bosmaguningizcha hech qanday test guruhi yaratilmaydi.\n\n"
)

# ----------------------------------------------------------
# 6. SHAXSIY QUIZ FUNKSIYALARI (MANUAL VA TAYMER BILAN)
# ----------------------------------------------------------
def build_question_message(state: Dict) -> tuple:
    idx = state["current_index"]
    questions = state["active_questions"]
    q = questions[idx]
    total = len(questions)

    letters = ["🅰", "🅱", "🅲", "🅳"]
    text = (
        f"❓ *{idx + 1}/{total} — savol:*\n\n"
        f"{safe_md(q['question'])}\n\n"
    )
    for i, opt in enumerate(q["options"]):
        text += f"{letters[i]} {safe_md(opt)}\n"

    keyboard = []
    for i, opt in enumerate(q["options"]):
        keyboard.append([InlineKeyboardButton(
            text=f"{chr(65+i)}) {opt}",
            callback_data=f"ans_{i}"
        )])

    return text, InlineKeyboardMarkup(keyboard)

async def send_question(chat_id: int, state: Dict, context: ContextTypes.DEFAULT_TYPE):
    text, markup = build_question_message(state)
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup
    )
    state["current_msg_id"] = msg.message_id
    state["answered"] = False
    save_data(user_states)

async def finish_quiz(chat_id: int, state: Dict, context: ContextTypes.DEFAULT_TYPE):
    state["is_active"] = False
    save_data(user_states)
    score = state["score"]
    total = len(state["active_questions"])
    percent = int((score / total) * 100) if total > 0 else 0

    emoji = "🏆" if percent >= 90 else "🥈" if percent >= 70 else "🥉" if percent >= 50 else "📚"
    stars = "⭐" * (percent // 20) if percent > 0 else ""

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🏁 *Test yakunlandi!*\n\n"
            f"{emoji} Natija: *{score}/{total}* ({percent}%)\n"
            f"{stars}\n\n"
            f"Menyuga qaytish uchun /start ni bosing."
        ),
        parse_mode="Markdown"
    )

# ----------------------------------------------------------
# 7. GURUH QUIZ FUNKSIYALARI
# ----------------------------------------------------------
async def send_group_question(chat_id: int, gqs: Dict, context: ContextTypes.DEFAULT_TYPE):
    idx = gqs["current_index"]
    q = gqs["questions"][idx]
    total = gqs["total"]
    time_limit = gqs["time_limit"]
    letters = ["🅰", "🅱", "🅲", "🅳"]

    time_display = "Javobdan keyin manual" if time_limit == 0 else f"{time_limit} soniya"
    timer_emoji = "🖱" if time_limit == 0 else "⚡" if time_limit == 15 else "⏰"

    text = (
        f"❓ *Savol {idx + 1}/{total}*\n"
        f"{timer_emoji} Tartib: *{time_display}*\n\n"
        f"{safe_md(q['question'])}\n\n"
    )
    for i, opt in enumerate(q["options"]):
        text += f"{letters[i]} {safe_md(opt)}\n"

    for p in gqs["participants"].values():
        p["answered"] = False
        p["last_answer"] = None

    keyboard = [
        [InlineKeyboardButton(f"{chr(65+i)}) {opt}", callback_data=f"gq_ans_{i}")]
        for i, opt in enumerate(q["options"])
    ]
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    gqs["current_msg_id"] = msg.message_id

    # Agar vaqt rejimi taymerli bo'lsa (15s yoki 30s)
    if time_limit > 0:
        job_name = f"gq_timer_{chat_id}"
        for old_job in context.application.job_queue.get_jobs_by_name(job_name):
            old_job.schedule_removal()
        context.application.job_queue.run_once(
            group_question_timeout,
            when=time_limit,
            data={"chat_id": chat_id, "question_index": idx},
            name=job_name
        )

async def group_question_timeout(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    question_index = job_data["question_index"]

    gqs = group_quiz_states.get(chat_id)
    if not gqs or not gqs.get("is_active"):
        return
    if gqs["current_index"] != question_index:
        return

    await advance_group_question(chat_id, gqs, context)

async def advance_group_question(chat_id: int, gqs: Dict, context: ContextTypes.DEFAULT_TYPE):
    q = gqs["questions"][gqs["current_index"]]
    correct = q["correct_id"]
    letters = ["A", "B", "C", "D"]

    correct_names = [
        p["name"] for p in gqs["participants"].values()
        if p.get("last_answer") == correct
    ]

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=gqs["current_msg_id"],
            reply_markup=None
        )
    except Exception:
        pass

    result_lines = [
        f"⏹ *Savol {gqs['current_index'] + 1} yakunlandi!*\n",
        f"✅ To'g'ri javob: *{letters[correct]}) {safe_md(q['options'][correct])}*\n",
    ]
    if correct_names:
        names_str = ", ".join(safe_md(n) for n in correct_names)
        result_lines.append(f"🎯 To'g'ri javob berganlar: {names_str}")
    else:
        result_lines.append("😔 Hech kim to'g'ri javob bermadi.")

    # Agar "Javobdan keyin o'tish" (manual) rejimi bo'lsa, faqat quiz egasi bosa oladigan tugma qo'shamiz
    next_keyboard = None
    if gqs["time_limit"] == 0:
        next_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➡️ Keyingi savol", callback_data=f"gq_manual_next:{gqs['host_user_id']}")
        ]])

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(result_lines),
        parse_mode="Markdown",
        reply_markup=next_keyboard
    )

    # Agar avtomatik taymerli rejim bo'lsa, 2 soniyadan keyin o'zi o'tadi
    if gqs["time_limit"] > 0:
        gqs["current_index"] += 1
        if gqs["current_index"] >= gqs["total"]:
            await finish_group_quiz(chat_id, gqs, context)
        else:
            await asyncio.sleep(2)
            await send_group_question(chat_id, gqs, context)

async def finish_group_quiz(chat_id: int, gqs: Dict, context: ContextTypes.DEFAULT_TYPE):
    gqs["is_active"] = False
    participants = gqs["participants"]
    total = gqs["total"]

    if not participants:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🏁 *Quiz yakunlandi!*\n\n😔 Hech kim ishtirok etmadi.",
            parse_mode="Markdown"
        )
        group_quiz_states.pop(chat_id, None)
        return

    sorted_p = sorted(participants.items(), key=lambda x: x[1]["score"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]

    lines = [f"🏆 *NATIJALAR JADVALI — {safe_md(gqs['group_name'])}*\n"]
    for i, (uid, p) in enumerate(sorted_p):
        medal = medals[i] if i < 3 else f"{i + 1}."
        score = p["score"]
        percent = int((score / total) * 100)
        stars = "⭐" * (percent // 20) if percent > 0 else ""
        lines.append(f"{medal} {safe_md(p['name'])}: *{score}/{total}* ({percent}%) {stars}")

    winner = sorted_p[0][1]["name"] if sorted_p else "—"
    lines.append(f"\n🎉 G'olib: *{safe_md(winner)}*!")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )
    group_quiz_states.pop(chat_id, None)

# ----------------------------------------------------------
# 8. HANDLERLAR (START, HELP, TEXT, CALLBACK)
# ----------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not await require_subscription(user.id, update.effective_chat.id, context):
        return

    # Deep link: ulashilgan test
    if args and args[0].startswith("share-"):
        parts = args[0].split("-")
        if len(parts) == 3:
            try:
                author_id = int(parts[1])
                group_id = int(parts[2])
                author_state = user_states.get(author_id)
                if author_state:
                    shared_group = next(
                        (g for g in author_state.get("groups", []) if g["id"] == group_id), None
                    )
                    if shared_group:
                        state = get_user_state(user.id)
                        new_id = next_group_id(state)
                        state["groups"].append({
                            "id": new_id,
                            "name": f"{shared_group['name']} (Ulashilgan)",
                            "questions": copy.deepcopy(shared_group["questions"]),
                            "time_limit": shared_group.get("time_limit", 30)
                        })
                        save_data(user_states)
                        keyboard = [[InlineKeyboardButton(
                            "🚀 Testni boshlash",
                            callback_data=f"startquiz:{new_id}"
                        )]]
                        await update.message.reply_text(
                            f"🎉 *Ulashilgan test qabul qilindi!*\n\n"
                            f"📁 Guruh: *{safe_md(shared_group['name'])}*\n"
                            f"📊 Savollar: *{len(shared_group['questions'])}* ta\n"
                            f"⏱ Rejim: *{shared_group.get('time_limit', 30)}s*\n\n"
                            f"Ushbu test ro'yxatingizga saqlandi.",
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                        return
            except Exception:
                pass
        await update.message.reply_text("❌ Ulashilgan test havolasi yaroqsiz.")
        return

    # Deep link: guruhda quiz boshlash
    if args and args[0].startswith("startquiz_"):
        parts = args[0].split("_")
        if len(parts) == 3:
            try:
                host_user_id = int(parts[1])
                group_id = int(parts[2])

                if update.effective_chat.type == "private":
                    await update.message.reply_text("❌ Bu havola guruhda quiz o'tkazish uchun mo'ljallangan.")
                    return

                if user.id != host_user_id:
                    await update.message.reply_text("❌ Faqat test yaratuvchisi uni boshlay oladi.")
                    return

                if update.effective_chat.id in group_quiz_states and group_quiz_states[update.effective_chat.id].get("is_active"):
                    await update.message.reply_text("⚠️ Guruhda faol test ketmoqda.")
                    return

                host_state = user_states.get(host_user_id)
                group = next((g for g in host_state.get("groups", []) if g["id"] == group_id), None)
                if not group:
                    await update.message.reply_text("❌ Test guruh topilmadi.")
                    return

                # Vaqt rejimi testning o'zida saqlangan
                time_limit = group.get("time_limit", 30)
                questions = copy.deepcopy(group["questions"])
                random.shuffle(questions)
                for q_item in questions:
                    correct_text = q_item["options"][q_item["correct_id"]]
                    random.shuffle(q_item["options"])
                    q_item["correct_id"] = q_item["options"].index(correct_text)
                    q_item["answer_letter"] = {0: "A", 1: "B", 2: "C", 3: "D"}[q_item["correct_id"]]

                group_quiz_states[update.effective_chat.id] = {
                    "host_user_id": host_user_id,
                    "group_name": group["name"],
                    "questions": questions,
                    "total": len(questions),
                    "current_index": 0,
                    "time_limit": time_limit,
                    "participants": {},
                    "is_active": True,
                    "current_msg_id": None,
                }

                time_display = "Javobdan keyin manual" if time_limit == 0 else f"{time_limit} soniya"
                await update.message.reply_text(
                    f"🚀 *{safe_md(group['name'])}* boshlandi!\n\n"
                    f"⏱ Vaqt sozlamasi: *{time_display}*\n"
                    f"📊 Savollar soni: *{len(questions)}* ta\n\n"
                    "Tayyor bo'ling...",
                    parse_mode="Markdown"
                )
                await asyncio.sleep(3)
                await send_group_question(update.effective_chat.id, group_quiz_states[update.effective_chat.id], context)
                return
            except Exception as e:
                logger.error(f"Guruhda test boshlashda xatolik: {e}")

    if update.effective_chat.type != "private":
        return

    await show_dashboard(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(EXAMPLE_FORMAT, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat

    if chat.type != "private":
        return

    if not await require_subscription(user_id, chat.id, context):
        return

    text = update.message.text.strip()

    # 1. Rename rejimi
    renaming_group_id = context.user_data.get("renaming_group_id")
    if renaming_group_id is not None:
        new_name = text[:64]
        state = get_user_state(user_id)
        group = next((g for g in state.get("groups", []) if g["id"] == renaming_group_id), None)
        context.user_data.pop("renaming_group_id", None)
        if group:
            group["name"] = new_name
            save_data(user_states)
            await update.message.reply_text(
                f"✅ Guruh nomi muvaffaqiyatli o'zgartirildi:\n*{safe_md(new_name)}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Testni boshqarish", callback_data=f"dash_quiz:{renaming_group_id}")],
                    [InlineKeyboardButton("⬅️ Bosh menyu", callback_data="dash_main")]
                ])
            )
        else:
            await update.message.reply_text("❌ Guruh topilmadi.")
        return

    # 2. Test savollarini parse qilish
    new_questions = parse_text_to_questions(text)
    state = get_user_state(user_id)

    if not new_questions:
        if state.get("is_drafting"):
            await update.message.reply_text(
                "❌ *Xato:* Yuborilgan matnda to'g'ri test formati topilmadi.\n\n"
                f"Jami qo'shilgan savollar: *{len(state['draft_questions'])}* ta\n\n"
                "Iltimos, namunadagidek yuboring:\n" + EXAMPLE_FORMAT,
                parse_mode="Markdown",
                reply_markup=draft_keyboard()
            )
        else:
            await update.message.reply_text(
                "❌ *Format xatosi:* Yozgan matningizdan testlarni ajratib bo'lmadi.\n"
                "Yangi test guruhini yaratish uchun avval quyidagi tugmani bosing:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Yangi test yaratish", callback_data="dash_new")
                ]])
            )
        return

    # Avtomatik draft rejimini yoqish (agar oldin yangi test deb bosilmagan bo'lsa)
    if not state.get("is_drafting"):
        state["draft_questions"] = []
        state["draft_name"] = None
        state["is_drafting"] = True

    state["draft_questions"].extend(new_questions)
    total_draft = len(state["draft_questions"])
    save_data(user_states)

    await update.message.reply_text(
        f"📥 *{len(new_questions)} ta yangi savol qabul qilindi!*\n\n"
        f"📊 Jami draftdagi savollar: *{total_draft}* ta\n\n"
        f"Yana savollar yuborishingiz mumkin. Barcha savollarni yuborib bo'lgach, \"💾 Saqlash\" tugmasini bosing.",
        parse_mode="Markdown",
        reply_markup=draft_keyboard()
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data = query.data

    await query.answer()

    # 1. Obuna bo'ldim tekshiruvi
    if data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)
        if is_subscribed:
            try:
                await query.message.edit_text("✅ *Rahmat! Obuna tasdiqlandi.*", parse_mode="Markdown")
            except Exception:
                pass
            await show_dashboard(update, context)
        else:
            await query.answer("❌ Kanalimizga a'zo bo'lmadingiz!", show_alert=True)
        return

    # Boshqa tugmalarda ham qat'iy a'zolikni tekshirish
    if not await require_subscription(user_id, chat_id, context):
        return

    # Dashboardga qaytish
    if data == "dash_main":
        await show_dashboard(update, context, edit_message=True)
        return

    # Yangi test yaratishni boshlash
    if data == "dash_new":
        state = get_user_state(user_id)
        state["draft_questions"] = []
        state["draft_name"] = None
        state["is_drafting"] = True
        save_data(user_states)
        await query.message.edit_text(
            DRAFT_MODE_TEXT + EXAMPLE_FORMAT,
            parse_mode="Markdown",
            reply_markup=draft_keyboard()
        )
        return

    # Mening quizlarim ro'yxati
    if data == "dash_myquizzes":
        state = get_user_state(user_id)
        groups = state.get("groups", [])
        if not groups:
            await query.message.edit_text(
                "📭 Sizda hozircha hech qanday test guruhi mavjud emas.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Test yaratish", callback_data="dash_new")],
                    [InlineKeyboardButton("⬅️ Bosh menyu", callback_data="dash_main")]
                ])
            )
            return

        keyboard = []
        for g in groups:
            keyboard.append([InlineKeyboardButton(
                f"📁 {g['name']} ({len(g.get('questions', []))} ta)",
                callback_data=f"dash_quiz:{g['id']}"
            )])
        keyboard.append([InlineKeyboardButton("⬅️ Bosh menyu", callback_data="dash_main")])
        await query.message.edit_text(
            "🗂 *Mening testlarim ro'yxati:*\n\nBatafsil ma'lumot uchun testni tanlang:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Quiz boshqaruvi
    if data.startswith("dash_quiz:"):
        group_id = int(data.split(":")[1])
        state = get_user_state(user_id)
        group = next((g for g in state["groups"] if g["id"] == group_id), None)
        if not group:
            await query.answer("❌ Test guruh topilmadi.", show_alert=True)
            return

        bot_me = await context.bot.get_me()
        startgroup_url = f"https://t.me/{bot_me.username}?startgroup=startquiz_{user_id}_{group_id}"
        time_display = "Javobdan keyin" if group.get("time_limit", 30) == 0 else f"{group.get('time_limit', 30)} soniya"

        keyboard = [
            [InlineKeyboardButton("🚀 Shaxsiyda boshlash", callback_data=f"startquiz:{group_id}")],
            [InlineKeyboardButton("👥 Guruhda boshlash", url=startgroup_url)],
            [InlineKeyboardButton("✏️ Nomini o'zgartirish", callback_data=f"rename_group:{group_id}")],
            [InlineKeyboardButton("🔗 Ulashish havolasi", callback_data=f"share:{group_id}")],
            [InlineKeyboardButton("🗑 O'chirish", callback_data=f"dash_del:{group_id}")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="dash_myquizzes")]
        ]
        await query.message.edit_text(
            f"📁 *Guruh:* {safe_md(group['name'])}\n"
            f"📊 *Savollar:* {len(group['questions'])} ta\n"
            f"⏱ *Vaqt rejimi:* {time_display}\n\n"
            "Tegishli amalni tanlang:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # O'chirish
    if data.startswith("dash_del:"):
        group_id = int(data.split(":")[1])
        state = get_user_state(user_id)
        state["groups"] = [g for g in state["groups"] if g["id"] != group_id]
        save_data(user_states)
        await query.answer("🗑 Test guruhi o'chirildi!")
        await query.message.edit_text(
            "✅ O'chirildi.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="dash_myquizzes")]])
        )
        return

    # Guruh nomini o'zgartirish
    if data.startswith("rename_group:"):
        group_id = int(data.split(":")[1])
        context.user_data["renaming_group_id"] = group_id
        await query.message.edit_text(
            "✏️ *Yangi nom yuboring:*\n\n(Maksimal 64 belgi)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data=f"dash_quiz:{group_id}")]])
        )
        return

    # Draft: Bekor qilish
    if data == "draft_cancel":
        state = get_user_state(user_id)
        state["draft_questions"] = []
        state["draft_name"] = None
        state["is_drafting"] = False
        save_data(user_states)
        await query.answer("🗑 Draft bekor qilindi.")
        await query.message.edit_text(
            "🗑 Barcha vaqtinchalik savollar o'chirildi va bekor qilindi.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Bosh menyu", callback_data="dash_main")]])
        )
        return

    # Draft: Saqlash tugmasi bosildi (Vaqt sozlash so'raladi)
    if data == "draft_confirm_save":
        state = get_user_state(user_id)
        if not state.get("draft_questions"):
            await query.answer("⚠️ Avval savollarni yuboring!", show_alert=True)
            return

        keyboard = [
            [InlineKeyboardButton("⚡ 15 soniya (Avtomatik)", callback_data="save_mode:15")],
            [InlineKeyboardButton("⏰ 30 soniya (Avtomatik)", callback_data="save_mode:30")],
            [InlineKeyboardButton("🖱 Javobdan so'ng (Manual)", callback_data="save_mode:0")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="dash_new")]
        ]
        await query.message.edit_text(
            "⏱ *Ushbu test guruhi uchun qaysi vaqt rejimini tanlaysiz?*\n\n"
            "• *15 / 30 soniya:* Har bir savolga avtomatik taymer beriladi.\n"
            "• *Javobdan so'ng:* Savolga javob berilgandan keyingina keyingi savolga o'tish tugmasi chiqadi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Draft: Vaqt sozlamasi tanlanib test saqlanmoqda
    if data.startswith("save_mode:"):
        time_limit = int(data.split(":")[1])
        state = get_user_state(user_id)
        draft_qs = state.get("draft_questions", [])

        if not draft_qs:
            await query.answer("❌ Xatolik yuz berdi.", show_alert=True)
            return

        new_id = next_group_id(state)
        draft_name = state.get("draft_name") or f"Guruh Test #{new_id}"

        new_group = {
            "id": new_id,
            "name": draft_name,
            "questions": draft_qs,
            "time_limit": time_limit
        }
        state["groups"].append(new_group)

        # Draftni tozalaymiz
        state["draft_questions"] = []
        state["draft_name"] = None
        state["is_drafting"] = False
        save_data(user_states)

        time_display = "Javobdan so'ng (Manual)" if time_limit == 0 else f"{time_limit} soniya"
        
        bot_me = await context.bot.get_me()
        startgroup_url = f"https://t.me/{bot_me.username}?startgroup=startquiz_{user_id}_{new_id}"
        
        await query.message.edit_text(
            f"✅ *Test guruhi muvaffaqiyatli yaratildi!*\n\n"
            f"📁 Guruh nomi: *{safe_md(draft_name)}*\n"
            f"📊 Savollar: *{len(draft_qs)}* ta\n"
            f"⏱ Rejim: *{time_display}*\n\n"
            "Uni shaxsiy yoki telegram guruhlarda boshlashingiz mumkin.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Shaxsiyda boshlash", callback_data=f"startquiz:{new_id}")],
                [InlineKeyboardButton("👥 Guruhda boshlash", url=startgroup_url)],
                [InlineKeyboardButton("✏️ Nomini o'zgartirish", callback_data=f"rename_group:{new_id}")],
                [InlineKeyboardButton("⚙️ Testni boshqarish", callback_data=f"dash_quiz:{new_id}")],
                [InlineKeyboardButton("⬅️ Bosh menyu", callback_data="dash_main")]
            ])
        )
        return

    # Shaxsiy testda javob berish
    if data.startswith("ans_"):
        state = user_states.get(user_id)
        if not state or not state.get("is_active"):
            return
        if state.get("answered"):
            return

        chosen = int(data.split("_")[1])
        state["answered"] = True

        idx = state["current_index"]
        q = state["active_questions"][idx]
        correct = q["correct_id"]
        total = len(state["active_questions"])
        letters = ["A", "B", "C", "D"]

        is_correct = (chosen == correct)
        if is_correct:
            state["score"] += 1
            result_text = "✅ *To'g'ri javob!*"
        else:
            result_text = f"❌ *Noto'g'ri!* To'g'ri javob: *{letters[correct]}) {q['options'][correct]}*"

        # Tugmalarni holatini yangilash
        new_keyboard = []
        for i, opt in enumerate(q["options"]):
            icon = "✅" if i == correct else "❌" if i == chosen else "◾"
            new_keyboard.append([InlineKeyboardButton(f"{icon} {letters[i]}) {opt}", callback_data="done")])

        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
        except Exception:
            pass

        state["current_index"] += 1
        save_data(user_states)

        # Test tugadi
        if state["current_index"] >= total:
            await query.message.reply_text(result_text, parse_mode="Markdown")
            await finish_quiz(chat_id, state, context)
            return

        # Guruh sozlamalarini tekshiramiz
        group_id = state.get("active_group_id")
        group = next((g for g in state.get("groups", []) if g["id"] == group_id), None)
        time_limit = group.get("time_limit", 30) if group else 30

        if time_limit == 0:
            # Manual o'tish (Javobdan keyin o'tish rejimi)
            await query.message.reply_text(
                result_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Keyingi savol", callback_data="nextq")]])
            )
        else:
            # Taymerli bo'lsa shaxsiy chatda ham tezkorlik uchun darhol 1.5 soniyada keyingisiga o'tkazadi
            await query.message.reply_text(result_text, parse_mode="Markdown")
            await asyncio.sleep(1.5)
            await send_question(chat_id, state, context)
        return

    # Shaxsiy testda keyingi savol
    if data == "nextq":
        state = user_states.get(user_id)
        if not state or not state.get("is_active"):
            return
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_question(chat_id, state, context)
        return

    # Guruhda javob berish
    if data.startswith("gq_ans_"):
        chosen = int(data.split("_")[2])
        gqs = group_quiz_states.get(chat_id)
        if not gqs or not gqs.get("is_active"):
            await query.answer("⚠️ Test yakunlangan yoki faol emas.", show_alert=True)
            return

        if user_id not in gqs["participants"]:
            gqs["participants"][user_id] = {
                "name": query.from_user.first_name,
                "score": 0,
                "answered": False,
                "last_answer": None
            }

        p = gqs["participants"][user_id]
        if p.get("answered"):
            await query.answer("✋ Siz allaqachon javob berdingiz!", show_alert=True)
            return

        q_item = gqs["questions"][gqs["current_index"]]
        correct = q_item["correct_id"]

        p["answered"] = True
        p["last_answer"] = chosen
        if chosen == correct:
            p["score"] += 1
            await query.answer("✅ To'g'ri javob!", show_alert=False)
        else:
            await query.answer(f"❌ Noto'g'ri! (To'g'ri: {['A','B','C','D'][correct]})", show_alert=False)

        # Agar guruhda hamma javob berib bo'lgan bo'lsa (ishtirokchilar soni > 1 va hamma javob bergan bo'lsa)
        if len(gqs["participants"]) > 1 and all(px.get("answered") for px in gqs["participants"].values()):
            if gqs["time_limit"] > 0:
                job_name = f"gq_timer_{chat_id}"
                for old_job in context.application.job_queue.get_jobs_by_name(job_name):
                    old_job.schedule_removal()
            await advance_group_question(chat_id, gqs, context)
        return

    # Guruhda manual keyingi savolga o'tish (Faqat test egasi uchun)
    if data.startswith("gq_manual_next:"):
        host_user_id = int(data.split(":")[1])
        if user_id != host_user_id:
            await query.answer("❌ Faqat test tashkilotchisi keyingi savolga o'tkaza oladi!", show_alert=True)
            return

        gqs = group_quiz_states.get(chat_id)
        if not gqs or not gqs.get("is_active"):
            return

        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        gqs["current_index"] += 1
        if gqs["current_index"] >= gqs["total"]:
            await finish_group_quiz(chat_id, gqs, context)
        else:
            await send_group_question(chat_id, gqs, context)
        return

    # Ulashish havolasini taqdim etish
    if data.startswith("share:"):
        group_id = int(data.split(":")[1])
        bot_me = await context.bot.get_me()
        bot_username = bot_me.username
        share_link = f"https://t.me/{bot_username}?start=share-{user_id}-{group_id}"
        
        try:
            await query.message.edit_text(
                f"🔗 *Ushbu test guruhining ulashish havolasi:*\n\n`{share_link}`\n\n"
                "Havolani nusxalash uchun ustiga bosing va do'stlaringizga yuboring.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data=f"dash_quiz:{group_id}")]])
            )
        except Exception as e:
            logger.error(f"Share link error: {e}")
            await query.message.reply_text(f"Havola: {share_link}")
        return

    # Shaxsiy quizni boshlash
    if data.startswith("startquiz:"):
        group_id = int(data.split(":")[1])
        state = get_user_state(user_id)
        group = next((g for g in state["groups"] if g["id"] == group_id), None)
        if not group or not group.get("questions"):
            await query.message.reply_text("❌ Savollar topilmadi.")
            return

        active_questions = copy.deepcopy(group["questions"])
        random.shuffle(active_questions)
        for q in active_questions:
            correct_text = q["options"][q["correct_id"]]
            random.shuffle(q["options"])
            q["correct_id"] = q["options"].index(correct_text)
            q["answer_letter"] = {0: "A", 1: "B", 2: "C", 3: "D"}[q["correct_id"]]

        state["active_group_id"] = group_id
        state["active_questions"] = active_questions
        state["current_index"] = 0
        state["score"] = 0
        state["is_active"] = True
        state["current_msg_id"] = None
        state["answered"] = False
        save_data(user_states)

        await query.message.reply_text(
            f"🚀 *{safe_md(group['name'])}* boshlandi!\n\n"
            f"📊 Jami: *{len(active_questions)}* ta savol.\n"
            "Omad tilaymiz!",
            parse_mode="Markdown"
        )
        await send_question(chat_id, state, context)
        return

    if data == "done":
        return

# ----------------------------------------------------------
# 9. KEEP-ALIVE PING TIZIMI (10 DAQIQA = 600 S)
# ----------------------------------------------------------
async def keep_alive_ping(context: ContextTypes.DEFAULT_TYPE):
    logger.info("📡 Keep-alive: Bot hayotligini tasdiqlamoqda...")
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=10)
                logger.info(f"Ping yuborildi: {url} (status={resp.status_code})")
        except Exception as e:
            logger.error(f"Keep-alive pingda xato: {e}")

# ----------------------------------------------------------
# 10. DUMMY HTTP SERVER (Render port talabi uchun)
# ----------------------------------------------------------
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Light Bot 2.0 is running perfectly!")

    def log_message(self, format, *args):
        pass

def run_dummy_server():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    logger.info(f"Dummy HTTP server {port} portida boshlandi.")
    server.serve_forever()

# ----------------------------------------------------------
# 11. ASOSIY PY PROGRAMMA (MAIN)
# ----------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN topilmadi! .env faylini tekshiring.")
        return

    # Render porti uchun dummy serverni fonda ochamiz
    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Job queue orqali keep-alive pingni 10 daqiqada (600 soniyada) ishlatamiz
    if app.job_queue:
        app.job_queue.run_repeating(keep_alive_ping, interval=600, first=10)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_text
    ))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🚀 Light Bot 2.0 muvaffaqiyatli ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
