import asyncio
import random
import math
import json
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

# Har bir mavzu uchun tarixda saqlanadigan so'nggi savollar soni
# (shu miqdordagi so'nggi savollar takrorlanmaydi)
HISTORY_LIMIT = 60


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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            code TEXT PRIMARY KEY,
            teacher_id INTEGER,
            name TEXT
        )
    """)
    conn.commit()
    # Eski database'larda ba'zi ustunlar bo'lmasligi mumkin - qo'shib qo'yamiz
    # DIQQAT: bu ro'yxat tartibi get_user() dagi row indekslariga mos kelishi shart -
    # yangi ustun qo'shilganda faqat oxiriga qo'shiladi, mavjudlarining o'rni o'zgarmaydi.
    for col_def in [
        "grade TEXT DEFAULT 'medium'",
        "current_mode TEXT DEFAULT 'normal'",
        "week_start TEXT",
        "week_correct INTEGER DEFAULT 0",
        "group_code TEXT",
        "personal_code TEXT",
        "history TEXT DEFAULT '{}'",
        "current_question TEXT",
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
        row = (user_id, first_name, 0, 0, None, None, None, 0, "medium", "normal", None, 0, None, None, "{}", None)

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
        "group_code": row[12] if len(row) > 12 else None,
        "personal_code": row[13] if len(row) > 13 else None,
        "history": row[14] if len(row) > 14 and row[14] else "{}",
        "current_question": row[15] if len(row) > 15 else None,
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


def generate_code(length=6):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(chars) for _ in range(length))


def create_group(teacher_id, name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for _ in range(10):
        code = generate_code(6)
        cursor.execute("SELECT code FROM groups WHERE code = ?", (code,))
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO groups (code, teacher_id, name) VALUES (?, ?, ?)", (code, teacher_id, name))
            conn.commit()
            conn.close()
            return code
    conn.close()
    return None


def get_group(code):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT code, teacher_id, name FROM groups WHERE code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_teacher_groups(teacher_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT code, name FROM groups WHERE teacher_id = ?", (teacher_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_group_students(code):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT first_name, correct, wrong FROM users WHERE group_code = ? ORDER BY correct DESC", (code,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def ensure_personal_code(user_id):
    user = get_user(user_id)
    if user["personal_code"]:
        return user["personal_code"]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    for _ in range(10):
        code = generate_code(5)
        cursor.execute("SELECT personal_code FROM users WHERE personal_code = ?", (code,))
        if cursor.fetchone() is None:
            conn.close()
            update_user(user_id, personal_code=code)
            return code
    conn.close()
    return None


def get_user_by_personal_code(code):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE personal_code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return get_user(row[0])


# ==================== SAVOLLAR TARIXI (takrorlanmasligi uchun) ====================
def get_topic_history(user_id, topic):
    user = get_user(user_id)
    try:
        hist = json.loads(user["history"] or "{}")
    except (json.JSONDecodeError, TypeError):
        hist = {}
    return set(hist.get(topic, []))


def save_to_history(user_id, topic, question_text):
    user = get_user(user_id)
    try:
        hist = json.loads(user["history"] or "{}")
    except (json.JSONDecodeError, TypeError):
        hist = {}
    lst = hist.get(topic, [])
    lst.append(question_text)
    if len(lst) > HISTORY_LIMIT:
        lst = lst[-HISTORY_LIMIT:]
    hist[topic] = lst
    update_user(user_id, history=json.dumps(hist, ensure_ascii=False))


# ==================== LEARNING PATH ====================
PATH_ORDER = [
    "add_sub", "negative", "mul_div", "fraction", "percent",
    "power", "sqrt", "ratio", "average",
    "linear_eq", "quad_eq",
    "triangle", "rectangle", "circle",
    "speed", "bank_percent",
    "trig", "log", "arith_prog", "geom_prog",
]


def get_path_status(user_id):
    rows = {t: (c, w) for t, c, w in get_topic_stats(user_id)}
    statuses = []
    current_topic = None
    unlocked_found = False

    for t in PATH_ORDER:
        c, w = rows.get(t, (0, 0))
        total = c + w
        acc = c / total if total > 0 else 0
        mastered = total >= 5 and acc >= 0.7

        if not unlocked_found:
            if mastered:
                status = "done"
            else:
                status = "current"
                current_topic = t
                unlocked_found = True
        else:
            status = "locked"
        statuses.append((t, status))

    if current_topic is None:
        current_topic = PATH_ORDER[-1]  # hammasi o'zlashtirilgan

    return statuses, current_topic


async def start_topic_question(user_id, first_name, topic, message=None, callback_message=None):
    """Berilgan mavzudan savol boshlaydi - matn xabar yoki callback orqali chaqirilishi mumkin."""
    user = get_user(user_id, first_name)
    example_text, answer = generate_example(user_id, topic, user["grade"])
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))
    update_user(user_id, current_topic=topic, current_answer=answer, current_mode="normal", current_question=example_text)

    text = (
        f"Mavzu: {TOPICS[topic]}\n\n"
        f"📝 Misol: {example_text}\n\n"
        f"To'g'ri javobni tanlang 👇"
    )
    markup = get_answer_keyboard(topic, options)

    if callback_message:
        await callback_message.edit_text(text, reply_markup=markup)
    elif message:
        await message.answer(text, reply_markup=markup)


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

# Har bir sinf darajasida o'quvchiga qaysi mavzular ko'rsatilishi kerak.
# Daraja o'sishi bilan oldingi darajaning mavzulari ham saqlanib qoladi
# (masalan 8-9 sinf o'quvchisi 5-7 sinf mavzularini ham ko'radi).
GRADE_TOPICS = {
    "easy": [
        "add_sub", "negative", "mul_div", "fraction", "percent",
        "power", "sqrt", "ratio", "average", "linear_eq",
        "triangle", "rectangle", "circle", "speed",
    ],
}
GRADE_TOPICS["medium"] = GRADE_TOPICS["easy"] + [
    "quad_eq", "bank_percent", "trig", "arith_prog", "geom_prog",
]
GRADE_TOPICS["hard"] = GRADE_TOPICS["medium"] + ["log"]


def topics_for_grade(grade):
    return GRADE_TOPICS.get(grade, list(TOPICS.keys()))

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
    "average": "Barcha sonlarni qo'shib, sonlar soniga bo'ling.",
    "negative": "Manfiy sonlar bilan ishlashda son o'qini tasavvur qiling.",
    "speed": "Masofa = tezlik × vaqt.",
    "bank_percent": "Foiz summasi = depozit × foiz ÷ 100.",
    "trig": "Asosiy burchaklar (0°, 30°, 45°, 60°, 90°) qiymatlarini yodda tuting.",
    "log": "log_a(b) = c degani a^c = b degani.",
    "arith_prog": "a_n = a1 + (n-1) × d formulasidan foydalaning.",
    "geom_prog": "a_n = a1 × q^(n-1) formulasidan foydalaning.",
}

# Qaysi mavzularda manfiy javob/variant mantiqan to'g'ri kelishi mumkin
NEGATIVE_ALLOWED_TOPICS = {"add_sub", "negative", "linear_eq"}


def topic_allows_negative(topic):
    return topic in NEGATIVE_ALLOWED_TOPICS


FORMULAS = {
    "add_sub": (
        "➕ QO'SHISH VA AYIRISH\n\n"
        "• a + b = qo'shindi (summa)\n"
        "• a − b = ayirma\n"
        "• a + b = b + a (o'rin almashtirish qonuni)\n"
        "• (a + b) + c = a + (b + c) (guruhlash qonuni)\n"
        "• a − b ≠ b − a (ayirishda o'rin almashtirib bo'lmaydi)\n"
        "• a + 0 = a, a − 0 = a\n"
        "• a − a = 0\n\n"
        "📌 Qoida: ko'p xonali sonlarni qo'shish/ayirishda raqamlarni o'ngdan chapga, "
        "xona-xona (birlik, o'nlik, yuzlik...) tekislab yozing."
    ),
    "mul_div": (
        "✖️ KO'PAYTIRISH VA BO'LISH\n\n"
        "• a × b = ko'paytma\n"
        "• a ÷ b = bo'linma (b ≠ 0)\n"
        "• a × b = b × a (o'rin almashtirish qonuni)\n"
        "• (a × b) × c = a × (b × c) (guruhlash qonuni)\n"
        "• a × (b + c) = a×b + a×c (taqsimot qonuni)\n"
        "• a × 1 = a,  a × 0 = 0\n"
        "• a ÷ 1 = a,  a ÷ a = 1 (a ≠ 0)\n"
        "• Bo'linma tekshiruvi: a = b × natija + qoldiq"
    ),
    "percent": (
        "% FOIZLAR\n\n"
        "• Sonning n% i = son × n ÷ 100\n"
        "• Foizni songa aylantirish: n% = n ÷ 100\n"
        "• Narx n% ga oshsa: yangi narx = eski narx + eski narx×n÷100 = eski narx×(1 + n/100)\n"
        "• Narx n% ga tushsa: yangi narx = eski narx − eski narx×n÷100 = eski narx×(1 − n/100)\n"
        "• A soni B sonining necha foizini tashkil qiladi: (A ÷ B) × 100%\n"
        "• Butun son = qism ÷ (foiz ÷ 100)"
    ),
    "fraction": (
        "½ KASRLAR\n\n"
        "• Sonning a/b qismi = son × a ÷ b\n"
        "• Kasrlarni qo'shish (bir xil maxrajda): a/c + b/c = (a+b)/c\n"
        "• Kasrlarni ko'paytirish: (a/b) × (c/d) = (a×c)/(b×d)\n"
        "• Kasrlarni bo'lish: (a/b) ÷ (c/d) = (a/b) × (d/c)\n"
        "• Aralash sonni kasrga aylantirish: a b/c = (a×c+b)/c\n"
        "• Qisqartirish: a/b = (a÷k)/(b÷k), k — umumiy bo'luvchi"
    ),
    "power": (
        "x² DARAJALAR\n\n"
        "• aⁿ = a × a × ... × a (n marta)\n"
        "• a¹ = a,  a⁰ = 1 (a ≠ 0)\n"
        "• aᵐ × aⁿ = aᵐ⁺ⁿ\n"
        "• aᵐ ÷ aⁿ = aᵐ⁻ⁿ\n"
        "• (aᵐ)ⁿ = aᵐ×ⁿ\n"
        "• (a×b)ⁿ = aⁿ × bⁿ\n"
        "• (a+b)² = a² + 2ab + b²\n"
        "• (a−b)² = a² − 2ab + b²\n"
        "• a² − b² = (a−b)(a+b)"
    ),
    "sqrt": (
        "√ KVADRAT ILDIZ\n\n"
        "• √a = shunday b ≥ 0 ki, b × b = a\n"
        "• √(a×b) = √a × √b\n"
        "• √(a/b) = √a ÷ √b (b ≠ 0)\n"
        "• (√a)² = a (a ≥ 0)\n"
        "• √a² = |a|\n"
        "• Muhim kvadratlar: 1,4,9,16,25,36,49,64,81,100,121,144,169,196,225,...\n"
        "  ularning ildizlari mos ravishda: 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,..."
    ),
    "linear_eq": (
        "🔤 CHIZIQLI TENGLAMA\n\n"
        "• x + b = c  →  x = c − b\n"
        "• ax = c  →  x = c ÷ a (a ≠ 0)\n"
        "• ax + b = c  →  x = (c − b) ÷ a\n"
        "• ax + b = cx + d  →  x = (d − b) ÷ (a − c)\n"
        "• Tenglamaning ikkala tomoniga bir xil son qo'shish/ayirish yechimni o'zgartirmaydi\n"
        "• Tenglamaning ikkala tomonini bir xil (nolmas) songa ko'paytirish/bo'lish yechimni o'zgartirmaydi"
    ),
    "quad_eq": (
        "🔤 KVADRAT TENGLAMA\n\n"
        "• Umumiy ko'rinish: ax² + bx + c = 0 (a ≠ 0)\n"
        "• Diskriminant: D = b² − 4ac\n"
        "• Ildizlar: x = (−b ± √D) ÷ (2a)\n"
        "• D > 0 → 2 ta ildiz, D = 0 → 1 ta ildiz, D < 0 → haqiqiy ildiz yo'q\n"
        "• Vieta teoremasi (x² + px + q = 0 uchun):\n"
        "  x1 + x2 = −p\n"
        "  x1 × x2 = q"
    ),
    "triangle": (
        "🔺 UCHBURCHAK\n\n"
        "• Yuza = (asos × balandlik) ÷ 2\n"
        "• Perimetr = a + b + c (barcha tomonlar yig'indisi)\n"
        "• Burchaklar yig'indisi = 180°\n"
        "• Teng yonli uchburchakda 2 tomon va 2 burchak teng\n"
        "• Teng tomonli uchburchakda barcha tomon va burchaklar teng (har biri 60°)\n"
        "• To'g'ri burchakli uchburchakda: Pifagor teoremasi — a² + b² = c² (c — gipotenuza)"
    ),
    "rectangle": (
        "▭ TO'RTBURCHAK (TO'G'RI TO'RTBURCHAK)\n\n"
        "• Yuza = a × b\n"
        "• Perimetr = 2 × (a + b)\n"
        "• Diagonal (Pifagor bo'yicha): d = √(a² + b²)\n"
        "• Kvadrat uchun (a = b): Yuza = a², Perimetr = 4a"
    ),
    "circle": (
        "⭕ DOIRA VA AYLANA\n\n"
        "• Doira yuzasi = π × r²\n"
        "• Aylana uzunligi (perimetri) = 2 × π × r = π × d\n"
        "• Diametr d = 2 × r\n"
        "• π ≈ 3.14 yoki 22/7 (taqribiy)\n"
        "• Radius yuza orqali: r = √(Yuza ÷ π)"
    ),
    "ratio": (
        "⚖️ NISBAT VA PROPORTSIYA\n\n"
        "• a : b = c : d  →  a × d = b × c (proportsiya asosiy xossasi)\n"
        "• Nisbatning ikkala tomonini bir xil songa ko'paytirish/bo'lish nisbatni o'zgartirmaydi\n"
        "• Sonni a:b nisbatda ulashish: kichik qism = son × a ÷ (a+b), katta qism = son × b ÷ (a+b)\n"
        "• To'g'ri proportsionallik: y = k × x\n"
        "• Teskari proportsionallik: y = k ÷ x"
    ),
    "average": (
        "📊 O'RTACHA QIYMAT\n\n"
        "• O'rtacha (arifmetik) = (barcha sonlar yig'indisi) ÷ (sonlar soni)\n"
        "• Yig'indi = o'rtacha × sonlar soni\n"
        "• Agar bitta son ma'lum bo'lmasa: noma'lum son = (o'rtacha × soni) − (ma'lum sonlar yig'indisi)"
    ),
    "negative": (
        "➖ MANFIY SONLAR\n\n"
        "• (−a) + (−b) = −(a+b)\n"
        "• (−a) − b = −(a+b)\n"
        "• a − (−b) = a + b\n"
        "• (−a) + b = b − a\n"
        "• (−a) × (−b) = a × b (manfiy × manfiy = musbat)\n"
        "• (−a) × b = −(a×b) (manfiy × musbat = manfiy)\n"
        "• (−a) ÷ (−b) = a ÷ b\n"
        "• (−a) ÷ b = −(a÷b)"
    ),
    "speed": (
        "🚗 TEZLIK-VAQT-MASOFA\n\n"
        "• Masofa (S) = Tezlik (V) × Vaqt (T)\n"
        "• Tezlik (V) = Masofa (S) ÷ Vaqt (T)\n"
        "• Vaqt (T) = Masofa (S) ÷ Tezlik (V)\n"
        "• Qarama-qarshi harakatda: yaqinlashish tezligi = V1 + V2\n"
        "• Bir yo'nalishda quvib o'tishda: V(farq) = V1 − V2"
    ),
    "bank_percent": (
        "🏦 FOIZ O'SISHI (BANK DEPOZITI)\n\n"
        "• Foiz summasi = Depozit × Foiz stavkasi ÷ 100\n"
        "• 1 yildan keyingi umumiy summa = Depozit + Foiz summasi = Depozit × (1 + stavka/100)\n"
        "• Oddiy foiz (n yil): Summa = Depozit × (1 + n×stavka/100)\n"
        "• Murakkab foiz (n yil): Summa = Depozit × (1 + stavka/100)ⁿ"
    ),
    "trig": (
        "📐 TRIGONOMETRIYA\n\n"
        "Asosiy burchaklar jadvali:\n"
        "• sin: 0°→0, 30°→0.5, 45°→√2/2, 60°→√3/2, 90°→1\n"
        "• cos: 0°→1, 30°→√3/2, 45°→√2/2, 60°→0.5, 90°→0\n"
        "• tan: 0°→0, 45°→1, 90°→aniqlanmagan\n\n"
        "• sin²α + cos²α = 1 (asosiy trigonometrik ayniyat)\n"
        "• tan α = sin α ÷ cos α"
    ),
    "log": (
        "📈 LOGARIFM\n\n"
        "• log_a(b) = c  ⟺  a^c = b  (a > 0, a ≠ 1, b > 0)\n"
        "• log_a(1) = 0\n"
        "• log_a(a) = 1\n"
        "• log_a(x×y) = log_a(x) + log_a(y)\n"
        "• log_a(x÷y) = log_a(x) − log_a(y)\n"
        "• log_a(xⁿ) = n × log_a(x)"
    ),
    "arith_prog": (
        "🔢 ARIFMETIK PROGRESSIYA\n\n"
        "• n-had: a_n = a1 + (n−1) × d\n"
        "• Ayirma: d = a_(n+1) − a_n\n"
        "• Yig'indi: S_n = n × (2×a1 + (n−1)×d) ÷ 2\n"
        "• Yig'indi (boshqa ko'rinish): S_n = n × (a1 + a_n) ÷ 2"
    ),
    "geom_prog": (
        "🔢 GEOMETRIK PROGRESSIYA\n\n"
        "• n-had: a_n = a1 × q^(n−1)\n"
        "• Maxraj: q = a_(n+1) ÷ a_n\n"
        "• Yig'indi (q ≠ 1): S_n = a1 × (qⁿ − 1) ÷ (q − 1)\n"
        "• Cheksiz kamayuvchi progressiya yig'indisi (|q| < 1): S = a1 ÷ (1 − q)"
    ),
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

NAMES_POOL = ["Ahmad", "Vali", "Aziza", "Dilnoza", "Sardor", "Malika", "Jasur", "Nodira", "Bekzod", "Kamola"]
ITEMS_POOL = ["olma", "qalam", "daftar", "konfet", "kitob", "yong'oq", "shar", "gul"]


# ==================== MISOL GENERATORLARI ====================
# Har bir mavzu uchun bir nechta MANTIQIY JIHATDAN TURLICHA variant (style) mavjud.
# Har chaqirilganda tasodifiy variant tanlanadi, shuning uchun savol shakli ham,
# sonlar ham o'zgarib turadi.

def _rng(grade, easy, medium, hard):
    return {"easy": easy, "medium": medium, "hard": hard}[grade]


# ---------- add_sub ----------
def ex_add_sub_plain(grade):
    lo, hi = _rng(grade, (5, 40), (50, 500), (200, 9999))
    op = random.choice(["+", "-"])
    if op == "-":
        # add_sub mavzusi manfiy sonlar bilan shug'ullanmaydi (buning uchun
        # alohida "negative" mavzusi bor) - shuning uchun a >= b bo'lishi shart
        a, b = sorted([random.randint(lo, hi), random.randint(lo, hi)], reverse=True)
        return f"{a} - {b}", a - b
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"{a} + {b}", a + b


def ex_add_sub_shop(grade):
    lo, hi = _rng(grade, (15, 60), (100, 800), (500, 5000))
    a = random.randint(lo, hi)
    b = random.randint(1, a - 1)
    item = random.choice(ITEMS_POOL)
    return f"Do'konda {a} ta {item} bor edi. {b} ta sotib olishdi. Necha ta {item} qoldi?", a - b


def ex_add_sub_two_people(grade):
    lo, hi = _rng(grade, (5, 40), (30, 300), (200, 2000))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    n1, n2 = random.sample(NAMES_POOL, 2)
    item = random.choice(ITEMS_POOL)
    return f"{n1} {a} ta, {n2} {b} ta {item} sotib oldi. Ular jami nechta {item} sotib olishdi?", a + b


def ex_add_sub_three_terms(grade):
    # Har bir qadamda natija manfiy bo'lib qolmasligi uchun ayirish
    # miqdorlarini joriy yig'indidan oshmaydigan qilib tanlaymiz.
    lo, hi = _rng(grade, (10, 50), (100, 600), (300, 999))
    a = random.randint(lo, hi)

    op1 = random.choice(["+", "-"])
    if op1 == "-":
        b = random.randint(1, a)
    else:
        b = random.randint(lo, hi)
    val1 = a + b if op1 == "+" else a - b

    op2 = random.choice(["+", "-"]) if val1 > 0 else "+"
    if op2 == "-":
        c = random.randint(1, val1)
    else:
        c = random.randint(lo, hi)
    val2 = val1 + c if op2 == "+" else val1 - c

    return f"{a} {op1} {b} {op2} {c}", val2


def ex_add_sub_bus(grade):
    lo, hi = _rng(grade, (10, 40), (20, 90), (50, 400))
    start = random.randint(lo, hi)
    left = random.randint(1, start)
    came = random.randint(1, 20)
    return f"Avtobusda {start} ta yo'lovchi bor edi. Bekatda {left} kishi tushdi, {came} kishi chiqdi. Endi avtobusda nechta yo'lovchi bor?", start - left + came


GEN_ADD_SUB = [ex_add_sub_plain, ex_add_sub_shop, ex_add_sub_two_people, ex_add_sub_three_terms, ex_add_sub_bus]


# ---------- mul_div ----------
def ex_mul_div_plain(grade):
    lo, hi = _rng(grade, (2, 10), (5, 25), (10, 50))
    a, b = random.randint(lo, hi), random.randint(2, 12)
    op = random.choice(["*", "/"])
    if op == "*":
        return f"{a} * {b}", a * b
    return f"{a*b} / {b}", a


def ex_mul_div_boxes(grade):
    lo, hi = _rng(grade, (2, 9), (5, 20), (10, 40))
    a, b = random.randint(lo, hi), random.randint(2, 12)
    return f"Har bir qutida {a} ta olma bor. {b} ta quti bo'lsa, jami nechta olma bo'ladi?", a * b


def ex_mul_div_share(grade):
    lo, hi = _rng(grade, (2, 9), (5, 15), (10, 30))
    a, b = random.randint(lo, hi), random.randint(2, 9)
    total = a * b
    item = random.choice(ITEMS_POOL)
    return f"{total} ta {item}ni {b} ta bolaga teng bo'lib berildi. Har biriga nechtadan tegadi?", a


def ex_mul_div_price(grade):
    lo, hi = _rng(grade, (2, 8), (3, 15), (5, 30))
    price = random.randint(lo, hi) * 1000
    count = random.randint(2, 12)
    return f"1 ta kitob narxi {price} so'm. {count} ta kitob uchun jami qancha to'lash kerak?", price * count


def ex_mul_div_combo(grade):
    c = random.randint(2, 9)
    lo, hi = _rng(grade, (2, 6), (2, 10), (3, 15))
    a = random.randint(lo, hi) * c
    b = random.randint(2, 12)
    return f"({a} × {b}) ÷ {c}", (a * b) // c


GEN_MUL_DIV = [ex_mul_div_plain, ex_mul_div_boxes, ex_mul_div_share, ex_mul_div_price, ex_mul_div_combo]


# ---------- percent ----------
# MUHIM: pct * base har doim 100 ga qoldiqsiz bo'linishi kerak (aks holda
# javob butun son chiqmay, xato hisoblanadi). Buning uchun base doim 20 ga
# karrali qilib tanlanadi - bu {5,10,15,20,25,50,75} foizlarning barchasi
# uchun aniq (butun) natija kafolatlaydi.
def _percent_base(k_lo, k_hi):
    return 20 * random.randint(k_lo, k_hi)


def ex_percent_discount(grade):
    base = _percent_base(*_rng(grade, (1, 7), (10, 50), (50, 250)))
    pct = random.choice([5, 10, 15, 20, 25, 50])
    return f"{base} so'mlik narsaga {pct}% chegirma qilindi. Chegirma summasi qancha so'm?", base * pct // 100


def ex_percent_increase(grade):
    base = _percent_base(*_rng(grade, (2, 10), (15, 40), (50, 250)))
    pct = random.choice([5, 10, 15, 20, 25])
    return f"Mahsulot narxi {base} so'm edi, keyin {pct}% oshdi. Yangi narx qancha so'm?", base + base * pct // 100


def ex_percent_decrease(grade):
    base = _percent_base(*_rng(grade, (2, 8), (10, 40), (50, 200)))
    pct = random.choice([5, 10, 20, 25])
    return f"Mahsulot narxi {base} so'm edi, keyin {pct}% arzonlashdi. Yangi narx qancha so'm?", base - base * pct // 100


def ex_percent_direct(grade):
    base = _percent_base(*_rng(grade, (1, 7), (8, 25), (30, 100)))
    pct = random.choice([5, 10, 15, 20, 25, 50, 75])
    return f"{base} ning {pct}% i nechaga teng?", base * pct // 100


def ex_percent_of_students(grade):
    total = _percent_base(*_rng(grade, (1, 5), (3, 8), (6, 15)))
    pct = random.choice([10, 20, 25, 50])
    return f"Sinfda {total} ta o'quvchi bor. Ularning {pct}% i qiz bo'lsa, nechta qiz bor?", total * pct // 100


GEN_PERCENT = [ex_percent_discount, ex_percent_increase, ex_percent_decrease, ex_percent_direct, ex_percent_of_students]


# ---------- fraction ----------
def ex_fraction_simple(grade):
    denom = random.choice(_rng(grade, [2, 3, 4], [2, 3, 4, 5, 6], [4, 5, 6, 8, 10, 12]))
    num = random.randint(1, denom - 1)
    k = random.randint(2, 10)
    total = denom * k
    return f"{total} sonining {num}/{denom} qismi nechaga teng?", num * total // denom


def ex_fraction_nested(grade):
    denom1 = random.choice([2, 3, 4])
    num1 = random.randint(1, denom1 - 1)
    denom2 = random.choice([2, 3])
    num2 = random.randint(1, denom2 - 1)
    k = random.randint(2, 8)
    total = denom1 * denom2 * k
    mid = num1 * total // denom1
    final = num2 * mid // denom2
    return f"{total} sonining {num1}/{denom1} qismining yana {num2}/{denom2} qismi nechaga teng?", final


def ex_fraction_remaining(grade):
    denom = random.choice([3, 4, 5])
    num = random.randint(1, denom - 1)
    k = random.randint(2, 10)
    total = denom * k
    used = num * total // denom
    item = random.choice(ITEMS_POOL)
    return f"{total} ta {item}ning {num}/{denom} qismi ishlatildi. Nechta {item} ishlatilmay qoldi?", total - used


def ex_fraction_money(grade):
    denom = random.choice([2, 4, 5, 10])
    num = random.randint(1, denom - 1)
    k = random.randint(2, 20)
    total = denom * k * 1000
    return f"{random.choice(NAMES_POOL)}da {total} so'm bor edi. U pulining {num}/{denom} qismini sarfladi. Necha so'm sarflandi?", num * total // denom


GEN_FRACTION = [ex_fraction_simple, ex_fraction_nested, ex_fraction_remaining, ex_fraction_money]


# ---------- power ----------
def ex_power_square(grade):
    lo, hi = _rng(grade, (2, 10), (2, 20), (2, 30))
    a = random.randint(lo, hi)
    return f"{a}² = ?", a * a


def ex_power_cube(grade):
    lo, hi = _rng(grade, (2, 6), (2, 9), (2, 12))
    a = random.randint(lo, hi)
    return f"{a}³ = ?", a ** 3


def ex_power_sum_then_power(grade):
    a, b = random.randint(2, 9), random.randint(1, 9)
    p = random.choice([2, 3]) if grade != "easy" else 2
    return f"({a}+{b})^{p} = ?", (a + b) ** p


def ex_power_diff_then_square(grade):
    a = random.randint(5, 20)
    b = random.randint(1, a - 1)
    return f"({a}−{b})² = ?", (a - b) ** 2


GEN_POWER = [ex_power_square, ex_power_cube, ex_power_sum_then_power, ex_power_diff_then_square]


# ---------- sqrt ----------
PERFECT_SQUARES_EASY = [4, 9, 16, 25, 36, 49, 64, 81, 100]
PERFECT_SQUARES_ALL = [n * n for n in range(2, 26)]


def ex_sqrt_direct(grade):
    pool = _rng(grade, PERFECT_SQUARES_EASY, PERFECT_SQUARES_ALL[:20], PERFECT_SQUARES_ALL)
    a = random.choice(pool)
    return f"√{a} = ?", int(math.sqrt(a))


def ex_sqrt_from_square(grade):
    lo, hi = _rng(grade, (5, 12), (10, 20), (15, 30))
    base = random.randint(lo, hi)
    return f"√{base*base} = ?", base


def ex_sqrt_area_to_side(grade):
    lo, hi = _rng(grade, (3, 10), (8, 18), (12, 25))
    side = random.randint(lo, hi)
    area = side * side
    return f"Yuzasi {area} bo'lgan kvadratning tomoni nechaga teng?", side


def ex_sqrt_product(grade):
    # √(a×b) = √a × √b xossasidan foydalanamiz
    lo, hi = _rng(grade, (2, 6), (2, 10), (2, 15))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"√{a*a} × √{b*b} nechaga teng?", a * b


GEN_SQRT = [ex_sqrt_direct, ex_sqrt_from_square, ex_sqrt_area_to_side, ex_sqrt_product]


# ---------- linear_eq ----------
def ex_linear_simple(grade):
    x = random.randint(1, 30)
    b = random.randint(1, 30)
    c = x + b
    return f"x + {b} = {c}, x = ?", x


def ex_linear_ax_b(grade):
    x = random.randint(1, 25)
    a = random.randint(2, 12)
    b = random.randint(1, 40)
    c = a * x + b
    return f"{a}x + {b} = {c}, x = ?", x


def ex_linear_both_sides(grade):
    x = random.randint(1, 15)
    a = random.randint(2, 8)
    c = random.randint(1, 8)
    while c == a:
        c = random.randint(1, 8)
    b = random.randint(1, 20)
    d = a * x + b - c * x
    sign_b = f"+ {b}" if b >= 0 else f"- {abs(b)}"
    sign_d = f"+ {d}" if d >= 0 else f"- {abs(d)}"
    return f"{a}x {sign_b} = {c}x {sign_d}, x = ?", x


def ex_linear_minus(grade):
    x = random.randint(1, 25)
    a = random.randint(2, 10)
    b = random.randint(1, 30)
    c = a * x - b
    return f"{a}x − {b} = {c}, x = ?", x


GEN_LINEAR_EQ = [ex_linear_simple, ex_linear_ax_b, ex_linear_both_sides, ex_linear_minus]


# ---------- quad_eq ----------
def ex_quad_pure(grade):
    lo, hi = _rng(grade, (2, 12), (2, 16), (2, 20))
    x = random.randint(lo, hi)
    return f"x² = {x*x} (x > 0), x = ?", x


def _quad_text(r1, r2):
    b, c = -(r1 + r2), r1 * r2
    sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
    sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
    return f"x² {sign_b} {sign_c} = 0", b, c


def ex_quad_sum(grade):
    r1, r2 = random.randint(1, 12), random.randint(1, 12)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlarining yig'indisi?", r1 + r2


def ex_quad_product(grade):
    r1, r2 = random.randint(1, 10), random.randint(1, 10)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlarining ko'paytmasi?", r1 * r2


def ex_quad_largest(grade):
    r1, r2 = random.randint(1, 12), random.randint(1, 12)
    while r1 == r2:
        r2 = random.randint(1, 12)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglamaning eng katta ildizi?", max(r1, r2)


GEN_QUAD_EQ = [ex_quad_pure, ex_quad_sum, ex_quad_product, ex_quad_largest]


# ---------- triangle ----------
def ex_triangle_area(grade):
    lo, hi = _rng(grade, (4, 12), (6, 20), (10, 40))
    base, height = random.randint(lo, hi), random.randint(lo, hi)
    if (base * height) % 2 != 0:
        height += 1
    return f"Asosi {base}, balandligi {height} bo'lgan uchburchak yuzasi?", base * height // 2


def ex_triangle_perimeter(grade):
    lo, hi = _rng(grade, (5, 15), (8, 25), (15, 50))
    a, b, c = random.randint(lo, hi), random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a}, {b}, {c} bo'lgan uchburchakning perimetri?", a + b + c


def ex_triangle_missing_side(grade):
    lo, hi = _rng(grade, (5, 15), (8, 25), (15, 40))
    a, b, c = random.randint(lo, hi), random.randint(lo, hi), random.randint(lo, hi)
    p = a + b + c
    return f"Uchburchak perimetri {p}. Ikki tomoni {a} va {b} bo'lsa, uchinchi tomoni nechaga teng?", c


GEN_TRIANGLE = [ex_triangle_area, ex_triangle_perimeter, ex_triangle_missing_side]


# ---------- rectangle ----------
def ex_rect_area(grade):
    lo, hi = _rng(grade, (2, 15), (5, 25), (10, 50))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a} va {b} bo'lgan to'rtburchak yuzasi?", a * b


def ex_rect_perimeter(grade):
    lo, hi = _rng(grade, (2, 15), (5, 25), (10, 50))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a} va {b} bo'lgan to'rtburchak perimetri?", 2 * (a + b)


def ex_rect_missing_side(grade):
    lo, hi = _rng(grade, (3, 12), (5, 20), (8, 30))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    area = a * b
    return f"Yuzasi {area}, bir tomoni {a} bo'lgan to'rtburchakning ikkinchi tomonini toping.", b


GEN_RECTANGLE = [ex_rect_area, ex_rect_perimeter, ex_rect_missing_side]


# ---------- circle ----------
# π=22/7 taqribiy qiymatidan foydalanganda natija ANIQ butun son chiqishi uchun
# radius doim 7 ga karrali bo'lishi shart. Yetarli xilma-xillik uchun keng
# diapazondagi 7 karrali sonlardan foydalanamiz.
def _circle_radius(grade):
    k = random.randint(*_rng(grade, (1, 10), (1, 20), (1, 30)))
    return 7 * k


def ex_circle_area(grade):
    r = _circle_radius(grade)
    return f"Radiusi {r} bo'lgan doira yuzasi (π=22/7 deb oling)?", int(22 * r * r / 7)


def ex_circle_circumference(grade):
    r = _circle_radius(grade)
    return f"Radiusi {r} bo'lgan doiraning aylana uzunligi (π=22/7 deb oling)?", int(2 * 22 * r / 7)


def ex_circle_radius_from_diameter(grade):
    r = _circle_radius(grade)
    d = r * 2
    return f"Diametri {d} bo'lgan doiraning radiusi nechaga teng?", r


def ex_circle_diameter_from_radius(grade):
    r = _circle_radius(grade)
    return f"Radiusi {r} bo'lgan doiraning diametri nechaga teng?", r * 2


def ex_circle_radius_from_area(grade):
    r = _circle_radius(grade)
    area = int(22 * r * r / 7)
    return f"Yuzasi {area} bo'lgan doiraning radiusi nechaga teng? (π=22/7 deb oling)", r


GEN_CIRCLE = [
    ex_circle_area, ex_circle_circumference, ex_circle_radius_from_diameter,
    ex_circle_diameter_from_radius, ex_circle_radius_from_area,
]


# ---------- ratio ----------
def ex_ratio_proportion(grade):
    a, b, mult = random.randint(1, 10), random.randint(1, 10), random.randint(2, 10)
    return f"{a}:{b} nisbat {a*mult}:x ga teng bo'lsa, x = ?", b * mult


def ex_ratio_split(grade):
    p1, p2 = random.randint(1, 8), random.randint(1, 8)
    while p1 == p2:
        p2 = random.randint(1, 8)
    k = random.randint(2, 12)
    total = (p1 + p2) * k
    return f"{total} sonini {p1}:{p2} nisbatda ulashganda kichik qism nechaga teng?", min(p1, p2) * k


def ex_ratio_students(grade):
    p1, p2 = random.randint(1, 6), random.randint(1, 6)
    while p1 == p2:
        p2 = random.randint(1, 6)
    k = random.randint(2, 10)
    boys, girls = p1 * k, p2 * k
    return f"Sinfda o'g'il va qizlar soni nisbati {p1}:{p2}. O'g'il bolalar {boys} ta bo'lsa, qizlar nechta?", girls


GEN_RATIO = [ex_ratio_proportion, ex_ratio_split, ex_ratio_students]


# ---------- average ----------
def ex_average_direct(grade):
    lo, hi = _rng(grade, (1, 20), (1, 50), (1, 100))
    nums = [random.randint(lo, hi) for _ in range(3)]
    while sum(nums) % 3 != 0:
        nums = [random.randint(lo, hi) for _ in range(3)]
    return f"{', '.join(map(str, nums))} sonlarining o'rtacha qiymati?", sum(nums) // 3


def ex_average_sum_from_avg(grade):
    avg = random.randint(10, 60)
    n = random.choice([3, 4, 5, 6])
    return f"{n} ta sonning o'rtacha qiymati {avg}. Bu sonlarning yig'indisi nechaga teng?", avg * n


def ex_average_score(grade):
    n = random.choice([3, 4, 5])
    lo, hi = _rng(grade, (2, 5), (50, 90), (60, 100))
    scores = [random.randint(lo, hi) for _ in range(n)]
    while sum(scores) % n != 0:
        scores = [random.randint(lo, hi) for _ in range(n)]
    return f"O'quvchi {n} ta nazoratdan {', '.join(map(str, scores))} ball oldi. O'rtacha bahosi nechaga teng?", sum(scores) // n


GEN_AVERAGE = [ex_average_direct, ex_average_sum_from_avg, ex_average_score]


# ---------- negative ----------
def ex_negative_add(grade):
    lo, hi = _rng(grade, (-20, -1), (-50, -1), (-99, -1))
    a, b = random.randint(lo, hi), random.randint(1, abs(lo))
    op = random.choice(["+", "-"])
    return f"({a}) {op} {b}", (a + b if op == "+" else a - b)


def ex_negative_mul(grade):
    lo, hi = _rng(grade, (-12, -2), (-15, -2), (-20, -2))
    a, b = random.randint(lo, hi), random.randint(2, 12)
    if random.random() < 0.5:
        b = -b
    return f"({a}) × ({b})" if b < 0 else f"({a}) × {b}", a * b


def ex_negative_both(grade):
    lo, hi = _rng(grade, (-30, -1), (-50, -1), (-99, -1))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    op = random.choice(["+", "-"])
    return f"({a}) {op} ({b})", (a + b if op == "+" else a - b)


def ex_negative_temperature(grade):
    start = random.randint(-15, 5)
    delta = random.randint(1, 20)
    op = random.choice(["ko'tarildi", "pasaydi"])
    result = start + delta if op == "ko'tarildi" else start - delta
    return f"Havo harorati {start}°C edi. Kechqurun {delta}° ga {op}. Hozir harorat necha daraja?", result


GEN_NEGATIVE = [ex_negative_add, ex_negative_mul, ex_negative_both, ex_negative_temperature]


# ---------- speed ----------
def ex_speed_distance(grade):
    lo_s, hi_s = _rng(grade, (10, 30), (30, 80), (60, 150))
    speed, time = random.randint(lo_s, hi_s), random.randint(1, 8)
    return f"Tezligi {speed} km/soat bo'lgan mashina {time} soatda necha km yo'l bosadi?", speed * time


def ex_speed_find_speed(grade):
    time = random.randint(2, 8)
    lo_s, hi_s = _rng(grade, (10, 40), (30, 90), (50, 150))
    speed = random.randint(lo_s, hi_s)
    distance = speed * time
    return f"Mashina {distance} km yo'lni {time} soatda bosib o'tdi. Uning tezligi necha km/soat?", speed


def ex_speed_find_time(grade):
    lo_s, hi_s = _rng(grade, (10, 40), (30, 90), (50, 150))
    speed = random.randint(lo_s, hi_s)
    time = random.randint(1, 8)
    distance = speed * time
    return f"{distance} km masofani {speed} km/soat tezlik bilan necha soatda bosib o'tish mumkin?", time


GEN_SPEED = [ex_speed_distance, ex_speed_find_speed, ex_speed_find_time]


# ---------- bank_percent ----------
# deposit har doim 100 ga karrali bo'lgani uchun pct/100 * deposit har doim butun son.
def _bank_deposit(grade):
    k = random.randint(*_rng(grade, (1, 20), (10, 100), (50, 500)))
    return 100 * k


def ex_bank_interest(grade):
    deposit = _bank_deposit(grade)
    pct = random.choice([2, 4, 5, 8, 10, 12, 15, 16, 20, 25])
    return f"{deposit} so'm depozitga {pct}% yillik foiz qo'shilsa, foiz summasi qancha so'm bo'ladi?", deposit * pct // 100


def ex_bank_total(grade):
    deposit = _bank_deposit(grade)
    pct = random.choice([2, 4, 5, 8, 10, 12, 15, 20, 25])
    return f"{deposit} so'm depozitga {pct}% yillik foiz qo'shilsa, 1 yildan keyin hisobdagi umumiy summa qancha bo'ladi?", deposit + deposit * pct // 100


def ex_bank_find_deposit(grade):
    pct = random.choice([4, 5, 10, 20, 25])
    result = _bank_deposit(grade) // 10  # kichikroq, real bo'lishi uchun
    if result == 0:
        result = 10
    deposit = result * 100 // pct
    return f"Bankka qo'yilgan pulga {pct}% foiz qo'shilganda {result} so'm foiz hosil bo'ldi. Boshlang'ich depozit qancha so'm edi?", deposit


def ex_bank_two_years(grade):
    # Oddiy foiz (yig'ma emas) asosida 2 yillik summa
    deposit = _bank_deposit(grade)
    pct = random.choice([2, 4, 5, 8, 10])
    total = deposit + 2 * (deposit * pct // 100)
    return f"{deposit} so'm depozitga har yili {pct}% oddiy foiz qo'shib borilsa, 2 yildan keyin hisobda qancha so'm bo'ladi?", total


GEN_BANK_PERCENT = [ex_bank_interest, ex_bank_total, ex_bank_find_deposit, ex_bank_two_years]


# ---------- trig ----------
# MUHIM TUZATISH: asl kodda sin(30°) va cos(60°) noto'g'ri "0" deb hisoblangan edi,
# aslida ularning qiymati 0.5 ga teng! Javob butun son bo'lishi kerakligi sababli,
# bu yerda barcha qiymatlarni FOIZ ko'rinishida so'raymiz (masalan sin(90°)=100%),
# shunda 0.5 kabi qiymatlar ham 50% sifatida to'g'ri va butun son bilan ifodalanadi.
# Faqat matematik jihatdan ANIQ (irratsional bo'lmagan) qiymatlar ishlatiladi:
# sin/cos ning 0°, 30°/60°, 90° dagi va tan ning 0°, 45° dagi qiymatlari.
TRIG_PERCENT_FACTS = [
    ("sin(0°)", 0), ("cos(90°)", 0),
    ("sin(30°)", 50), ("cos(60°)", 50),
    ("sin(90°)", 100), ("cos(0°)", 100),
]

TRIG_QUESTION_TEMPLATES = [
    "{q} ning qiymati necha foizga teng? (sin(90°) = 100% deb hisoblang)",
    "{q} nechaga teng, foiz ko'rinishida ayting? (masalan cos(0°) = 100%)",
    "Agar sin(90°) = 100% desak, {q} necha foizga teng bo'ladi?",
]


def ex_trig_value(grade):
    q, pct = random.choice(TRIG_PERCENT_FACTS)
    template = random.choice(TRIG_QUESTION_TEMPLATES)
    return template.format(q=q), pct


def ex_trig_identity(grade):
    # sin^2 + cos^2 = 1 ayniyati asosida (foiz ko'rinishida: 100% = butun ayniyat)
    known = random.choice(["sin", "cos"])
    other = "cos" if known == "sin" else "sin"
    return (
        f"sin²α + cos²α = 1 ayniyatiga ko'ra, agar {known}²α = 0 bo'lsa, "
        f"{other}²α nechaga teng?",
        1,
    )


def ex_trig_tan(grade):
    angle = random.choice([0, 45])
    ans = 0 if angle == 0 else 1
    return f"tan({angle}°) qiymatini toping. (0 yoki 1)", ans


def ex_trig_pythagorean(grade):
    # Katta emas, lekin trig bilan chambarchas bog'liq: to'g'ri burchakli
    # uchburchakda Pifagor teoremasi (gipotenuza uchun butun sonli uchliklar)
    triples = [
        (3, 4, 5), (6, 8, 10), (5, 12, 13), (9, 12, 15), (8, 15, 17),
        (7, 24, 25), (10, 24, 26), (20, 21, 29), (12, 16, 20), (9, 40, 41),
    ]
    a, b, c = random.choice(triples)
    if random.random() < 0.5:
        return f"To'g'ri burchakli uchburchakda katetlar {a} va {b}. Gipotenuza nechaga teng?", c
    missing_is_a = random.random() < 0.5
    known_cathetus = b if missing_is_a else a
    return f"To'g'ri burchakli uchburchakda gipotenuza {c}, bir katet {known_cathetus}. Ikkinchi katet nechaga teng?", (a if missing_is_a else b)


GEN_TRIG = [ex_trig_value, ex_trig_identity, ex_trig_tan, ex_trig_pythagorean]


# ---------- log ----------
LOG_BASES = [2, 3, 4, 5, 6, 7, 10]


def ex_log_direct(grade):
    base = random.choice(LOG_BASES)
    p = random.randint(1, 5)
    return f"log{base}({base**p}) = ?", p


def ex_log_find_base(grade):
    base = random.choice(LOG_BASES)
    p = random.randint(2, 4)
    return f"log_x({base**p}) = {p} bo'lsa, x ni toping.", base


def ex_log_find_power(grade):
    base = random.choice(LOG_BASES)
    p = random.randint(1, 5)
    return f"{base}^x = {base**p} bo'lsa, x ni toping.", p


def ex_log_of_one(grade):
    base = random.choice(LOG_BASES)
    return f"log{base}(1) nechaga teng? (log_a(1) = 0 xossasidan foydalaning)", 0


def ex_log_of_self(grade):
    base = random.choice(LOG_BASES)
    return f"log{base}({base}) nechaga teng? (log_a(a) = 1 xossasidan foydalaning)", 1


def ex_log_addition_rule(grade):
    # log_a(x) + log_a(y) = log_a(x*y)
    base = random.choice(LOG_BASES)
    p1, p2 = random.randint(1, 3), random.randint(1, 3)
    return (
        f"log{base}({base**p1}) + log{base}({base**p2}) yig'indisi log{base}(x) ko'rinishida "
        f"yozilsa, x nechaga teng?",
        base ** (p1 + p2),
    )


GEN_LOG = [
    ex_log_direct, ex_log_find_base, ex_log_find_power,
    ex_log_of_one, ex_log_of_self, ex_log_addition_rule,
]


# ---------- arith_prog ----------
def ex_arith_next(grade):
    a1, d = random.randint(1, 15), random.randint(1, 8)
    return f"Arifmetik progressiya: a1={a1}, d={d}. a2 nechaga teng?", a1 + d


def ex_arith_nth(grade):
    a1, d, n = random.randint(1, 15), random.randint(1, 12), random.randint(3, 10)
    return f"Arifmetik progressiya: a1={a1}, d={d}. a{n} nechaga teng?", a1 + (n - 1) * d


def ex_arith_sum(grade):
    a1, d, n = random.randint(1, 10), random.randint(1, 8), random.randint(3, 10)
    sn = n * (2 * a1 + (n - 1) * d) // 2
    return f"Arifmetik progressiya: a1={a1}, d={d}. Birinchi {n} ta hadning yig'indisi (S{n}) nechaga teng?", sn


def ex_arith_find_d(grade):
    a1 = random.randint(1, 15)
    d = random.randint(1, 10)
    a2 = a1 + d
    return f"Arifmetik progressiya: a1={a1}, a2={a2}. Ayirmasi (d) nechaga teng?", d


GEN_ARITH_PROG = [ex_arith_next, ex_arith_nth, ex_arith_sum, ex_arith_find_d]


# ---------- geom_prog ----------
def ex_geom_next(grade):
    a1, q = random.randint(1, 6), random.randint(2, 3)
    return f"Geometrik progressiya: a1={a1}, q={q}. a2 nechaga teng?", a1 * q


def ex_geom_nth(grade):
    a1, q, n = random.randint(1, 5), random.randint(2, 3), random.randint(2, 5)
    return f"Geometrik progressiya: a1={a1}, q={q}. a{n} nechaga teng?", a1 * (q ** (n - 1))


def ex_geom_sum(grade):
    a1, q, n = random.randint(1, 3), random.choice([2, 3]), random.randint(3, 5)
    sn = a1 * (q ** n - 1) // (q - 1)
    return f"Geometrik progressiya: a1={a1}, q={q}. Birinchi {n} ta hadning yig'indisi (S{n}) nechaga teng?", sn


def ex_geom_find_q(grade):
    a1 = random.randint(1, 6)
    q = random.randint(2, 4)
    a2 = a1 * q
    return f"Geometrik progressiya: a1={a1}, a2={a2}. Maxraji (q) nechaga teng?", q


GEN_GEOM_PROG = [ex_geom_next, ex_geom_nth, ex_geom_sum, ex_geom_find_q]


TOPIC_GENERATORS = {
    "add_sub": GEN_ADD_SUB,
    "mul_div": GEN_MUL_DIV,
    "percent": GEN_PERCENT,
    "fraction": GEN_FRACTION,
    "power": GEN_POWER,
    "sqrt": GEN_SQRT,
    "linear_eq": GEN_LINEAR_EQ,
    "quad_eq": GEN_QUAD_EQ,
    "triangle": GEN_TRIANGLE,
    "rectangle": GEN_RECTANGLE,
    "circle": GEN_CIRCLE,
    "ratio": GEN_RATIO,
    "average": GEN_AVERAGE,
    "negative": GEN_NEGATIVE,
    "speed": GEN_SPEED,
    "bank_percent": GEN_BANK_PERCENT,
    "trig": GEN_TRIG,
    "log": GEN_LOG,
    "arith_prog": GEN_ARITH_PROG,
    "geom_prog": GEN_GEOM_PROG,
}


def generate_example(user_id, topic, grade="medium"):
    """
    Berilgan mavzu/daraja uchun misol yaratadi. Har chaqiriqda tasodifiy
    mantiqiy variant (style) tanlanadi va foydalanuvchining shu mavzudagi
    so'nggi savollari bilan solishtirib, TAKRORLANMAYDIGAN savol qaytaradi.
    """
    generators = TOPIC_GENERATORS[topic]
    history = get_topic_history(user_id, topic)

    text, answer = None, None
    max_attempts = 40
    for _ in range(max_attempts):
        gen_func = random.choice(generators)
        candidate_text, candidate_answer = gen_func(grade)
        if candidate_text not in history:
            text, answer = candidate_text, candidate_answer
            break

    if text is None:
        # Barcha oson kombinatsiyalar tugagan bo'lsa (masalan trig/log kabi
        # cheklangan mavzularda) - eng eski tarixni tozalab, yangidan boshlaymiz
        history = set()
        gen_func = random.choice(generators)
        text, answer = gen_func(grade)

    save_to_history(user_id, topic, text)
    return text, answer


def get_hint_keyboard(topic):
    builder = InlineKeyboardBuilder()
    builder.button(text="💡 Yordam", callback_data=f"hint_{topic}")
    return builder.as_markup()


def generate_options(answer, allow_negative=False):
    """
    To'g'ri javob atrofida 3 ta noto'g'ri variant yaratadi.
    Variantlar javob kattaligiga mos (proportsional) qilib tanlanadi,
    shuningdek mavzu mantiqan manfiy son bo'lishi mumkin bo'lmasa,
    manfiy variantlar chiqarilmaydi.
    """
    options = {answer}
    magnitude = max(abs(answer), 4)
    # Javob kattaligiga qarab mos miqyosdagi farqlar to'plami
    deltas = sorted(set(
        [1, 2, 3] +
        [max(1, magnitude // 20), max(1, magnitude // 10), max(1, magnitude // 5)]
    ))

    attempts = 0
    while len(options) < 4 and attempts < 60:
        attempts += 1
        delta = random.choice(deltas) * random.choice([-1, 1])
        candidate = answer + delta
        if not allow_negative and candidate < 0:
            candidate = answer + abs(delta)
        if candidate == answer or candidate in options:
            continue
        if not allow_negative and candidate < 0:
            continue
        options.add(candidate)

    # Agar hali ham 4 taga yetmagan bo'lsa (juda kichik javoblarda bo'lishi mumkin)
    filler = 1
    while len(options) < 4:
        candidate = answer + filler if allow_negative or answer + filler >= 0 else answer + filler + magnitude
        if candidate not in options and (allow_negative or candidate >= 0):
            options.add(candidate)
        filler += 1
        if filler > 1000:
            break

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


def get_topics_keyboard(grade=None):
    builder = InlineKeyboardBuilder()
    allowed = topics_for_grade(grade) if grade else list(TOPICS.keys())
    for key in allowed:
        builder.button(text=TOPICS[key], callback_data=f"topic_{key}")
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
speedtest_active = {}  # {user_id: {"count": 0}}


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
        f"Avval sinf darajangizni tanlang (misollarning qiyinligi va mavzular ro'yxati shunga qarab moslanadi):",
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
    await callback.message.answer("Mavzuni tanlang:", reply_markup=get_topics_keyboard(grade))
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
    example_text, answer = generate_example(user_id, topic, user["grade"])
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))

    update_user(user_id, current_topic=topic, current_answer=answer, current_mode="normal", current_question=example_text)

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
        old_question = user["current_question"] or "?"
        log_mistake(user_id, topic, old_question, correct_answer)
        solution_hint = HINTS.get(topic, "")
        result_text = f"❌ Noto'g'ri. To'g'ri javob: {correct_answer}\n📖 Yechim: {solution_hint}"

    # Keyingi mavzuni tanlaymiz (random/challenge rejimda - yangi tasodifiy mavzu)
    mode = user["current_mode"]
    if mode == "random":
        next_topic = random.choice(topics_for_grade(user["grade"]))
    elif mode == "challenge":
        next_topic = random.choice(list(TOPICS.keys()))
    else:
        next_topic = topic

    effective_grade = "hard" if mode == "challenge" else user["grade"]
    example_text, answer = generate_example(user_id, next_topic, effective_grade)
    options = generate_options(answer, allow_negative=topic_allows_negative(next_topic))
    update_user(user_id, current_topic=next_topic, current_answer=answer, current_question=example_text)

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
    user = get_user(message.from_user.id, message.from_user.first_name)
    await message.answer("Mavzuni tanlang:", reply_markup=get_topics_keyboard(user["grade"]))


@dp.message(Command("random"))
async def random_handler(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name)
    update_user(user_id, current_mode="random")

    topic = random.choice(topics_for_grade(user["grade"]))
    example_text, answer = generate_example(user_id, topic, user["grade"])
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))
    update_user(user_id, current_topic=topic, current_answer=answer, current_question=example_text)

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
    example_text, answer = generate_example(user_id, topic, "hard")
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))
    update_user(user_id, current_topic=topic, current_answer=answer, current_question=example_text)

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


@dp.message(Command("creategroup"))
async def creategroup_handler(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Guruh nomini kiriting: /creategroup 9-A sinf")
        return

    name = parts[1].strip()
    get_user(message.from_user.id, message.from_user.first_name)
    code = create_group(message.from_user.id, name)

    if code is None:
        await message.answer("Xatolik yuz berdi, qayta urinib ko'ring.")
        return

    await message.answer(
        f"🏫 Guruh yaratildi: {name}\n\n"
        f"Qo'shilish kodi: `{code}`\n\n"
        f"O'quvchilaringizga shu kodni bering, ular botga `/joingroup {code}` deb yozishlari kerak.\n"
        f"O'quvchilar ro'yxatini ko'rish uchun /mystudents yozing.",
        parse_mode="Markdown"
    )


@dp.message(Command("joingroup"))
async def joingroup_handler(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Guruh kodini kiriting: /joingroup ABC123")
        return

    code = parts[1].strip().upper()
    group = get_group(code)

    if group is None:
        await message.answer("Bunday kodli guruh topilmadi. Kodni tekshirib qayta urinib ko'ring.")
        return

    get_user(message.from_user.id, message.from_user.first_name)
    update_user(message.from_user.id, group_code=code)

    await message.answer(f"✅ Siz \"{group[2]}\" guruhiga qo'shildingiz!")


@dp.message(Command("mystudents"))
async def mystudents_handler(message: types.Message):
    teacher_id = message.from_user.id
    groups = get_teacher_groups(teacher_id)

    if not groups:
        await message.answer("Sizda hali guruh yo'q. /creategroup <nom> orqali yarating.")
        return

    text = ""
    for code, name in groups:
        students = get_group_students(code)
        text += f"🏫 {name} (kod: {code})\n"
        if not students:
            text += "   Hali o'quvchi qo'shilmagan.\n\n"
            continue
        for first_name, correct, wrong in students:
            total = correct + wrong
            pct = round(correct / total * 100) if total > 0 else 0
            text += f"   • {first_name} — {correct} to'g'ri ({pct}%)\n"
        text += "\n"

    await message.answer(text)


@dp.message(Command("path"))
async def path_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)
    statuses, current_topic = get_path_status(user_id)

    text = "📚 O'quv yo'lingiz:\n\n"
    for t, status in statuses:
        icon = {"done": "✅", "current": "▶️", "locked": "🔒"}[status]
        text += f"{icon} {TOPICS[t]}\n"

    text += f"\nHozirgi bosqich: {TOPICS[current_topic]}\n(Bir bosqichni ochish uchun kamida 5 ta savolda 70% to'g'ri javob bering)"

    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Boshlash", callback_data=f"pathstart_{current_topic}")
    await message.answer(text, reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("pathstart_"))
async def pathstart_handler(callback: types.CallbackQuery):
    topic = callback.data.replace("pathstart_", "")
    await start_topic_question(
        callback.from_user.id, callback.from_user.first_name,
        topic, callback_message=callback.message
    )
    await callback.answer()


@dp.message(Command("mycode"))
async def mycode_handler(message: types.Message):
    get_user(message.from_user.id, message.from_user.first_name)
    code = ensure_personal_code(message.from_user.id)
    await message.answer(
        f"🔗 Sizning shaxsiy kodingiz: `{code}`\n\n"
        f"Do'stingizga shu kodni bering, u /compare {code} deb yozib natijalaringizni solishtira oladi.",
        parse_mode="Markdown"
    )


@dp.message(Command("compare"))
async def compare_handler(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Do'stingizning kodini kiriting: /compare 12345\n\nO'z kodingizni ko'rish uchun /mycode yozing.")
        return

    code = parts[1].strip().upper()
    other = get_user_by_personal_code(code)

    if other is None:
        await message.answer("Bunday kodli foydalanuvchi topilmadi.")
        return

    if other["user_id"] == message.from_user.id:
        await message.answer("Bu sizning o'z kodingiz 🙂 Do'stingizning kodini kiriting.")
        return

    me = get_user(message.from_user.id, message.from_user.first_name)
    me_total = me["correct"] + me["wrong"]
    me_pct = round(me["correct"] / me_total * 100) if me_total > 0 else 0
    other_total = other["correct"] + other["wrong"]
    other_pct = round(other["correct"] / other_total * 100) if other_total > 0 else 0

    await message.answer(
        f"🔗 Taqqoslash:\n\n"
        f"👤 {me['first_name']}: {me['correct']} to'g'ri, {me_pct}% aniqlik, 🔥{me['streak']} kun\n"
        f"👤 {other['first_name']}: {other['correct']} to'g'ri, {other_pct}% aniqlik, 🔥{other['streak']} kun"
    )


@dp.message(Command("speedtest"))
async def speedtest_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)

    if user_id in speedtest_active:
        await message.answer("Siz allaqachon tezlik testida ishtirok etyapsiz! Javob yozishda davom eting.")
        return

    speedtest_active[user_id] = {"count": 0}
    example_text, answer = generate_example(user_id, "add_sub", "medium")
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

    if is_speedtest and user["current_answer"] is not None and message.text and message.text.lstrip("-").isdigit():
        correct_answer = user["current_answer"]
        user_answer = int(message.text)

        if user_answer == correct_answer:
            speedtest_active[user_id]["count"] += 1
            await message.answer("✅ To'g'ri!")
        else:
            await message.answer(f"❌ Noto'g'ri. To'g'ri javob: {correct_answer}")

        example_text, answer = generate_example(user_id, "add_sub", "medium")
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
        types.BotCommand(command="path", description="🗺️ O'quv yo'lim"),
        types.BotCommand(command="random", description="🎲 Aralash rejim"),
        types.BotCommand(command="challenge", description="⭐ Qiyin savollar"),
        types.BotCommand(command="speedtest", description="⏱️ Tezlik testi"),
        types.BotCommand(command="formula", description="🧮 Formulalar bazasi"),
        types.BotCommand(command="mistakes", description="📖 Xatolaringiz"),
        types.BotCommand(command="map", description="📊 Bilim xaritangiz"),
        types.BotCommand(command="stats", description="📈 Statistikangiz"),
        types.BotCommand(command="top", description="🏆 Umumiy reyting"),
        types.BotCommand(command="topweek", description="📅 Haftalik reyting"),
        types.BotCommand(command="mycode", description="🔗 Shaxsiy kodim"),
        types.BotCommand(command="compare", description="🔗 Do'st bilan solishtirish"),
        types.BotCommand(command="creategroup", description="🏫 Guruh yaratish (o'qituvchi)"),
        types.BotCommand(command="joingroup", description="🏫 Guruhga qo'shilish"),
        types.BotCommand(command="mystudents", description="🏫 O'quvchilarim (o'qituvchi)"),
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
