import asyncio
import logging
import os
import re
from datetime import datetime, date, timedelta

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# O'zingizning (admin/egasining) Telegram user_id'lari — vergul bilan ajratib
# Render'ning Environment bo'limida ADMIN_IDS o'zgaruvchisiga yozing,
# masalan: 123456789,987654321
# Bu ID'lardan kelgan xabarlar "xodim so'ragan summa" sifatida hisoblanmaydi,
# va faqat shu ID'lar /hisobot buyrug'ini ishlata oladi.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# Gaz-zapravka guruhingizning chat_id'si. Bot guruhga qo'shilgandan keyin
# guruhda "/groupid" deb yozib shu qiymatni olasiz, so'ng Render'ning
# Environment bo'limiga GROUP_CHAT_ID nomi bilan qo'ying. Shundan keyin
# botga shaxsiy (DM) yozib ham shu guruhning hisobotini ko'ra olasiz.
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
GROUP_CHAT_ID = int(GROUP_CHAT_ID) if GROUP_CHAT_ID and GROUP_CHAT_ID.lstrip("-").isdigit() else None

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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                has_photo INTEGER NOT NULL DEFAULT 0,
                file_id TEXT,
                caption TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def save_receipt(chat_id: int, user_id: int, full_name: str,
                        username: str | None, has_photo: bool,
                        file_id: str | None, caption: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO receipts (chat_id, user_id, full_name, username,
                                   has_photo, file_id, caption, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, full_name, username, int(has_photo),
             file_id, caption, datetime.now().isoformat()),
        )
        await db.commit()


async def fetch_receipt_users(chat_id: int, start: date | None = None,
                               end: date | None = None) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        if start is None:
            cursor = await db.execute(
                """
                SELECT DISTINCT user_id, full_name
                FROM receipts
                WHERE chat_id = ?
                ORDER BY full_name
                """,
                (chat_id,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT DISTINCT user_id, full_name
                FROM receipts
                WHERE chat_id = ? AND date(created_at) BETWEEN date(?) AND date(?)
                ORDER BY full_name
                """,
                (chat_id, start.isoformat(), end.isoformat()),
            )
        return await cursor.fetchall()


async def fetch_receipts(chat_id: int, user_id: int | None = None,
                          start: date | None = None,
                          end: date | None = None) -> list[tuple]:
    conditions = ["chat_id = ?"]
    params: list = [chat_id]

    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)

    if start is not None:
        conditions.append("date(created_at) BETWEEN date(?) AND date(?)")
        params.extend([start.isoformat(), end.isoformat()])

    query = (
        "SELECT full_name, has_photo, caption, created_at, file_id FROM receipts "
        f"WHERE {' AND '.join(conditions)} ORDER BY created_at ASC"
    )

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        return await cursor.fetchall()


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


async def fetch_recent(chat_id: int, limit: int = 15) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, full_name, amount, raw_text, created_at
            FROM requests
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return await cursor.fetchall()


async def delete_request(record_id: int, chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM requests WHERE id = ? AND chat_id = ?",
            (record_id, chat_id),
        )
        await db.commit()
        return cursor.rowcount > 0


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


def format_receipts(title: str, rows: list[tuple]) -> str:
    if not rows:
        return f"🧾 {title}\n\nHozircha cheklar/izohlar yo'q."

    lines = [f"🧾 {title}", ""]
    for full_name, has_photo, caption, created_at, file_id in rows:
        try:
            dt = datetime.fromisoformat(created_at)
            ts = dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            ts = created_at
        piece = "📷 chek rasmi (pastda)" if has_photo else "📝 izoh"
        if caption:
            piece += f": {caption}"
        lines.append(f"{ts} — {full_name} — {piece}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FSM (sana oralig'ini so'rash uchun)
# ---------------------------------------------------------------------------
class ReportStates(StatesGroup):
    waiting_start_date = State()
    waiting_end_date = State()


class ReceiptStates(StatesGroup):
    waiting_start_date = State()
    waiting_end_date = State()
    picking_user = State()


def receipt_period_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Bugun", callback_data=f"recvp:today:{target_chat_id}"),
            InlineKeyboardButton(text="Kecha", callback_data=f"recvp:yday:{target_chat_id}"),
        ],
        [
            InlineKeyboardButton(text="Bu oy", callback_data=f"recvp:month:{target_chat_id}"),
            InlineKeyboardButton(text="Hammasi (vaqt)", callback_data=f"recvp:all:{target_chat_id}"),
        ],
        [
            InlineKeyboardButton(text="Sana oralig'i", callback_data=f"recvp:range:{target_chat_id}"),
        ],
    ])


def report_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Bugun", callback_data=f"rep_today:{target_chat_id}"),
            InlineKeyboardButton(text="Kecha", callback_data=f"rep_yesterday:{target_chat_id}"),
        ],
        [
            InlineKeyboardButton(text="Bu oy", callback_data=f"rep_month:{target_chat_id}"),
            InlineKeyboardButton(text="Sana oralig'i", callback_data=f"rep_range:{target_chat_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Yozuvni o'chirish", callback_data=f"rep_delete:{target_chat_id}"),
        ],
        [
            InlineKeyboardButton(text="🧾 Cheklar", callback_data=f"rep_receipts:{target_chat_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Hisobot")]],
    resize_keyboard=True,
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Salom! Men gaz-zapravka rasxodlarini kuzatuvchi botman.\n\n"
        "Guruhga qo'shilganimdan keyin xodimlar yozgan summalarni avtomatik "
        "saqlab boraman. Hisobotni ko'rish uchun pastdagi \"📊 Hisobot\" "
        "tugmasini bosing (yoki /hisobot deb yozing).",
        reply_markup=main_menu_keyboard,
    )


async def show_hisobot_menu(message: Message):
    user_id = message.from_user.id

    if ADMIN_IDS and user_id not in ADMIN_IDS:
        # Admin bo'lmagan xodimlar hisobotni ko'ra olmaydi
        return

    if message.chat.type == "private":
        if GROUP_CHAT_ID is None:
            await message.answer(
                "GROUP_CHAT_ID hali sozlanmagan. Avval guruhda \"/groupid\" "
                "deb yozib chat_id'ni oling, so'ng uni Render'ning "
                "Environment bo'limiga GROUP_CHAT_ID nomi bilan qo'shing."
            )
            return
        target_chat_id = GROUP_CHAT_ID
    else:
        target_chat_id = message.chat.id

    await message.answer("Qaysi davr uchun hisobot kerak?", reply_markup=report_keyboard(target_chat_id))


@dp.message(Command("hisobot"))
async def cmd_hisobot(message: Message):
    await show_hisobot_menu(message)


@dp.message(F.text == "📊 Hisobot")
async def btn_hisobot(message: Message):
    await show_hisobot_menu(message)


@dp.message(Command("groupid"))
async def cmd_groupid(message: Message):
    if message.chat.type in ("group", "supergroup"):
        await message.answer(f"Bu guruhning ID raqami: {message.chat.id}")


def recent_keyboard(target_chat_id: int, rows: list[tuple]) -> InlineKeyboardMarkup:
    buttons = []
    for record_id, full_name, amount, raw_text, created_at in rows:
        ts = created_at[11:16] if len(created_at) > 16 else created_at
        label = f"🗑 {ts} {full_name} — {amount:,} so'm".replace(",", " ")
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"del:{record_id}:{target_chat_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("oxirgilar"))
async def cmd_oxirgilar(message: Message):
    user_id = message.from_user.id
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        return

    if message.chat.type == "private":
        if GROUP_CHAT_ID is None:
            await message.answer(
                "GROUP_CHAT_ID hali sozlanmagan. Avval guruhda \"/groupid\" "
                "deb yozib chat_id'ni oling, so'ng uni Render'ning "
                "Environment bo'limiga GROUP_CHAT_ID nomi bilan qo'shing."
            )
            return
        target_chat_id = GROUP_CHAT_ID
    else:
        target_chat_id = message.chat.id

    rows = await fetch_recent(target_chat_id)
    if not rows:
        await message.answer("Hozircha yozuvlar yo'q.")
        return

    await message.answer(
        "Oxirgi yozuvlar. O'chirish uchun kerakli qatorni bosing:",
        reply_markup=recent_keyboard(target_chat_id, rows),
    )


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    if ADMIN_IDS and call.from_user.id not in ADMIN_IDS:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    _, record_id, target_chat_id = call.data.split(":")
    ok = await delete_request(int(record_id), int(target_chat_id))

    rows = await fetch_recent(int(target_chat_id))
    if not rows:
        await call.message.edit_text("Hozircha yozuvlar yo'q.")
    else:
        await call.message.edit_text(
            "Oxirgi yozuvlar. O'chirish uchun kerakli qatorni bosing:",
            reply_markup=recent_keyboard(int(target_chat_id), rows),
        )
    await call.answer("O'chirildi ✅" if ok else "Topilmadi")


@dp.callback_query(F.data.startswith("rep_today:"))
async def rep_today(call: CallbackQuery):
    target_chat_id = int(call.data.split(":", 1)[1])
    today = date.today()
    rows = await fetch_report(target_chat_id, today, today)
    await call.message.edit_text(format_report(f"Hisobot: bugun ({today:%d.%m.%Y})", rows))
    await call.answer()


@dp.callback_query(F.data.startswith("rep_yesterday:"))
async def rep_yesterday(call: CallbackQuery):
    target_chat_id = int(call.data.split(":", 1)[1])
    y = date.today() - timedelta(days=1)
    rows = await fetch_report(target_chat_id, y, y)
    await call.message.edit_text(format_report(f"Hisobot: kecha ({y:%d.%m.%Y})", rows))
    await call.answer()


@dp.callback_query(F.data.startswith("rep_month:"))
async def rep_month(call: CallbackQuery):
    target_chat_id = int(call.data.split(":", 1)[1])
    today = date.today()
    start = today.replace(day=1)
    rows = await fetch_report(target_chat_id, start, today)
    title = f"Hisobot: bu oy ({start:%d.%m.%Y} - {today:%d.%m.%Y})"
    await call.message.edit_text(format_report(title, rows))
    await call.answer()


@dp.callback_query(F.data.startswith("rep_delete:"))
async def rep_delete(call: CallbackQuery):
    target_chat_id = int(call.data.split(":", 1)[1])
    rows = await fetch_recent(target_chat_id)
    if not rows:
        await call.message.edit_text("Hozircha yozuvlar yo'q.")
    else:
        await call.message.edit_text(
            "Oxirgi yozuvlar. O'chirish uchun kerakli qatorni bosing:",
            reply_markup=recent_keyboard(target_chat_id, rows),
        )
    await call.answer()


@dp.callback_query(F.data.startswith("rep_receipts:"))
async def rep_receipts(call: CallbackQuery):
    target_chat_id = int(call.data.split(":", 1)[1])
    await call.message.edit_text(
        "Cheklarni qaysi davr uchun ko'rmoqchisiz?",
        reply_markup=receipt_period_keyboard(target_chat_id),
    )
    await call.answer()


async def _show_receipt_users(message: Message, state: FSMContext,
                               target_chat_id: int, start: date | None,
                               end: date | None, title: str):
    users = await fetch_receipt_users(target_chat_id, start, end)

    if not users:
        await message.edit_text(f"🧾 {title}\n\nBu davrda cheklar/izohlar yo'q.")
        await state.clear()
        return

    # Callback_data uzunligi cheklangani uchun user_id'larni FSM holatida
    # saqlaymiz, tugmada esa faqat qisqa index ishlatiladi.
    user_map = {str(i): user_id for i, (user_id, _) in enumerate(users)}
    await state.update_data(
        chat_id=target_chat_id,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        title=title,
        user_map=user_map,
    )
    await state.set_state(ReceiptStates.picking_user)

    buttons = [
        [InlineKeyboardButton(text=full_name, callback_data=f"recvpick:{i}")]
        for i, (user_id, full_name) in enumerate(users)
    ]
    buttons.append([InlineKeyboardButton(text="👥 Hammasi", callback_data="recvpick:all")])

    await message.edit_text(
        f"🧾 {title}\n\nKimning cheklarini ko'rmoqchisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("recvp:"))
async def recvp_period(call: CallbackQuery, state: FSMContext):
    _, period, target_chat_id = call.data.split(":")
    target_chat_id = int(target_chat_id)
    today = date.today()

    if period == "today":
        await _show_receipt_users(call.message, state, target_chat_id, today, today,
                                   f"Cheklar: bugun ({today:%d.%m.%Y})")
    elif period == "yday":
        y = today - timedelta(days=1)
        await _show_receipt_users(call.message, state, target_chat_id, y, y,
                                   f"Cheklar: kecha ({y:%d.%m.%Y})")
    elif period == "month":
        start = today.replace(day=1)
        await _show_receipt_users(call.message, state, target_chat_id, start, today,
                                   f"Cheklar: bu oy ({start:%d.%m.%Y} - {today:%d.%m.%Y})")
    elif period == "all":
        await _show_receipt_users(call.message, state, target_chat_id, None, None,
                                   "Cheklar: hammasi (vaqt)")
    elif period == "range":
        await state.set_state(ReceiptStates.waiting_start_date)
        await state.update_data(chat_id=target_chat_id)
        await call.message.answer("Boshlanish sanasini kiriting (masalan: 01.09.2026)")

    await call.answer()


@dp.message(ReceiptStates.waiting_start_date)
async def receipt_got_start_date(message: Message, state: FSMContext):
    try:
        start = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Sana formati noto'g'ri. Masalan: 01.09.2026 deb yozing.")
        return
    await state.update_data(range_start=start.isoformat())
    await state.set_state(ReceiptStates.waiting_end_date)
    await message.answer("Endi tugash sanasini kiriting (masalan: 26.09.2026)")


@dp.message(ReceiptStates.waiting_end_date)
async def receipt_got_end_date(message: Message, state: FSMContext):
    try:
        end = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Sana formati noto'g'ri. Masalan: 26.09.2026 deb yozing.")
        return
    data = await state.get_data()
    start = date.fromisoformat(data["range_start"])
    target_chat_id = data["chat_id"]
    title = f"Cheklar: {start:%d.%m.%Y} - {end:%d.%m.%Y}"

    users = await fetch_receipt_users(target_chat_id, start, end)
    if not users:
        await message.answer(f"🧾 {title}\n\nBu davrda cheklar/izohlar yo'q.")
        await state.clear()
        return

    user_map = {str(i): user_id for i, (user_id, _) in enumerate(users)}
    await state.update_data(start=start.isoformat(), end=end.isoformat(),
                             title=title, user_map=user_map)
    await state.set_state(ReceiptStates.picking_user)

    buttons = [
        [InlineKeyboardButton(text=full_name, callback_data=f"recvpick:{i}")]
        for i, (user_id, full_name) in enumerate(users)
    ]
    buttons.append([InlineKeyboardButton(text="👥 Hammasi", callback_data="recvpick:all")])
    await message.answer(
        f"🧾 {title}\n\nKimning cheklarini ko'rmoqchisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(ReceiptStates.picking_user, F.data.startswith("recvpick:"))
async def recvpick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_chat_id = data["chat_id"]
    title = data["title"]
    start = date.fromisoformat(data["start"]) if data.get("start") else None
    end = date.fromisoformat(data["end"]) if data.get("end") else None

    choice = call.data.split(":", 1)[1]

    if choice == "all":
        rows = await fetch_receipts(target_chat_id, None, start, end)
        text = format_receipts(f"{title} — hammasi", rows)
    else:
        user_id = data["user_map"].get(choice)
        rows = await fetch_receipts(target_chat_id, user_id, start, end)
        full_name = rows[0][0] if rows else ""
        text = format_receipts(f"{title} — {full_name}", rows)

    await call.message.edit_text(text)
    await call.answer()
    await state.clear()

    # Rasmi bor yozuvlarni haqiqiy rasm sifatida, izohi bilan yuboramiz
    for full_name, has_photo, caption, created_at, file_id in rows:
        if has_photo and file_id:
            try:
                dt = datetime.fromisoformat(created_at)
                ts = dt.strftime("%d.%m.%Y %H:%M")
            except ValueError:
                ts = created_at
            cap = f"{ts} — {full_name}"
            if caption:
                cap += f"\n{caption}"
            await call.message.answer_photo(file_id, caption=cap)


@dp.callback_query(F.data.startswith("rep_range:"))
async def rep_range(call: CallbackQuery, state: FSMContext):
    target_chat_id = int(call.data.split(":", 1)[1])
    await state.set_state(ReportStates.waiting_start_date)
    await state.update_data(chat_id=target_chat_id)
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


@dp.message(
    F.chat.type.in_({"group", "supergroup"})
    & F.reply_to_message
    & F.from_user.id.in_(ADMIN_IDS)
)
async def admin_payment_reply(message: Message):
    """Admin guruhda kimningdir xabariga javob (reply) qilib to'lov/chek
    yuborsa, bot o'sha xodimni belgilab chek so'raydi."""
    replied_user = message.reply_to_message.from_user
    if replied_user is None or replied_user.id in ADMIN_IDS or replied_user.is_bot:
        return  # o'ziga-o'zi yoki botga javob bo'lsa e'tiborsiz

    mention = f'<a href="tg://user?id={replied_user.id}">{replied_user.full_name}</a>'
    await message.answer(
        f"🔔 {mention}, to'lovingiz amalga oshirildi. "
        f"Iltimos, chek rasmini yoki tasdiqni shu yerga yuboring.",
        parse_mode="HTML",
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def catch_group_message(message: Message):
    """Guruhdagi oddiy xabarlarni tekshirib, summa yoki chek/izoh bo'lsa saqlaydi."""
    if message.chat.type not in ("group", "supergroup"):
        return  # shaxsiy chatdagi oddiy matnlarni e'tiborsiz qoldiramiz

    user = message.from_user
    is_admin = user.id in ADMIN_IDS
    full_name = user.full_name or (user.username or str(user.id))

    # Admin (chek tashlovchi) emas, faqat oddiy xodimlarning "chek"
    # so'zi bilan yozgan xabarlari (masalan "chek yo'q berdi") alohida
    # chek/izoh sifatida saqlanadi — summa sifatida hisoblanmaydi.
    if not is_admin and "chek" in message.text.lower():
        await save_receipt(
            chat_id=message.chat.id,
            user_id=user.id,
            full_name=full_name,
            username=user.username,
            has_photo=False,
            file_id=None,
            caption=message.text,
        )
        log.info("Chek izohi saqlandi: %s -> %s", full_name, message.text)
        return

    amount = parse_amount(message.text)
    if amount is None:
        return

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


@dp.message(F.photo)
async def catch_group_photo(message: Message):
    """Guruhda xodim (admin emas) tashlagan chek rasmini saqlaydi."""
    if message.chat.type not in ("group", "supergroup"):
        return

    user = message.from_user
    if user.id in ADMIN_IDS:
        return  # admin/egasi tashlagan chek rasmlari hisobga qo'shilmaydi

    full_name = user.full_name or (user.username or str(user.id))
    largest_photo = message.photo[-1]

    await save_receipt(
        chat_id=message.chat.id,
        user_id=user.id,
        full_name=full_name,
        username=user.username,
        has_photo=True,
        file_id=largest_photo.file_id,
        caption=message.caption,
    )
    log.info("Chek rasmi saqlandi: %s", full_name)


async def health(request):
    return web.Response(text="Bot ishlayapti")


async def start_web_server():
    """Render.com (va shunga o'xshash) 'Web Service' talab qiladigan
    portni tinglaydigan minimal server. Botning asosiy ishiga (Telegram
    polling) hech qanday aloqasi yo'q — faqat 'xizmat tirik' deb
    ko'rsatish uchun kerak."""
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health-check server %s portda ishga tushdi", port)


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "BOT_TOKEN muhit o'zgaruvchisi topilmadi yoki bo'sh. "
            "Render'dagi Environment bo'limida BOT_TOKEN qiymatini tekshiring."
        )
    await init_db()
    await start_web_server()
    log.info("Bot ishga tushdi (polling rejimida)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
