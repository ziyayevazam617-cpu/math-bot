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
                grade TEXT DEFAULT '11-sinf',
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
            ("grade", "TEXT DEFAULT '11-sinf'"),
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
                    "INSERT INTO users (user_id, first_name, correct, wrong, streak, grade) VALUES (?, ?, 0, 0, 0, '11-sinf')",
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


# ==================== MISOLLAR VA YORDAMCHI FUNKSIYALAR ====================

def generate_options(correct_answer: int) -> list:
    """To'g'ri va unikal mantiqiy noto'g'ri variantlar hosil qiladi."""
    options = {correct_answer}
    attempts = 0
    while len(options) < 4 and attempts < 100:
        attempts += 1
        delta = random.choice([-2 * correct_answer, -correct_answer, -5, -2, -1, 1, 2, 3, 5, 10])
        fake = correct_answer + delta
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


# ==================== 11-SINF MANTIQIY FIKRLASH GENERATORLARI ====================

def ex_derivative_tangent(grade):
    """1. Hosilaning geometrik ma'nosi: Uranma og'ish burchagi k = tg(alpha) = f'(x0)"""
    a = random.randint(1, 4)
    b = random.randint(1, 5)
    x0 = random.randint(1, 3)
    # f(x) = a*x^2 + b*x => f'(x) = 2*a*x + b
    ans = 2 * a * x0 + b
    return f"y = {a}x² + {b}x funksiya grafigiga x₀ = {x0} nuqtada o'tkazilgan urinmaning burchak koeffitsiyenti (k = tgα) ni toping.", ans

def ex_integral_area(grade):
    """2. Aniq integral va egri chiziqli trapetsiya yuzi"""
    a = random.randint(1, 3)
    b = random.randint(2, 4)
    # y = 3*a*x^2, [0, b] kesmada S = a * b^3
    ans = a * (b ** 3)
    return f"y = {3*a}x² egri chiziq, y = 0 va x = {b} to'g'ri chiziqlari bilan chegaralangan shakl yuzini toping.", ans

def ex_log_properties(grade):
    """3. Logarifm xossalari: log_a(b) + log_a(c)"""
    base = random.choice([2, 3, 5])
    x1 = random.randint(1, 3)
    x2 = random.randint(1, 3)
    ans = x1 + x2
    val1 = base ** x1
    val2 = base ** x2
    return f"log_{base}({val1}) + log_{base}({val2}) ifodaning qiymatini hisoblang.", ans

def ex_trig_identity_logic(grade):
    """4. Trigonometrik ayniyat: a * (sin^2 x + cos^2 x) + a * tg x * ctg x"""
    a = random.randint(2, 9)
    ans = 2 * a
    return f"{a} · (sin²α + cos²α) + {a} · tgα · ctgα ifodaning qiymatini toping.", ans

def ex_limit_indeterminate(grade):
    """5. Limitlardagi 0/0 noaniqlikni yo'qotish: lim_(x->a) (x^2 - a^2)/(x - a) = 2*a"""
    a = random.randint(2, 7)
    ans = 2 * a
    return f"lim_(x -> {a}) (x² - {a**2}) / (x - {a}) limitning qiymatini hisoblang.", ans

def ex_comb_probability_logic(grade):
    """6. Kombinatorika: C(n, 2) teramlar soni"""
    n = random.randint(5, 8)
    ans = math.comb(n, 2)
    return f"Savatda {n} xil meva bor. Ulardan 2 tasini necha xil usulda tanlab olish mumkin (C({n}, 2))?", ans

def ex_stereometry_cube_logic(grade):
    """7. Stereometriya: Kub sirtidan uning hajmiga o'tish"""
    a = random.randint(2, 5)
    ans = a ** 3
    return f"To'la sirti S = {6 * (a**2)} sm² bo'lgan kubning hajmi (V) qanchaga teng?", ans

ELEVEN_GRADE_GENERATORS = [
    ex_derivative_tangent,
    ex_integral_area,
    ex_log_properties,
    ex_trig_identity_logic,
    ex_limit_indeterminate,
    ex_comb_probability_logic,
    ex_stereometry_cube_logic
]


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
        f"Salom {message.from_user.first_name}!\n"
        f"11-sinf Chuqurlashtirilgan Matematika botiga xush kelibsiz!\n\n"
        f"Bot to'liq 11-sinf dasturi bo'yicha mantiqiy misollar bilan yangilandi.\n\n"
        f"Darajangiz: {user.get('grade', '11-sinf')}. Boshlash uchun tugmalardan birini bosing.",
        reply_markup=keyboard
    )

@dp.message(F.text == "🧮 Misol yechish")
async def start_quiz(message: types.Message):
    user = await get_user(message.from_user.id)
    
    gen_fn = random.choice(ELEVEN_GRADE_GENERATORS)
    question, ans = gen_fn(user.get("grade", "11-sinf"))
    
    await update_user(user["user_id"], current_answer=ans, current_topic="11_math_logic", current_question=question)
    
    opts = generate_options(ans)
    kb = get_answer_keyboard("11_math_logic", opts)
    await message.answer(f"🧠 **11-Sinf Mantiqiy Misoli:**\n{question}", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "next_q")
async def next_question_cb(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    gen_fn = random.choice(ELEVEN_GRADE_GENERATORS)
    
    question, ans = gen_fn(user.get("grade", "11-sinf"))
    await update_user(user["user_id"], current_answer=ans, current_topic="11_math_logic", current_question=question)
    
    opts = generate_options(ans)
    kb = get_answer_keyboard("11_math_logic", opts)
    await callback.message.edit_text(f"🧠 **11-Sinf Mantiqiy Misoli:**\n{question}", reply_markup=kb, parse_mode="Markdown")
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
            f"✅ **To'g'ri javob!** 🎉\n\nJamlangan ball: {new_correct}\nKetma-ket to'g'ri javoblar (Streak): {new_streak}", 
            parse_mode="Markdown"
        )
    else:
        new_wrong = user["wrong"] + 1
        await update_user(user["user_id"], wrong=new_wrong, streak=0)
        await callback.message.edit_text(
            f"❌ **Noto'g'ri!**\n\nTo'g'ri javob: **{user['current_answer']}** edi.", 
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.message(F.text == "📊 Statistikam")
async def show_stats(message: types.Message):
    user = await get_user(message.from_user.id)
    total = user['correct'] + user['wrong']
    percent = round((user['correct'] / total * 100), 1) if total > 0 else 0
    
    await message.answer(
        f"📊 **Sizning statistikangiz:**\n\n"
        f"✅ To'g'ri javoblar: {user['correct']}\n"
        f"❌ Noto'g'ri javoblar: {user['wrong']}\n"
        f"🎯 Aniqlik ko'rsatkichi: {percent}%\n"
        f"🔥 Ketma-ketlik (Streak): {user['streak']}\n"
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
