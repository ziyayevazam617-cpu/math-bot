import asyncio
import random
import math
import sqlite3
import os
from datetime import date, timedelta, datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ==================== DATABASE ====================
DB_NAME = "bot_database.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
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
            week_correct INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS topic_stats (
            user_id INTEGER,
            topic TEXT,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, topic)
        )
    """)
    conn.commit()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            topic TEXT,
            question TEXT,
            correct_answer INTEGER
        )
    """)
    conn.commit()
    # Eski database'larda ba'zi ustunlar bo'lmasligi mumkin - qo'shib qo'yamiz
    for col_def in [
        "grade TEXT DEFAULT 'medium'",
        "current_mode TEXT DEFAULT 'normal'",
        "week_start TEXT",
        "week_correct INTEGER DEFAULT 0",
    ]:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # ustun allaqachon bor
    conn.close()


def get_user(user_id, first_name=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            "INSERT INTO users (user_id, first_name, correct, wrong, streak, grade) VALUES (?, ?, 0, 0, 0, 'medium')",
            (user_id, first_name)
        )
        conn.commit()
        row = (user_id, first_name, 0, 0, None, None, None, 0, "medium", "normal", None, 0)

    conn.close()
    return {
        "user_id": row[0],
        "first_name": row[1],
        "correct": row[2],
        "wrong": row[3],
        "current_topic": row[4],
        "current_answer": row[5],
        "last_played": row[6],
        "streak": row[7],
        "grade": row[8] if len(row) > 8 and row[8] else "medium",
        "current_mode": row[9] if len(row) > 9 and row[9] else "normal",
        "week_start": row[10] if len(row) > 10 else None,
        "week_correct": row[11] if len(row) > 11 and row[11] else 0,
    }


def update_user(user_id, **kwargs):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    fields = ", ".join([f"{k} = ?" for k in kwargs])
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)
    conn.commit()
    conn.close()


def get_top_users(limit=10):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT first_name, correct, wrong FROM users
        ORDER BY correct DESC LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_streak(user_id):
    """Har kuni birinchi marta faollik ko'rsatganda streakni yangilaydi."""
    user = get_user(user_id)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    last_played = user["last_played"]

    if last_played == today:
        # Bugun allaqachon hisoblangan
        return user["streak"]
    elif last_played == yesterday:
        # Ketma-ket kun - streak davom etadi
        new_streak = user["streak"] + 1
    else:
        # Streak uzilgan yoki birinchi marta
        new_streak = 1

    update_user(user_id, last_played=today, streak=new_streak)
    return new_streak


def get_monday(d):
    return (d - timedelta(days=d.weekday())).isoformat()


def add_weekly_correct(user_id):
    """Haftalik hisoblagichni yangilaydi, agar yangi hafta boshlangan bo'lsa nolldan boshlaydi."""
    user = get_user(user_id)
    this_monday = get_monday(date.today())

    if user["week_start"] != this_monday:
        update_user(user_id, week_start=this_monday, week_correct=1)
    else:
        update_user(user_id, week_correct=(user["week_correct"] or 0) + 1)


def get_weekly_top(limit=10):
    this_monday = get_monday(date.today())
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT first_name, week_correct FROM users
        WHERE week_start = ? AND week_correct > 0
        ORDER BY week_correct DESC LIMIT ?
    """, (this_monday, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_topic_stat(user_id, topic, correct_delta=0, wrong_delta=0):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT correct, wrong FROM topic_stats WHERE user_id = ? AND topic = ?", (user_id, topic))
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO topic_stats (user_id, topic, correct, wrong) VALUES (?, ?, ?, ?)",
            (user_id, topic, correct_delta, wrong_delta)
        )
    else:
        cursor.execute(
            "UPDATE topic_stats SET correct = correct + ?, wrong = wrong + ? WHERE user_id = ? AND topic = ?",
            (correct_delta, wrong_delta, user_id, topic)
        )
    conn.commit()
    conn.close()


def get_topic_stats(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT topic, correct, wrong FROM topic_stats WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def log_mistake(user_id, topic, question, correct_answer):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO mistakes (user_id, topic, question, correct_answer) VALUES (?, ?, ?, ?)",
        (user_id, topic, question, correct_answer)
    )
    conn.commit()
    conn.close()


def get_recent_mistakes(user_id, limit=8):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT topic, question, correct_answer FROM mistakes WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_active_user_ids():
    """Kunlik eslatma uchun - kamida bitta marta o'ynagan barcha foydalanuvchilar."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, last_played FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows


# ==================== TOPICS ====================
TOPICS = {
    "add_sub": "➕ Qo'shish/Ayirish",
    "mul_div": "✖️ Ko'paytirish/Bo'lish",
    "percent": "% Foizlar",
    "fraction": "½ Kasrlar",
    "power": "x² Darajalar",
    "sqrt": "√ Kvadrat ildiz",
    "linear_eq": "🔤 Chiziqli tenglama",
    "quad_eq": "🔤 Kvadrat tenglama",
    "triangle": "🔺 Uchburchak",
    "rectangle": "▭ To'rtburchak",
    "circle": "⭕ Doira",
    "ratio": "⚖️ Nisbat",
    "average": "📊 O'rtacha qiymat",
    "negative": "➖ Manfiy sonlar",
    "speed": "🚗 Tezlik-vaqt-masofa",
    "bank_percent": "🏦 Foiz o'sishi",
    "trig": "📐 Trigonometriya",
    "log": "📈 Logarifm",
    "arith_prog": "🔢 Arifmetik progressiya",
    "geom_prog": "🔢 Geometrik progressiya",
}


GRADE_LABELS = {
    "easy": "🟢 5-7 sinf",
    "medium": "🟡 8-9 sinf",
    "hard": "🔴 10-11 sinf",
}

HINTS = {
    "add_sub": "Sonlarni raqam ustuniga joylab, o'ngdan chapga qo'shing/ayiring.",
    "mul_div": "Ko'paytirish jadvalini eslang, bo'lishda qaysi songa necha marta sig'ishini toping.",
    "percent": "Foizni 100 ga bo'lib, songa ko'paytiring: son × foiz ÷ 100.",
    "fraction": "Avval sonni maxrajga bo'ling, keyin suratga ko'paytiring.",
    "power": "Daraja - sonni o'zi bilan necha marta ko'paytirish kerakligini bildiradi.",
    "sqrt": "Qaysi son o'zi bilan ko'paytirilganda shu songa teng bo'lishini toping.",
    "linear_eq": "Avval erkin hadni ikkala tomondan ayiring, keyin x oldidagi songa bo'ling.",
    "quad_eq": "Ildizlar yig'indisi -b, ko'paytmasi c ga teng (Vieta teoremasi).",
    "triangle": "Uchburchak yuzasi = (asos × balandlik) ÷ 2.",
    "rectangle": "To'rtburchak yuzasi = tomon × tomon.",
    "circle": "Doira yuzasi = π × r × r.",
    "ratio": "Nisbatning ikkala tomonini bir xil songa ko'paytiring yoki bo'ling.",
    "average": "Barcha sonlarni qo'shib, sonlar sonига bo'ling.",
    "negative": "Manfiy sonlar bilan ishlashda son o'qini tasavvur qiling.",
    "speed": "Masofa = tezlik × vaqt.",
    "bank_percent": "Foiz summasi = depozit × foiz ÷ 100.",
    "trig": "Asosiy burchaklar (0°, 30°, 60°, 90°) qiymatlarini yodda tuting.",
    "log": "log_a(b) = c degani a^c = b degani.",
    "arith_prog": "a_n = a1 + (n-1) × d formulasidan foydalaning.",
    "geom_prog": "a_n = a1 × q^(n-1) formulasidan foydalaning.",
}

FORMULAS = {
    "add_sub": "➕ Qo'shish/Ayirish\n\nFormula yo'q, lekin qoida: raqamlarni o'ng tomondan boshlab, ustun-ustun qo'shing yoki ayiring.",
    "mul_div": "✖️ Ko'paytirish/Bo'lish\n\na × b = ko'paytma\na ÷ b = bo'linma (b ≠ 0)",
    "percent": "% Foizlar\n\nSonning n% i = son × n ÷ 100",
    "fraction": "½ Kasrlar\n\nSonning a/b qismi = son × a ÷ b",
    "power": "x² Darajalar\n\naⁿ = a × a × ... (n marta)",
    "sqrt": "√ Kvadrat ildiz\n\n√a = shunday b ki, b × b = a",
    "linear_eq": "🔤 Chiziqli tenglama\n\nax + b = c  →  x = (c − b) ÷ a",
    "quad_eq": "🔤 Kvadrat tenglama\n\nx² + bx + c = 0\nIldizlar yig'indisi = −b, ko'paytmasi = c",
    "triangle": "🔺 Uchburchak\n\nYuza = (asos × balandlik) ÷ 2\nPerimetr = barcha tomonlar yig'indisi",
    "rectangle": "▭ To'rtburchak\n\nYuza = a × b\nPerimetr = 2 × (a + b)",
    "circle": "⭕ Doira\n\nYuza = π × r²\nAylana uzunligi = 2 × π × r",
    "ratio": "⚖️ Nisbat\n\na:b = c:d  →  a × d = b × c",
    "average": "📊 O'rtacha qiymat\n\nO'rtacha = (barcha sonlar yig'indisi) ÷ (sonlar soni)",
    "negative": "➖ Manfiy sonlar\n\n(−a) + (−b) = −(a+b)\n(−a) − b = −(a+b)",
    "speed": "🚗 Tezlik-vaqt-masofa\n\nMasofa = Tezlik × Vaqt\nTezlik = Masofa ÷ Vaqt",
    "bank_percent": "🏦 Foiz o'sishi\n\nFoiz summasi = Depozit × Foiz stavkasi ÷ 100",
    "trig": "📐 Trigonometriya\n\nsin(0°)=0, sin(30°)=0.5, sin(90°)=1\ncos(0°)=1, cos(60°)=0.5, cos(90°)=0",
    "log": "📈 Logarifm\n\nlog_a(b) = c  ⟺  a^c = b",
    "arith_prog": "🔢 Arifmetik progressiya\n\na_n = a₁ + (n−1) × d",
    "geom_prog": "🔢 Geometrik progressiya\n\na_n = a₁ × q^(n−1)",
}

MOTIVATIONS = [
    "✅ To'g'ri! Zo'r ishladingiz!",
    "✅ To'g'ri! Ajoyib!",
    "✅ To'g'ri! Siz iqtidorlisiz!",
    "✅ To'g'ri! Davom eting!",
    "✅ To'g'ri! Zo'r natija!",
    "✅ To'g'ri! Mukammal!",
    "✅ To'g'ri! Shunday davom eting!",
]


def generate_example(topic, grade="medium"):
    if topic == "add_sub":
        if grade == "easy":
            a, b = random.randint(5, 30), random.randint(1, 30)
        elif grade == "hard":
            a, b = random.randint(100, 999), random.randint(50, 500)
        else:
            a, b = random.randint(10, 100), random.randint(1, 100)
        op = random.choice(["+", "-"])
        return f"{a} {op} {b}", (a + b if op == "+" else a - b)

    if topic == "mul_div":
        if grade == "easy":
            a, b = random.randint(2, 10), random.randint(2, 9)
        elif grade == "hard":
            a, b = random.randint(12, 40), random.randint(3, 15)
        else:
            a, b = random.randint(2, 20), random.randint(2, 12)
        op = random.choice(["*", "/"])
        if op == "*":
            return f"{a} * {b}", a * b
        else:
            product = a * b
            return f"{product} / {b}", a

    if topic == "percent":
        base = random.choice([50, 100, 150, 200, 300, 400, 500])
        pct = random.choice([5, 10, 15, 20, 25, 50])
        return f"{base} ning {pct}% i nechaga teng?", base * pct // 100

    if topic == "fraction":
        denom = random.choice([2, 3, 4, 5, 6, 8, 10])
        num = random.randint(1, denom - 1)
        k = random.randint(2, 10)
        total = denom * k
        return f"{total} sonining {num}/{denom} qismi nechaga teng?", num * total // denom

    if topic == "power":
        if grade == "easy":
            a = random.randint(2, 10)
        elif grade == "hard":
            a = random.randint(5, 25)
        else:
            a = random.randint(2, 15)
        p = random.choice([2, 3]) if grade != "easy" else 2
        return f"{a}^{p} = ?", a ** p

    if topic == "sqrt":
        a = random.choice([4, 9, 16, 25, 36, 49, 64, 81, 100, 121, 144])
        return f"√{a} = ?", int(math.sqrt(a))

    if topic == "linear_eq":
        x = random.randint(1, 20)
        a = random.randint(2, 10)
        b = random.randint(1, 30)
        c = a * x + b
        return f"{a}x + {b} = {c}, x = ?", x

    if topic == "quad_eq":
        r1, r2 = random.randint(1, 10), random.randint(1, 10)
        b = -(r1 + r2)
        c = r1 * r2
        sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
        sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
        return f"x² {sign_b} {sign_c} = 0 tenglamaning eng katta ildizi?", max(r1, r2)

    if topic == "triangle":
        base, height = random.randint(4, 20), random.randint(4, 20)
        return f"Asosi {base}, balandligi {height} bo'lgan uchburchak yuzasi?", base * height // 2

    if topic == "rectangle":
        a, b = random.randint(2, 20), random.randint(2, 20)
        return f"Tomonlari {a} va {b} bo'lgan to'rtburchak yuzasi?", a * b

    if topic == "circle":
        r = random.choice([7, 14, 21])
        return f"Radiusi {r} bo'lgan doira yuzasi (π=22/7 deb oling)?", int(22/7 * r * r)

    if topic == "ratio":
        a, b, mult = random.randint(1, 10), random.randint(1, 10), random.randint(2, 10)
        return f"{a}:{b} nisbat {a*mult}:x ga teng bo'lsa, x = ?", b * mult

    if topic == "average":
        nums = [random.randint(1, 50) for _ in range(3)]
        return f"{', '.join(map(str, nums))} sonlarining o'rtacha qiymati?", sum(nums) // len(nums)

    if topic == "negative":
        a, b = random.randint(-50, -1), random.randint(-50, 50)
        op = random.choice(["+", "-"])
        return f"({a}) {op} ({b})", (a + b if op == "+" else a - b)

    if topic == "speed":
        speed = random.randint(40, 120)
        time = random.randint(2, 6)
        return f"Tezligi {speed} km/soat bo'lgan mashina {time} soatda necha km yo'l bosadi?", speed * time

    if topic == "bank_percent":
        deposit = random.choice([1000, 2000, 5000, 10000])
        pct = random.choice([5, 10, 20])
        return f"{deposit} so'm depozitga {pct}% yillik foiz qo'shilsa, 1 yildan keyin qancha foiz summasi qo'shiladi?", deposit * pct // 100

    if topic == "trig":
        vals = {"sin(30)": 0, "cos(60)": 0, "sin(90)": 1, "cos(0)": 1, "sin(0)": 0, "cos(90)": 0}
        options = list(vals.items())
        q, ans = random.choice(options)
        return f"{q}° ning qiymati? (0 yoki 1 dan)", ans

    if topic == "log":
        base = random.choice([2, 3, 10])
        p = random.randint(1, 4)
        value = base ** p
        return f"log{base}({value}) = ?", p

    if topic == "arith_prog":
        a1 = random.randint(1, 10)
        d = random.randint(1, 10)
        n = random.randint(3, 8)
        return f"Arifmetik progressiya: a1={a1}, d={d}. a{n} nechaga teng?", a1 + (n - 1) * d

    if topic == "geom_prog":
        a1 = random.randint(1, 5)
        q = random.randint(2, 3)
        n = random.randint(2, 5)
        return f"Geometrik progressiya: a1={a1}, q={q}. a{n} nechaga teng?", a1 * (q ** (n - 1))


def get_hint_keyboard(topic):
    builder = InlineKeyboardBuilder()
    builder.button(text="💡 Yordam", callback_data=f"hint_{topic}")
    return builder.as_markup()


def generate_options(answer):
    """To'g'ri javob atrofida 3 ta noto'g'ri variant yaratadi."""
    options = {answer}
    attempts = 0
    while len(options) < 4 and attempts < 30:
        attempts += 1
        delta = random.choice([-20, -10, -5, -3, -2, -1, 1, 2, 3, 5, 10, 20])
        candidate = answer + delta
        if candidate not in options:
            options.add(candidate)
    options = list(options)
    random.shuffle(options)
    return options


def get_answer_keyboard(topic, options):
    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(text=str(opt), callback_data=f"ans_{opt}")
    builder.adjust(2, 2)
    builder.button(text="💡 Yordam", callback_data=f"hint_{topic}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_grade_keyboard():
    builder = InlineKeyboardBuilder()
    for key, name in GRADE_LABELS.items():
        builder.button(text=name, callback_data=f"grade_{key}")
    builder.adjust(1)
    return builder.as_markup()


def get_topics_keyboard():
    builder = InlineKeyboardBuilder()
    for key, name in TOPICS.items():
        builder.button(text=name, callback_data=f"topic_{key}")
    builder.adjust(2)
    return builder.as_markup()


def get_formula_keyboard():
    builder = InlineKeyboardBuilder()
    for key, name in TOPICS.items():
        builder.button(text=name, callback_data=f"formula_{key}")
    builder.adjust(2)
    return builder.as_markup()


# ==================== SPEEDTEST STATE ====================
# Foydalanuvchi tezlik testida ekanligini va vaqtini kuzatamiz
speedtest_active = {}  # {user_id: {"count": 0, "task": asyncio_task}}


async def end_speedtest(user_id, chat_id):
    await asyncio.sleep(60)
    if user_id in speedtest_active:
        count = speedtest_active[user_id]["count"]
        del speedtest_active[user_id]
        # Tezlik testidan keyingi "soxta" mavzuni tozalaymiz
        update_user(user_id, current_topic=None, current_answer=None)
        await bot.send_message(
            chat_id,
            f"⏱️ Vaqt tugadi!\n\n"
            f"60 soniyada siz {count} ta misolni to'g'ri yechdingiz! 🎉\n\n"
            f"Yana urinish uchun /speedtest yozing."
        )


# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    get_user(message.from_user.id, message.from_user.first_name)
    await message.answer(
        f"Salom, {message.from_user.first_name}! 🤖\n\n"
        f"Men matematika mashq botiman.\n\n"
        f"Avval sinf darajangizni tanlang (misollarning qiyinligi shunga qarab moslanadi):",
        reply_markup=get_grade_keyboard()
    )


@dp.callback_query(F.data.startswith("grade_"))
async def grade_chosen(callback: types.CallbackQuery):
    grade = callback.data.replace("grade_", "")
    user_id = callback.from_user.id
    get_user(user_id, callback.from_user.first_name)
    update_user(user_id, grade=grade)

    await callback.message.edit_text(
        f"Daraja tanlandi: {GRADE_LABELS[grade]}\n\n"
        f"Endi mavzuni tanlang, yoki pastdagi 📋 Menu tugmasidan barcha buyruqlarni ko'ring:"
    )
    await callback.message.answer("Mavzuni tanlang:", reply_markup=get_topics_keyboard())
    await callback.answer()


@dp.message(Command("level"))
async def level_handler(message: types.Message):
    await message.answer("Sinf darajangizni tanlang:", reply_markup=get_grade_keyboard())


@dp.callback_query(F.data.startswith("topic_"))
async def topic_chosen(callback: types.CallbackQuery):
    topic = callback.data.replace("topic_", "")
    user_id = callback.from_user.id
    user = get_user(user_id, callback.from_user.first_name)

    topic_name = TOPICS[topic]
    example_text, answer = generate_example(topic, user["grade"])
    options = generate_options(answer)

    update_user(user_id, current_topic=topic, current_answer=answer, current_mode="normal")

    await callback.message.edit_text(
        f"Mavzu: {topic_name}\n\n"
        f"📝 Misol: {example_text}\n\n"
        f"To'g'ri javobni tanlang 👇\n\n"
        f"(Menu tugmasidan boshqa buyruqlarni tanlashingiz mumkin)",
        reply_markup=get_answer_keyboard(topic, options)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("hint_"))
async def hint_handler(callback: types.CallbackQuery):
    topic = callback.data.replace("hint_", "")
    hint_text = HINTS.get(topic, "Diqqat bilan hisoblang!")
    await callback.answer(hint_text, show_alert=True)


@dp.callback_query(F.data.startswith("ans_"))
async def answer_button_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id, callback.from_user.first_name)

    chosen = int(callback.data.replace("ans_", ""))
    correct_answer = user["current_answer"]
    topic = user["current_topic"]

    if correct_answer is None or topic not in TOPICS:
        await callback.answer("Bu savol eskirgan. /topics yozing.", show_alert=True)
        return

    if chosen == correct_answer:
        update_user(user_id, correct=user["correct"] + 1)
        update_topic_stat(user_id, topic, correct_delta=1)
        add_weekly_correct(user_id)
        new_streak = update_streak(user_id)
        result_text = f"{random.choice(MOTIVATIONS)} 🔥 Streak: {new_streak} kun"
    else:
        update_user(user_id, wrong=user["wrong"] + 1)
        update_topic_stat(user_id, topic, wrong_delta=1)
        # Savol matnini olib, xatolar jurnaliga yozamiz
        old_question = callback.message.text.split("Misol:")[-1].split("\n")[0].strip() if "Misol:" in callback.message.text else "?"
        log_mistake(user_id, topic, old_question, correct_answer)
        solution_hint = HINTS.get(topic, "")
        result_text = f"❌ Noto'g'ri. To'g'ri javob: {correct_answer}\n📖 Yechim: {solution_hint}"

    # Keyingi mavzuni tanlaymiz (random/challenge rejimda - yangi tasodifiy mavzu)
    mode = user["current_mode"]
    if mode in ("random", "challenge"):
        next_topic = random.choice(list(TOPICS.keys()))
    else:
        next_topic = topic

    effective_grade = "hard" if mode == "challenge" else user["grade"]
    example_text, answer = generate_example(next_topic, effective_grade)
    options = generate_options(answer)
    update_user(user_id, current_topic=next_topic, current_answer=answer)

    topic_label = f" ({TOPICS[next_topic]})" if mode in ("random", "challenge") else ""

    await callback.message.edit_text(
        f"{result_text}\n\n"
        f"📝 Keyingi misol{topic_label}: {example_text}\n\n"
        f"To'g'ri javobni tanlang 👇",
        reply_markup=get_answer_keyboard(next_topic, options)
    )
    await callback.answer()


@dp.message(Command("topics"))
async def topics_handler(message: types.Message):
    await message.answer("Mavzuni tanlang:", reply_markup=get_topics_keyboard())


@dp.message(Command("random"))
async def random_handler(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name)
    update_user(user_id, current_mode="random")

    topic = random.choice(list(TOPICS.keys()))
    example_text, answer = generate_example(topic, user["grade"])
    options = generate_options(answer)
    update_user(user_id, current_topic=topic, current_answer=answer)

    await message.answer(
        f"🎲 Aralash rejim yoqildi! Har safar boshqa mavzudan savol keladi.\n\n"
        f"📝 Misol ({TOPICS[topic]}): {example_text}\n\n"
        f"To'g'ri javobni tanlang 👇",
        reply_markup=get_answer_keyboard(topic, options)
    )


@dp.message(Command("challenge"))
async def challenge_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)
    update_user(user_id, current_mode="challenge")

    topic = random.choice(list(TOPICS.keys()))
    example_text, answer = generate_example(topic, "hard")
    options = generate_options(answer)
    update_user(user_id, current_topic=topic, current_answer=answer)

    await message.answer(
        f"⭐ Challenge rejimi! Faqat eng qiyin darajadagi savollar.\n\n"
        f"📝 Misol ({TOPICS[topic]}): {example_text}\n\n"
        f"To'g'ri javobni tanlang 👇",
        reply_markup=get_answer_keyboard(topic, options)
    )


@dp.message(Command("formula"))
async def formula_handler(message: types.Message):
    await message.answer("📘 Qaysi mavzu formulasini ko'rmoqchisiz?", reply_markup=get_formula_keyboard())


@dp.callback_query(F.data.startswith("formula_"))
async def formula_chosen(callback: types.CallbackQuery):
    topic = callback.data.replace("formula_", "")
    text = FORMULAS.get(topic, "Formula topilmadi.")
    await callback.message.answer(f"📘 {text}")
    await callback.answer()


@dp.message(Command("mistakes"))
async def mistakes_handler(message: types.Message):
    user_id = message.from_user.id
    rows = get_recent_mistakes(user_id, limit=8)

    if not rows:
        await message.answer("📖 Hali xatolaringiz yo'q. Zo'r natija!")
        return

    text = "📖 Oxirgi xatolaringiz:\n\n"
    for topic, question, correct_answer in rows:
        topic_name = TOPICS.get(topic, topic)
        text += f"• {topic_name}: {question} → to'g'ri javob: {correct_answer}\n"
    text += "\nShu mavzularni qayta mashq qilish uchun Menu orqali /topics ni tanlang."

    await message.answer(text)


@dp.message(Command("topweek"))
async def topweek_handler(message: types.Message):
    rows = get_weekly_top(10)

    if not rows:
        await message.answer("Bu hafta hali hech kim mashq qilmagan.")
        return

    text = "📅 Haftalik reyting:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, week_correct) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {name} — {week_correct} ta to'g'ri\n"

    await message.answer(text)


@dp.message(Command("map"))
async def map_handler(message: types.Message):
    user_id = message.from_user.id
    rows = get_topic_stats(user_id)

    if not rows:
        await message.answer("Hali hech qanday mavzuda mashq qilmagansiz. /topics orqali boshlang!")
        return

    def accuracy(row):
        c, w = row[1], row[2]
        return c / (c + w) if (c + w) > 0 else 0

    rows_sorted = sorted(rows, key=accuracy, reverse=True)

    text = "📊 Bilim xaritangiz:\n\n"
    for topic, correct, wrong in rows_sorted:
        total = correct + wrong
        pct = round(correct / total * 100) if total > 0 else 0
        topic_name = TOPICS.get(topic, topic)
        filled = pct // 10
        bar = "🟩" * filled + "⬜" * (10 - filled)
        text += f"{topic_name}\n{bar} {pct}%\n\n"

    await message.answer(text)


@dp.message(Command("stats"))
async def stats_handler(message: types.Message):
    user = get_user(message.from_user.id, message.from_user.first_name)
    correct = user["correct"]
    wrong = user["wrong"]
    total = correct + wrong
    percent = round((correct / total * 100), 1) if total > 0 else 0
    streak = user["streak"] or 0

    await message.answer(
        f"📊 Sizning statistikangiz:\n\n"
        f"✅ To'g'ri: {correct}\n"
        f"❌ Noto'g'ri: {wrong}\n"
        f"🎯 Aniqlik: {percent}%\n"
        f"🔥 Streak: {streak} kun"
    )


@dp.message(Command("top"))
async def top_handler(message: types.Message):
    top_users = get_top_users(10)

    if not top_users:
        await message.answer("Hozircha hech kim mashq qilmagan.")
        return

    text = "🏆 Eng yaxshi 10 ta ishtirokchi:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, correct, wrong) in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {name} — {correct} ta to'g'ri\n"

    await message.answer(text)


@dp.message(Command("speedtest"))
async def speedtest_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)

    if user_id in speedtest_active:
        await message.answer("Siz allaqachon tezlik testida ishtirok etyapsiz! Javob yozishda davom eting.")
        return

    speedtest_active[user_id] = {"count": 0}
    example_text, answer = generate_example("add_sub")
    update_user(user_id, current_topic="speedtest_add_sub", current_answer=answer)

    await message.answer(
        f"⏱️ Tezlik testi boshlandi! 60 soniyada nechta misol yecha olasiz?\n\n"
        f"📝 {example_text} = ?"
    )

    asyncio.create_task(end_speedtest(user_id, message.chat.id))


@dp.message()
async def check_answer_handler(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name)

    is_speedtest = user_id in speedtest_active

    if is_speedtest and user["current_answer"] is not None and message.text.lstrip("-").isdigit():
        correct_answer = user["current_answer"]
        user_answer = int(message.text)

        if user_answer == correct_answer:
            speedtest_active[user_id]["count"] += 1
            await message.answer("✅ To'g'ri!")
        else:
            await message.answer(f"❌ Noto'g'ri. To'g'ri javob: {correct_answer}")

        example_text, answer = generate_example("add_sub")
        update_user(user_id, current_answer=answer)
        await message.answer(f"📝 {example_text} = ?")
    else:
        await message.answer("Mavzu tanlash uchun /topics, tezlik testi uchun /speedtest, statistika uchun /stats, reyting uchun /top yozing.")


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def daily_reminder_loop():
    """Har kuni soat 18:00 da, bugun mashq qilmagan foydalanuvchilarga eslatma yuboradi."""
    while True:
        now = datetime.now()
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        today = date.today().isoformat()
        rows = get_all_active_user_ids()
        for user_id, last_played in rows:
            if last_played != today:
                try:
                    await bot.send_message(
                        user_id,
                        "🔔 Bugun hali mashq qilmadingiz!\n\n"
                        "5 daqiqa vaqt ajratib, bilimingizni mustahkamlang 💪\n"
                        "Menu tugmasidan /topics ni tanlang va boshlang!"
                    )
                except Exception:
                    pass  # foydalanuvchi botni bloklagan bo'lishi mumkin


async def set_bot_commands():
    commands = [
        types.BotCommand(command="start", description="Botni ishga tushirish"),
        types.BotCommand(command="topics", description="📚 Mavzular ro'yxati"),
        types.BotCommand(command="random", description="🎲 Aralash rejim"),
        types.BotCommand(command="challenge", description="⭐ Qiyin savollar"),
        types.BotCommand(command="speedtest", description="⏱️ Tezlik testi"),
        types.BotCommand(command="formula", description="🧮 Formulalar bazasi"),
        types.BotCommand(command="mistakes", description="📖 Xatolaringiz"),
        types.BotCommand(command="map", description="📊 Bilim xaritangiz"),
        types.BotCommand(command="stats", description="📈 Statistikangiz"),
        types.BotCommand(command="top", description="🏆 Umumiy reyting"),
        types.BotCommand(command="topweek", description="📅 Haftalik reyting"),
        types.BotCommand(command="level", description="🎓 Sinf darajasi"),
    ]
    await bot.set_my_commands(commands)


async def main():
    init_db()
    await set_bot_commands()
    await start_web_server()
    asyncio.create_task(daily_reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
