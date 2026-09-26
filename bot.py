import asyncio
import logging
import os
import re
from datetime import datetime, date, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("GAZ_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# O'zingizning (admin/egasining) Telegram user_id'lari.
# Bu ID'lardan kelgan xabarlar (masalan chek rasmlari) "xodim so'ragan summa"
# sifatida hisoblanmaydi, faqat log qilinadi.
ADMIN_IDS = {
    # 123456789,  # <-- shu yerga o'z Telegram ID'ingizni yozing
}

DB_PATH = os.getenv("GAZ_BOT_DB", "gaz_rasxod.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gaz-bot")

# ---------------------------------------------------------------------------
# SUMMANI MATNDAN AJRATIB OLISH
# ---------------------------------------------------------------------------
# "50000", "50 ming", "50ming", "menga 50 ming kk", "yuz ming so'm kerak" kabi
# holatlarni tushunadi. Sana (26.09.2026) va telefon raqamlariga o'xshash
# ketma-ketliklarni chalkashtirmaslik uchun oddiy filtrlar qo'yilgan.

WORD_MULT = {
    "ming": 1_000,
    "mln": 1_000_000,
    "million": 1_000_000,
}

DATE_LIKE_RE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}")
NUMBER_RE = re.compile(r"(\d[\d\s.,]{0,12}\d|\d)")


def parse_amount(text: str) -> int | None:
    if not text:
        return None

    # Sana bo'lib ko'ringan qismlarni matndan olib tashlaymiz, aks holda
    # "26.09.2026" dagi raqamlar summa deb noto'g'ri aniqlanishi mumkin.
    cleaned = DATE_LIKE_RE.sub(" ", text.lower())

    m = NUMBER_RE.search(cleaned)
    if not m:
        return None

    raw_num = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
    if not raw_num.isdigit():
        return None
    num = int(raw_num)

    # Songgi raqamdan keyin keladigan so'zni (ming/mln/million) tekshiramiz
    tail = cleaned[m.end():m.end() + 15]
    for word, mult in WORD_MULT.items():
        if word in tail.split()[:2] if tail.split() else False:
            num *= mult
            break
        if tail.strip().startswith(word):
            num *= mult
            break

    # Juda kichik (masalan "2 marta", "5 kun" kabi) sonlarni summa deb olmaymiz
    if num < 500:
        return None

    return num


# ---------------------------------------------------------------------------
# BAZA
# ---------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                amount INTEGER NOT NULL,
                raw_text TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def save_request(chat_id: int, user_id: int, full_name: str,
                        username: str | None, amount: int, raw_text: str,
                        is_admin: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO requests (chat_id, user_id, full_name, username,
                                   amount, raw_text, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, full_name, username, amount, raw_text,
             int(is_admin), datetime.now().isoformat()),
        )
        await db.commit()


async def fetch_report(chat_id: int, start: date, end: date) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT full_name, SUM(amount) as total, COUNT(*) as cnt
            FROM requests
            WHERE chat_id = ?
              AND is_admin = 0
              AND date(created_at) BETWEEN date(?) AND date(?)
            GROUP BY user_id
            ORDER BY total DESC
            """,
            (chat_id, start.isoformat(), end.isoformat()),
        )
        rows = await cursor.fetchall()
        return rows


def format_report(title: str, rows: list[tuple]) -> str:
    if not rows:
        return f"📊 {title}\n\nBu davrda yozuvlar topilmadi."

    lines = [f"📊 {title}", ""]
    grand_total = 0
    for full_name, total, cnt in rows:
        grand_total += total
        lines.append(f"👤 {full_name} — {total:,} so'm ({cnt} ta so'rov)".replace(",", " "))
    lines.append("")
    lines.append(f"💰 Jami: {grand_total:,} so'm".replace(",", " "))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FSM (sana oralig'ini so'rash uchun)
# ---------------------------------------------------------------------------
class ReportStates(StatesGroup):
    waiting_start_date = State()
    waiting_end_date = State()


def report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Bugun", callback_data="rep_today"),
            InlineKeyboardButton(text="Kecha", callback_data="rep_yesterday"),
        ],
        [
            InlineKeyboardButton(text="Bu oy", callback_data="rep_month"),
            InlineKeyboardButton(text="Sana oralig'i", callback_data="rep_range"),
        ],
    ])


# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Salom! Men gaz-zapravka rasxodlarini kuzatuvchi botman.\n\n"
        "Guruhga qo'shilganimdan keyin xodimlar yozgan summalarni avtomatik "
        "saqlab boraman. Hisobotni ko'rish uchun /hisobot buyrug'ini yuboring."
    )


@dp.message(Command("hisobot"))
async def cmd_hisobot(message: Message):
    await message.answer("Qaysi davr uchun hisobot kerak?", reply_markup=report_keyboard())


@dp.callback_query(F.data == "rep_today")
async def rep_today(call: CallbackQuery):
    today = date.today()
    rows = await fetch_report(call.message.chat.id, today, today)
    await call.message.edit_text(format_report(f"Hisobot: bugun ({today:%d.%m.%Y})", rows))
    await call.answer()


@dp.callback_query(F.data == "rep_yesterday")
async def rep_yesterday(call: CallbackQuery):
    y = date.today() - timedelta(days=1)
    rows = await fetch_report(call.message.chat.id, y, y)
    await call.message.edit_text(format_report(f"Hisobot: kecha ({y:%d.%m.%Y})", rows))
    await call.answer()


@dp.callback_query(F.data == "rep_month")
async def rep_month(call: CallbackQuery):
    today = date.today()
    start = today.replace(day=1)
    rows = await fetch_report(call.message.chat.id, start, today)
    title = f"Hisobot: bu oy ({start:%d.%m.%Y} - {today:%d.%m.%Y})"
    await call.message.edit_text(format_report(title, rows))
    await call.answer()


@dp.callback_query(F.data == "rep_range")
async def rep_range(call: CallbackQuery, state: FSMContext):
    await state.set_state(ReportStates.waiting_start_date)
    await state.update_data(chat_id=call.message.chat.id)
    await call.message.answer("Boshlanish sanasini kiriting (masalan: 01.09.2026)")
    await call.answer()


@dp.message(ReportStates.waiting_start_date)
async def got_start_date(message: Message, state: FSMContext):
    try:
        start = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Sana formati noto'g'ri. Masalan: 01.09.2026 deb yozing.")
        return
    await state.update_data(start=start.isoformat())
    await state.set_state(ReportStates.waiting_end_date)
    await message.answer("Endi tugash sanasini kiriting (masalan: 26.09.2026)")


@dp.message(ReportStates.waiting_end_date)
async def got_end_date(message: Message, state: FSMContext):
    try:
        end = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Sana formati noto'g'ri. Masalan: 26.09.2026 deb yozing.")
        return
    data = await state.get_data()
    start = date.fromisoformat(data["start"])
    chat_id = data["chat_id"]
    rows = await fetch_report(chat_id, start, end)
    title = f"Hisobot: {start:%d.%m.%Y} - {end:%d.%m.%Y}"
    await message.answer(format_report(title, rows))
    await state.clear()


@dp.message(F.text & ~F.text.startswith("/"))
async def catch_group_message(message: Message):
    """Guruhdagi oddiy xabarlarni tekshirib, summa bo'lsa saqlaydi."""
    if message.chat.type not in ("group", "supergroup"):
        return  # shaxsiy chatdagi oddiy matnlarni e'tiborsiz qoldiramiz

    amount = parse_amount(message.text)
    if amount is None:
        return

    user = message.from_user
    is_admin = user.id in ADMIN_IDS
    full_name = user.full_name or (user.username or str(user.id))

    await save_request(
        chat_id=message.chat.id,
        user_id=user.id,
        full_name=full_name,
        username=user.username,
        amount=amount,
        raw_text=message.text,
        is_admin=is_admin,
    )
    log.info("Saqlandi: %s -> %s so'm (%s)", full_name, amount, message.text)


async def main():
    await init_db()
    log.info("Bot ishga tushdi (polling rejimida)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
