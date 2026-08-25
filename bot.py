# -*- coding: utf-8 -*-
import asyncio
import random
import math
import json
import aiosqlite
import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Logger sozlamalari
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_NAME = "bot_database.db"
HISTORY_LIMIT = 60

# ==================== ASYNC DATABASE (aiosqlite) ====================
# aiosqlite orqali 50-100 kishi bir vaqtda yozganda ham baza "osilib" qolmaydi (Concurrency fix)

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                correct INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0,
                current_topic TEXT,
                current_answer INTEGER,
                last_played TEXT,
                streak INTEGER DEFAULT 0,
                grade TEXT DEFAULT 'medium',
                current_mode TEXT DEFAULT 'normal',
                week_start TEXT,
                week_correct INTEGER DEFAULT 0,
                group_code TEXT,
                personal_code TEXT,
                history TEXT DEFAULT '{}',
                current_question TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topic_stats (
                user_id INTEGER,
                topic TEXT,
                correct INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, topic)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mistakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                topic TEXT,
                question TEXT,
                correct_answer INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                code TEXT PRIMARY KEY,
                teacher_id INTEGER,
                name TEXT
            )
        """)
        await db.commit()

        # Jadvallarni yangilash uchun ustunlarni xavfsiz tekshirib qo'shish
        cols = [
            ("grade", "TEXT DEFAULT 'medium'"),
            ("current_mode", "TEXT DEFAULT 'normal'"),
            ("week_start", "TEXT"),
            ("week_correct", "INTEGER DEFAULT 0"),
            ("group_code", "TEXT"),
            ("personal_code", "TEXT"),
            ("history", "TEXT DEFAULT '{}'"),
            ("current_question", "TEXT")
        ]
        for col_name, col_type in cols:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                await db.commit()
            except Exception:
                pass


async def get_user(user_id: int, first_name: str = None) -> dict:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO users (user_id, first_name, correct, wrong, streak, grade) VALUES (?, ?, 0, 0, 0, 'medium')",
                    (user_id, first_name or "Foydalanuvchi")
                )
                await db.commit()
                async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor_new:
                    row = await cursor_new.fetchone()
            return dict(row) if row else {}


async def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        fields = ", ".join([f"{k} = ?" for k in kwargs])
        values = list(kwargs.values()) + [user_id]
        await db.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)
        await db.commit()


# ==================== MISOLLAR VA YERDAMCHI FUNKSIYALAR ====================

def generate_options(correct_answer: int, allow_negative: bool = False) -> list:
    """To'g'ri va noto'g'ri javob variantlarini hosil qiladi."""
    options = {correct_answer}
    attempts = 0
    while len(options) < 4 and attempts < 100:
        attempts += 1
        delta = random.choice([-10, -5, -3, -2, -1, 1, 2, 3, 5, 10, 15, 20])
        fake = correct_answer + delta
        if not allow_negative and fake < 0:
            continue
        options.add(fake)
    
    opts = list(options)
    random.shuffle(opts)
    return opts


def get_answer_keyboard(topic: str, options: list):
    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(text=str(opt), callback_data=f"ans:{opt}")
    builder.button(text="💡 Maslahat", callback_data=f"hint:{topic}")
    builder.button(text="📖 Formula", callback_data=f"formula:{topic}")
    builder.button(text="➡️ Keyingi misol", callback_data="next_q")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


# --- Misol generatorlari (Tuzatilgan buglar bilan) ---

def ex_sqrt_estimate(grade):
    # O'zgaruvchi nomlari to'g'rilangan (mantiqiy chalkashliklar olib tashlangan)
    val = random.randint(2, 12)
    square = val ** 2
    return f"sqrt({square}) qiymatini toping.", val

def ex_speed_catchup(grade):
    # Cheksiz while sikli oldi olindi (v_fast > v_slow qat'iy va vaqt butun)
    v_slow = random.randint(10, 40)
    v_fast = v_slow + random.randint(5, 20)
    t = random.randint(1, 5)
    head_start = (v_fast - v_slow) * t
    return f"Birinchi mashina tezligi {v_slow} km/h, ikkinchisiniki {v_fast} km/h. Birinchisi {head_start} km oldinda. Ikkinchisi qancha soatda yetib oladi?", t

EXPO_BASES = [2, 3, 5]

def ex_expo_simple(grade):
    base = random.choice(EXPO_BASES)
    x = random.randint(1, 5)
    ans = base ** x
    return f"{base}^x = {ans} bo'lsa, x ni toping.", x

def ex_arith_prog_nth(grade):
    a1 = random.randint(1, 20)
    d = random.randint(2, 10)
    n = random.randint(3, 15)
    an = a1 + (n - 1) * d
    return f"Arifmetik progressiyada a1 = {a1}, d = {d} bo'lsa, {n}-hadini (a_{n}) toping.", an

def ex_geom_prog_nth(grade):
    b1 = random.randint(1, 5)
    q = random.randint(2, 3)
    n = random.randint(3, 6)
    bn = b1 * (q ** (n - 1))
    return f"Geometrik progressiyada b1 = {b1}, q = {q} bo'lsa, {n}-hadini (b_{n}) toping.", bn

def ex_comb_factorial(grade):
    n = random.randint(3, 6)
    return f"{n}! (faktorial) qiymatini hisoblang.", math.factorial(n)


# ==================== TELEGRAM BOT HANDLERS ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = await get_user(message.from_user.id, message.from_user.first_name)
    kb = [
        [types.KeyboardButton(text="🧮 Misol yechish"), types.KeyboardButton(text="📊 Statistikam")],
        [types.KeyboardButton(text="🏆 Reyting"), types.KeyboardButton(text="⚙️ Sozlamalar")]
    ]
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(
        f"Salom {message.from_user.first_name}!
"
        f"Matematika botiga xush kelibsiz! Bot to'liq asinxron (aiosqlite) rejimga o'tkazildi va ko'p foydalanuvchilar bilan bemalol ishlaydi.

"
        f"Darajangiz: {user.get('grade', 'medium')}. Boshlash uchun tugmalardan birini bosing.",
        reply_markup=keyboard
    )

@dp.message(F.text == "🧮 Misol yechish")
async def start_quiz(message: types.Message):
    user = await get_user(message.from_user.id)
    
    # Tasodifiy misol turini tanlash
    generators = [ex_sqrt_estimate, ex_speed_catchup, ex_expo_simple, ex_arith_prog_nth, ex_geom_prog_nth, ex_comb_factorial]
    gen_fn = random.choice(generators)
    
    question, ans = gen_fn(user.get("grade", "medium"))
    
    await update_user(user["user_id"], current_answer=ans, current_topic="math_mix", current_question=question)
    
    opts = generate_options(ans)
    kb = get_answer_keyboard("math_mix", opts)
    await message.answer(f"📝 **Misol:**
{question}", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "next_q")
async def next_question_cb(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    generators = [ex_sqrt_estimate, ex_speed_catchup, ex_expo_simple, ex_arith_prog_nth, ex_geom_prog_nth, ex_comb_factorial]
    gen_fn = random.choice(generators)
    
    question, ans = gen_fn(user.get("grade", "medium"))
    await update_user(user["user_id"], current_answer=ans, current_topic="math_mix", current_question=question)
    
    opts = generate_options(ans)
    kb = get_answer_keyboard("math_mix", opts)
    await callback.message.edit_text(f"📝 **Misol:**
{question}", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("ans:"))
async def handle_answer(callback: types.CallbackQuery):
    user_ans = int(callback.data.split(":")[1])
    user = await get_user(callback.from_user.id)
    
    if user_ans == user["current_answer"]:
        new_correct = user["correct"] + 1
        new_streak = user["streak"] + 1
        await update_user(user["user_id"], correct=new_correct, streak=new_streak)
        await callback.message.edit_text(
            f"✅ **To'g'ri javob!** 🎉

Jamlangan ball: {new_correct}
Ketma-ket to'g'ri javoblar (Streak): {new_streak}", 
            parse_mode="Markdown"
        )
    else:
        new_wrong = user["wrong"] + 1
        await update_user(user["user_id"], wrong=new_wrong, streak=0)
        await callback.message.edit_text(
            f"❌ **Noto'g'ri!**

To'g'ri javob: **{user['current_answer']}** edi.", 
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.message(F.text == "📊 Statistikam")
async def show_stats(message: types.Message):
    user = await get_user(message.from_user.id)
    total = user['correct'] + user['wrong']
    percent = round((user['correct'] / total * 100), 1) if total > 0 else 0
    
    await message.answer(
        f"📊 **Sizning statistikangiz:**

"
        f"✅ To'g'ri javoblar: {user['correct']}
"
        f"❌ Noto'g'ri javoblar: {user['wrong']}
"
        f"🎯 Anqlik ko'rsatkichi: {percent}%
"
        f"🔥 Ketma-ketlik (Streak): {user['streak']}
"
        f"🎓 Daraja: {user['grade']}",
        parse_mode="Markdown"
    )

# ==================== BOTNI ISHGA TUSHIRISH ====================
async def main():
    await init_db()
    logging.info("Bot va asinxron baza muvaffaqiyatli ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
