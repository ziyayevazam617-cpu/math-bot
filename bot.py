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
    "linear_eq", "quad_eq", "system_eq",
    "triangle", "rectangle", "circle",
    "speed", "bank_percent",
    "trig", "log", "expo_eq", "arith_prog", "geom_prog", "combinatorics",
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
        f"рџ“ќ Misol: {example_text}\n\n"
        f"To'g'ri javobni tanlang рџ‘‡"
    )
    markup = get_answer_keyboard(topic, options)

    if callback_message:
        await callback_message.edit_text(text, reply_markup=markup)
    elif message:
        await message.answer(text, reply_markup=markup)


# ==================== TOPICS ====================
TOPICS = {
    "add_sub": "вћ• Qo'shish/Ayirish",
    "mul_div": "вњ–пёЏ Ko'paytirish/Bo'lish",
    "percent": "% Foizlar",
    "fraction": "ВЅ Kasrlar",
    "power": "xВІ Darajalar",
    "sqrt": "в€љ Kvadrat ildiz",
    "linear_eq": "рџ”¤ Chiziqli tenglama",
    "quad_eq": "рџ”¤ Kvadrat tenglama",
    "system_eq": "рџ”— Tenglamalar sistemasi",
    "triangle": "рџ”є Uchburchak",
    "rectangle": "в–­ To'rtburchak",
    "circle": "в­• Doira",
    "ratio": "вљ–пёЏ Nisbat",
    "average": "рџ“Љ O'rtacha qiymat",
    "negative": "вћ– Manfiy sonlar",
    "speed": "рџљ— Tezlik-vaqt-masofa",
    "bank_percent": "рџЏ¦ Foiz o'sishi",
    "trig": "рџ“ђ Trigonometriya",
    "log": "рџ“€ Logarifm",
    "expo_eq": "рџ“¶ Ko'rsatkichli tenglama",
    "arith_prog": "рџ”ў Arifmetik progressiya",
    "geom_prog": "рџ”ў Geometrik progressiya",
    "combinatorics": "рџЋІ Kombinatorika",
}


GRADE_LABELS = {
    "easy": "рџџў 5-sinf",
    "medium": "рџџЎ 8-9 sinf",
    "hard": "рџ”ґ 10-11 sinf",
}

# 5-sinf endi mustaqil daraja sifatida ishlaydi.
# 6-7 sinf keyingi bosqichda alohida qo'shiladi.
GRADE_TOPICS = {
    "easy": [
        "add_sub", "mul_div", "fraction", "percent", "power",
        "ratio", "average", "linear_eq", "triangle", "rectangle", "speed",
    ],
}

# 8-9 va 10-11 sinfning avvalgi mavzularini saqlab qolamiz;
# ular hozircha 5-sinf blokiga bog'lanmaydi.
GRADE_TOPICS["medium"] = [
    "add_sub", "negative", "mul_div", "fraction", "percent", "power",
    "sqrt", "ratio", "average", "linear_eq", "triangle", "rectangle",
    "circle", "speed", "quad_eq", "system_eq", "bank_percent", "trig",
    "arith_prog", "geom_prog",
]
GRADE_TOPICS["hard"] = GRADE_TOPICS["medium"] + ["log", "expo_eq", "combinatorics"]


def topics_for_grade(grade):
    return GRADE_TOPICS.get(grade, list(TOPICS.keys()))

HINTS = {
    "add_sub": "Sonlarni raqam ustuniga joylab, o'ngdan chapga qo'shing/ayiring.",
    "mul_div": "Ko'paytirish jadvalini eslang, bo'lishda qaysi songa necha marta sig'ishini toping.",
    "percent": "Foizni 100 ga bo'lib, songa ko'paytiring: son Г— foiz Г· 100.",
    "fraction": "Avval sonni maxrajga bo'ling, keyin suratga ko'paytiring.",
    "power": "Daraja - sonni o'zi bilan necha marta ko'paytirish kerakligini bildiradi.",
    "sqrt": "Qaysi son o'zi bilan ko'paytirilganda shu songa teng bo'lishini toping.",
    "linear_eq": "Avval erkin hadni ikkala tomondan ayiring, keyin x oldidagi songa bo'ling.",
    "quad_eq": "Ildizlar yig'indisi -b, ko'paytmasi c ga teng (Vieta teoremasi).",
    "system_eq": "Ikkinchi tenglamadan bitta o'zgaruvchini ifodalab, birinchisiga qo'ying (o'rniga qo'yish usuli) yoki ikkala tenglamani mos songa ko'paytirib qo'shing/ayiring (qo'shish usuli).",
    "triangle": "Uchburchak yuzasi = (asos Г— balandlik) Г· 2.",
    "rectangle": "To'rtburchak yuzasi = tomon Г— tomon.",
    "circle": "Doira yuzasi = ПЂ Г— r Г— r.",
    "ratio": "Nisbatning ikkala tomonini bir xil songa ko'paytiring yoki bo'ling.",
    "average": "Barcha sonlarni qo'shib, sonlar soniga bo'ling.",
    "negative": "Manfiy sonlar bilan ishlashda son o'qini tasavvur qiling.",
    "speed": "Masofa = tezlik Г— vaqt.",
    "bank_percent": "Foiz summasi = depozit Г— foiz Г· 100.",
    "trig": "Asosiy burchaklar (0В°, 30В°, 45В°, 60В°, 90В°) qiymatlarini yodda tuting.",
    "log": "log_a(b) = c degani a^c = b degani.",
    "expo_eq": "Ikkala tomonni bir xil asosga keltiring, so'ng darajalarni tenglashtiring: a^x = a^n bo'lsa, x = n.",
    "arith_prog": "a_n = a1 + (n-1) Г— d formulasidan foydalaning.",
    "geom_prog": "a_n = a1 Г— q^(n-1) formulasidan foydalaning.",
    "combinatorics": "Tartib muhim bo'lsa - o'rin almashtirish (P/A), tartib muhim bo'lmasa - kombinatsiya (C) formulasidan foydalaning.",
}

# Qaysi mavzularda manfiy javob/variant mantiqan to'g'ri kelishi mumkin
NEGATIVE_ALLOWED_TOPICS = {"add_sub", "negative", "linear_eq", "system_eq"}


def topic_allows_negative(topic):
    return topic in NEGATIVE_ALLOWED_TOPICS


FORMULAS = {
    "add_sub": (
        "вћ• QO'SHISH VA AYIRISH\n\n"
        "вЂў a + b = qo'shindi (summa)\n"
        "вЂў a в€’ b = ayirma\n"
        "вЂў a + b = b + a (o'rin almashtirish qonuni)\n"
        "вЂў (a + b) + c = a + (b + c) (guruhlash qonuni)\n"
        "вЂў a в€’ b в‰  b в€’ a (ayirishda o'rin almashtirib bo'lmaydi)\n"
        "вЂў a + 0 = a, a в€’ 0 = a\n"
        "вЂў a в€’ a = 0\n\n"
        "рџ“Њ Qoida: ko'p xonali sonlarni qo'shish/ayirishda raqamlarni o'ngdan chapga, "
        "xona-xona (birlik, o'nlik, yuzlik...) tekislab yozing."
    ),
    "mul_div": (
        "вњ–пёЏ KO'PAYTIRISH VA BO'LISH\n\n"
        "вЂў a Г— b = ko'paytma\n"
        "вЂў a Г· b = bo'linma (b в‰  0)\n"
        "вЂў a Г— b = b Г— a (o'rin almashtirish qonuni)\n"
        "вЂў (a Г— b) Г— c = a Г— (b Г— c) (guruhlash qonuni)\n"
        "вЂў a Г— (b + c) = aГ—b + aГ—c (taqsimot qonuni)\n"
        "вЂў a Г— 1 = a,  a Г— 0 = 0\n"
        "вЂў a Г· 1 = a,  a Г· a = 1 (a в‰  0)\n"
        "вЂў Bo'linma tekshiruvi: a = b Г— natija + qoldiq"
    ),
    "percent": (
        "% FOIZLAR\n\n"
        "вЂў Sonning n% i = son Г— n Г· 100\n"
        "вЂў Foizni songa aylantirish: n% = n Г· 100\n"
        "вЂў Narx n% ga oshsa: yangi narx = eski narx + eski narxГ—nГ·100 = eski narxГ—(1 + n/100)\n"
        "вЂў Narx n% ga tushsa: yangi narx = eski narx в€’ eski narxГ—nГ·100 = eski narxГ—(1 в€’ n/100)\n"
        "вЂў A soni B sonining necha foizini tashkil qiladi: (A Г· B) Г— 100%\n"
        "вЂў Butun son = qism Г· (foiz Г· 100)"
    ),
    "fraction": (
        "ВЅ KASRLAR\n\n"
        "вЂў Sonning a/b qismi = son Г— a Г· b\n"
        "вЂў Kasrlarni qo'shish (bir xil maxrajda): a/c + b/c = (a+b)/c\n"
        "вЂў Kasrlarni ko'paytirish: (a/b) Г— (c/d) = (aГ—c)/(bГ—d)\n"
        "вЂў Kasrlarni bo'lish: (a/b) Г· (c/d) = (a/b) Г— (d/c)\n"
        "вЂў Aralash sonni kasrga aylantirish: a b/c = (aГ—c+b)/c\n"
        "вЂў Qisqartirish: a/b = (aГ·k)/(bГ·k), k вЂ” umumiy bo'luvchi"
    ),
    "power": (
        "xВІ DARAJALAR\n\n"
        "вЂў aвЃї = a Г— a Г— ... Г— a (n marta)\n"
        "вЂў aВ№ = a,  aвЃ° = 1 (a в‰  0)\n"
        "вЂў aбµђ Г— aвЃї = aбµђвЃєвЃї\n"
        "вЂў aбµђ Г· aвЃї = aбµђвЃ»вЃї\n"
        "вЂў (aбµђ)вЃї = aбµђГ—вЃї\n"
        "вЂў (aГ—b)вЃї = aвЃї Г— bвЃї\n"
        "вЂў (a+b)ВІ = aВІ + 2ab + bВІ\n"
        "вЂў (aв€’b)ВІ = aВІ в€’ 2ab + bВІ\n"
        "вЂў aВІ в€’ bВІ = (aв€’b)(a+b)"
    ),
    "sqrt": (
        "в€љ KVADRAT ILDIZ\n\n"
        "вЂў в€љa = shunday b в‰Ґ 0 ki, b Г— b = a\n"
        "вЂў в€љ(aГ—b) = в€љa Г— в€љb\n"
        "вЂў в€љ(a/b) = в€љa Г· в€љb (b в‰  0)\n"
        "вЂў (в€љa)ВІ = a (a в‰Ґ 0)\n"
        "вЂў в€љaВІ = |a|\n"
        "вЂў Muhim kvadratlar: 1,4,9,16,25,36,49,64,81,100,121,144,169,196,225,...\n"
        "  ularning ildizlari mos ravishda: 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,..."
    ),
    "linear_eq": (
        "рџ”¤ CHIZIQLI TENGLAMA\n\n"
        "вЂў x + b = c  в†’  x = c в€’ b\n"
        "вЂў ax = c  в†’  x = c Г· a (a в‰  0)\n"
        "вЂў ax + b = c  в†’  x = (c в€’ b) Г· a\n"
        "вЂў ax + b = cx + d  в†’  x = (d в€’ b) Г· (a в€’ c)\n"
        "вЂў Tenglamaning ikkala tomoniga bir xil son qo'shish/ayirish yechimni o'zgartirmaydi\n"
        "вЂў Tenglamaning ikkala tomonini bir xil (nolmas) songa ko'paytirish/bo'lish yechimni o'zgartirmaydi"
    ),
    "quad_eq": (
        "рџ”¤ KVADRAT TENGLAMA\n\n"
        "вЂў Umumiy ko'rinish: axВІ + bx + c = 0 (a в‰  0)\n"
        "вЂў Diskriminant: D = bВІ в€’ 4ac\n"
        "вЂў Ildizlar: x = (в€’b В± в€љD) Г· (2a)\n"
        "вЂў D > 0 в†’ 2 ta ildiz, D = 0 в†’ 1 ta ildiz, D < 0 в†’ haqiqiy ildiz yo'q\n"
        "вЂў Vieta teoremasi (xВІ + px + q = 0 uchun):\n"
        "  x1 + x2 = в€’p\n"
        "  x1 Г— x2 = q"
    ),
    "system_eq": (
        "рџ”— TENGLAMALAR SISTEMASI\n\n"
        "вЂў Umumiy ko'rinish: ax + by = c,  dx + ey = f\n"
        "вЂў O'RNIGA QO'YISH USULI: bitta tenglamadan bitta o'zgaruvchini "
        "ifodalang (masalan y = ...), so'ng ikkinchi tenglamaga qo'ying\n"
        "вЂў QO'SHISH (ALGEBRAIK) USULI: tenglamalarni bir xil koeffitsientlar "
        "hosil bo'ladigan songa ko'paytirib, qo'shish yoki ayirish orqali "
        "bitta o'zgaruvchini yo'qotasiz\n"
        "вЂў GRAFIK USULI: har ikkala tenglama chiziq sifatida chizilib, "
        "kesishish nuqtasi yechim bo'ladi\n"
        "вЂў Sistema yechimlari soni: agar to'g'ri chiziqlar kesishsa - 1 ta "
        "yechim, parallel bo'lsa - yechim yo'q, ustma-ust tushsa - cheksiz "
        "yechim"
    ),
    "triangle": (
        "рџ”є UCHBURCHAK\n\n"
        "вЂў Yuza = (asos Г— balandlik) Г· 2\n"
        "вЂў Perimetr = a + b + c (barcha tomonlar yig'indisi)\n"
        "вЂў Burchaklar yig'indisi = 180В°\n"
        "вЂў Teng yonli uchburchakda 2 tomon va 2 burchak teng\n"
        "вЂў Teng tomonli uchburchakda barcha tomon va burchaklar teng (har biri 60В°)\n"
        "вЂў To'g'ri burchakli uchburchakda: Pifagor teoremasi вЂ” aВІ + bВІ = cВІ (c вЂ” gipotenuza)"
    ),
    "rectangle": (
        "в–­ TO'RTBURCHAK (TO'G'RI TO'RTBURCHAK)\n\n"
        "вЂў Yuza = a Г— b\n"
        "вЂў Perimetr = 2 Г— (a + b)\n"
        "вЂў Diagonal (Pifagor bo'yicha): d = в€љ(aВІ + bВІ)\n"
        "вЂў Kvadrat uchun (a = b): Yuza = aВІ, Perimetr = 4a"
    ),
    "circle": (
        "в­• DOIRA VA AYLANA\n\n"
        "вЂў Doira yuzasi = ПЂ Г— rВІ\n"
        "вЂў Aylana uzunligi (perimetri) = 2 Г— ПЂ Г— r = ПЂ Г— d\n"
        "вЂў Diametr d = 2 Г— r\n"
        "вЂў ПЂ в‰€ 3.14 yoki 22/7 (taqribiy)\n"
        "вЂў Radius yuza orqali: r = в€љ(Yuza Г· ПЂ)"
    ),
    "ratio": (
        "вљ–пёЏ NISBAT VA PROPORTSIYA\n\n"
        "вЂў a : b = c : d  в†’  a Г— d = b Г— c (proportsiya asosiy xossasi)\n"
        "вЂў Nisbatning ikkala tomonini bir xil songa ko'paytirish/bo'lish nisbatni o'zgartirmaydi\n"
        "вЂў Sonni a:b nisbatda ulashish: kichik qism = son Г— a Г· (a+b), katta qism = son Г— b Г· (a+b)\n"
        "вЂў To'g'ri proportsionallik: y = k Г— x\n"
        "вЂў Teskari proportsionallik: y = k Г· x"
    ),
    "average": (
        "рџ“Љ O'RTACHA QIYMAT\n\n"
        "вЂў O'rtacha (arifmetik) = (barcha sonlar yig'indisi) Г· (sonlar soni)\n"
        "вЂў Yig'indi = o'rtacha Г— sonlar soni\n"
        "вЂў Agar bitta son ma'lum bo'lmasa: noma'lum son = (o'rtacha Г— soni) в€’ (ma'lum sonlar yig'indisi)"
    ),
    "negative": (
        "вћ– MANFIY SONLAR\n\n"
        "вЂў (в€’a) + (в€’b) = в€’(a+b)\n"
        "вЂў (в€’a) в€’ b = в€’(a+b)\n"
        "вЂў a в€’ (в€’b) = a + b\n"
        "вЂў (в€’a) + b = b в€’ a\n"
        "вЂў (в€’a) Г— (в€’b) = a Г— b (manfiy Г— manfiy = musbat)\n"
        "вЂў (в€’a) Г— b = в€’(aГ—b) (manfiy Г— musbat = manfiy)\n"
        "вЂў (в€’a) Г· (в€’b) = a Г· b\n"
        "вЂў (в€’a) Г· b = в€’(aГ·b)"
    ),
    "speed": (
        "рџљ— TEZLIK-VAQT-MASOFA\n\n"
        "вЂў Masofa (S) = Tezlik (V) Г— Vaqt (T)\n"
        "вЂў Tezlik (V) = Masofa (S) Г· Vaqt (T)\n"
        "вЂў Vaqt (T) = Masofa (S) Г· Tezlik (V)\n"
        "вЂў Qarama-qarshi harakatda: yaqinlashish tezligi = V1 + V2\n"
        "вЂў Bir yo'nalishda quvib o'tishda: V(farq) = V1 в€’ V2"
    ),
    "bank_percent": (
        "рџЏ¦ FOIZ O'SISHI (BANK DEPOZITI)\n\n"
        "вЂў Foiz summasi = Depozit Г— Foiz stavkasi Г· 100\n"
        "вЂў 1 yildan keyingi umumiy summa = Depozit + Foiz summasi = Depozit Г— (1 + stavka/100)\n"
        "вЂў Oddiy foiz (n yil): Summa = Depozit Г— (1 + nГ—stavka/100)\n"
        "вЂў Murakkab foiz (n yil): Summa = Depozit Г— (1 + stavka/100)вЃї"
    ),
    "trig": (
        "рџ“ђ TRIGONOMETRIYA\n\n"
        "Asosiy burchaklar jadvali:\n"
        "вЂў sin: 0В°в†’0, 30В°в†’0.5, 45В°в†’в€љ2/2, 60В°в†’в€љ3/2, 90В°в†’1\n"
        "вЂў cos: 0В°в†’1, 30В°в†’в€љ3/2, 45В°в†’в€љ2/2, 60В°в†’0.5, 90В°в†’0\n"
        "вЂў tan: 0В°в†’0, 45В°в†’1, 90В°в†’aniqlanmagan\n\n"
        "вЂў sinВІО± + cosВІО± = 1 (asosiy trigonometrik ayniyat)\n"
        "вЂў tan О± = sin О± Г· cos О±"
    ),
    "log": (
        "рџ“€ LOGARIFM\n\n"
        "вЂў log_a(b) = c  вџє  a^c = b  (a > 0, a в‰  1, b > 0)\n"
        "вЂў log_a(1) = 0\n"
        "вЂў log_a(a) = 1\n"
        "вЂў log_a(xГ—y) = log_a(x) + log_a(y)\n"
        "вЂў log_a(xГ·y) = log_a(x) в€’ log_a(y)\n"
        "вЂў log_a(xвЃї) = n Г— log_a(x)"
    ),
    "expo_eq": (
        "рџ“¶ KO'RSATKICHLI TENGLAMA\n\n"
        "вЂў Umumiy g'oya: a^x = a^n  вџє  x = n  (a > 0, a в‰  1)\n"
        "вЂў a^x Г— a^y = a^(x+y)\n"
        "вЂў a^x Г· a^y = a^(xв€’y)\n"
        "вЂў (a^x)^y = a^(xГ—y)\n"
        "вЂў Ikkala tomonni BIR XIL ASOSGA keltirib, so'ng darajalarni tenglashtiring\n"
        "вЂў Masalan: 8^x = 2^9 в†’ (2Ві)^x = 2^9 в†’ 2^(3x) = 2^9 в†’ 3x = 9 в†’ x = 3"
    ),
    "arith_prog": (
        "рџ”ў ARIFMETIK PROGRESSIYA\n\n"
        "вЂў n-had: a_n = a1 + (nв€’1) Г— d\n"
        "вЂў Ayirma: d = a_(n+1) в€’ a_n\n"
        "вЂў Yig'indi: S_n = n Г— (2Г—a1 + (nв€’1)Г—d) Г· 2\n"
        "вЂў Yig'indi (boshqa ko'rinish): S_n = n Г— (a1 + a_n) Г· 2"
    ),
    "geom_prog": (
        "рџ”ў GEOMETRIK PROGRESSIYA\n\n"
        "вЂў n-had: a_n = a1 Г— q^(nв€’1)\n"
        "вЂў Maxraj: q = a_(n+1) Г· a_n\n"
        "вЂў Yig'indi (q в‰  1): S_n = a1 Г— (qвЃї в€’ 1) Г· (q в€’ 1)\n"
        "вЂў Cheksiz kamayuvchi progressiya yig'indisi (|q| < 1): S = a1 Г· (1 в€’ q)"
    ),
    "combinatorics": (
        "рџЋІ KOMBINATORIKA\n\n"
        "вЂў Faktorial: n! = 1 Г— 2 Г— 3 Г— ... Г— n  (0! = 1)\n"
        "вЂў Ko'paytirish qoidasi: agar 1-tanlov m xil, 2-tanlov n xil usulda "
        "bo'lsa, ikkalasi birga mГ—n xil usulda bajariladi\n"
        "вЂў O'rin almashtirish (permutatsiya): P_n = n!\n"
        "вЂў Joylashtirish (tartib MUHIM): A_n^k = n! Г· (nв€’k)!\n"
        "вЂў Kombinatsiya (tartib MUHIM EMAS): C_n^k = n! Г· (k! Г— (nв€’k)!)"
    ),
}

MOTIVATIONS = [
    "вњ… To'g'ri! Zo'r ishladingiz!",
    "вњ… To'g'ri! Ajoyib!",
    "вњ… To'g'ri! Siz iqtidorlisiz!",
    "вњ… To'g'ri! Davom eting!",
    "вњ… To'g'ri! Zo'r natija!",
    "вњ… To'g'ri! Mukammal!",
    "вњ… To'g'ri! Shunday davom eting!",
]

NAMES_POOL = ["Ahmad", "Vali", "Aziza", "Dilnoza", "Sardor", "Malika", "Jasur", "Nodira", "Bekzod", "Kamola"]
ITEMS_POOL = ["olma", "qalam", "daftar", "konfet", "kitob", "yong'oq", "shar", "gul"]


# ==================== MISOL GENERATORLARI ====================
# Har bir generator funksiya (savol_matni, javob) qaytaradi va qaysi sinf
# darajalarida ("easy"=5-7, "medium"=8-9, "hard"=10-11) ishlatilishi mumkinligini
# bildiruvchi TIERS to'plamiga ega bo'ladi.
#
# MUHIM PRINSIP: "medium" va "hard" darajalar uchun FAQAT o'sha darajaga mos
# KUCHLIROQ va MANTIQIY jihatdan chuqurroq generatorlar ishlatiladi - oddiy/bolalarcha
# ("necha ta olma qoldi" kabi) misollar faqat "easy" darajada qoladi. Bu orqali
# 8-9 sinf o'quvchisiga hech qachon 5-7 sinf darajasidagi oddiy misol chiqmaydi.

ALL_TIERS = {"easy", "medium", "hard"}
EM_TIERS = {"easy", "medium"}
MH_TIERS = {"medium", "hard"}
M_ONLY = {"medium"}
H_ONLY = {"hard"}


def _rng(grade, easy, medium, hard):
    return {"easy": easy, "medium": medium, "hard": hard}[grade]


# ============================================================
# ---------- add_sub ----------
# ============================================================
def ex_add_sub_plain(grade):
    lo, hi = _rng(grade, (5, 40), (100, 900), (500, 9999))
    op = random.choice(["+", "-"])
    if op == "-":
        a, b = sorted([random.randint(lo, hi), random.randint(lo, hi)], reverse=True)
        return f"{a} в€’ {b}", a - b
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"{a} + {b}", a + b


def ex_add_sub_shop(grade):
    lo, hi = _rng(grade, (15, 60), None, None)
    a = random.randint(lo, hi)
    b = random.randint(1, a - 1)
    item = random.choice(ITEMS_POOL)
    return f"Do'konda {a} ta {item} bor edi. {b} ta sotib olishdi. Necha ta {item} qoldi?", a - b


def ex_add_sub_two_people(grade):
    lo, hi = _rng(grade, (5, 40), None, None)
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    n1, n2 = random.sample(NAMES_POOL, 2)
    item = random.choice(ITEMS_POOL)
    return f"{n1} {a} ta, {n2} {b} ta {item} sotib oldi. Ular jami nechta {item} sotib olishdi?", a + b


def ex_add_sub_bus(grade):
    lo, hi = _rng(grade, (10, 40), None, None)
    start = random.randint(lo, hi)
    left = random.randint(1, start)
    came = random.randint(1, 20)
    return f"Avtobusda {start} ta yo'lovchi bor edi. Bekatda {left} kishi tushdi, {came} kishi chiqdi. Endi avtobusda nechta yo'lovchi bor?", start - left + came


def ex_add_sub_three_terms(grade):
    lo, hi = _rng(grade, (10, 50), (200, 900), (500, 3000))
    a = random.randint(lo, hi)
    op1 = random.choice(["+", "-"])
    b = random.randint(1, a) if op1 == "-" else random.randint(lo, hi)
    val1 = a + b if op1 == "+" else a - b
    op2 = random.choice(["+", "-"]) if val1 > 0 else "+"
    c = random.randint(1, val1) if op2 == "-" else random.randint(lo, hi)
    val2 = val1 + c if op2 == "+" else val1 - c
    return f"{a} {op1} {b} {op2} {c}", val2


def ex_add_sub_balance(grade):
    # Bank hisobi / byudjet kontekstida - 8-9 sinf uchun jiddiyroq mavzu
    lo, hi = _rng(grade, None, (500, 5000), (2000, 20000))
    start = random.randint(lo, hi)
    n_ops = random.choice([3, 4])
    balance = start
    parts = [f"{start} so'm hisobda bor edi"]
    for _ in range(n_ops):
        # Balans 0 (yoki manfiy) bo'lib qolgan bo'lsa, faqat kirim bo'lishi mumkin -
        # aks holda random.randint(1, balance) xato beradi
        op = random.choice(["kirim", "chiqim"]) if balance > 0 else "kirim"
        if op == "kirim":
            amt = random.randint(lo // 4, hi // 2)
            balance += amt
            parts.append(f"{amt} so'm kirim bo'ldi")
        else:
            amt = random.randint(1, balance)
            balance -= amt
            parts.append(f"{amt} so'm sarflandi")
    text = ", ".join(parts) + ". Hisobda hozir qancha so'm qoldi?"
    return text, balance


def ex_add_sub_missing_term(grade):
    # x + b = c ko'rinishidagi tenglama emas, balki so'z shaklidagi "noma'lum
    # hadni topish" - 8-9 sinf uchun mantiqiy fikrlashni talab qiladi
    lo, hi = _rng(grade, None, (100, 900), (300, 5000))
    result = random.randint(lo, hi)
    known = random.randint(1, result - 1)
    unknown = result - known
    return f"Ikki sonning yig'indisi {result}. Ulardan biri {known} bo'lsa, ikkinchisi nechaga teng?", unknown


GEN_ADD_SUB = [
    (ex_add_sub_plain, ALL_TIERS),
    (ex_add_sub_shop, {"easy"}),
    (ex_add_sub_two_people, {"easy"}),
    (ex_add_sub_bus, {"easy"}),
    (ex_add_sub_three_terms, ALL_TIERS),
    (ex_add_sub_balance, MH_TIERS),
    (ex_add_sub_missing_term, MH_TIERS),
]


# ============================================================
# ---------- mul_div ----------
# ============================================================
def ex_mul_div_plain(grade):
    lo, hi = _rng(grade, (2, 10), (12, 40), (20, 80))
    a, b = random.randint(lo, hi), random.randint(2, 12)
    op = random.choice(["*", "/"])
    if op == "*":
        return f"{a} Г— {b}", a * b
    return f"{a*b} Г· {b}", a


def ex_mul_div_boxes(grade):
    lo, hi = _rng(grade, (2, 9), None, None)
    a, b = random.randint(lo, hi), random.randint(2, 12)
    return f"Har bir qutida {a} ta olma bor. {b} ta quti bo'lsa, jami nechta olma bo'ladi?", a * b


def ex_mul_div_share(grade):
    lo, hi = _rng(grade, (2, 9), None, None)
    a, b = random.randint(lo, hi), random.randint(2, 9)
    total = a * b
    item = random.choice(ITEMS_POOL)
    return f"{total} ta {item}ni {b} ta bolaga teng bo'lib berildi. Har biriga nechtadan tegadi?", a


def ex_mul_div_price(grade):
    lo, hi = _rng(grade, (2, 8), (5, 25), (10, 60))
    price = random.randint(lo, hi) * 1000
    count = random.randint(2, 15)
    return f"1 ta kitob narxi {price} so'm. {count} ta kitob uchun jami qancha to'lash kerak?", price * count


def ex_mul_div_combo(grade):
    c = random.randint(2, 9)
    lo, hi = _rng(grade, (2, 6), (5, 15), (10, 25))
    a = random.randint(lo, hi) * c
    b = random.randint(2, 12)
    return f"({a} Г— {b}) Г· {c}", (a * b) // c


def ex_mul_div_order_ops(grade):
    # Amallar tartibi (avval ko'paytirish/bo'lish, keyin qo'shish/ayirish) -
    # 8-9 sinf uchun muhim algebraik ko'nikma
    lo, hi = _rng(grade, None, (2, 20), (5, 50))
    a = random.randint(lo, hi)
    b = random.randint(2, 12)
    c = random.randint(lo, hi)
    d = random.randint(2, 12)
    op_mid = random.choice(["+", "-"])
    mul1 = a * b
    mul2 = c * d
    if op_mid == "-":
        # manfiy natija chiqmasligi uchun kattasini oldinga qo'yamiz
        if mul1 < mul2:
            a, b, c, d = c, d, a, b
            mul1, mul2 = mul2, mul1
        result = mul1 - mul2
    else:
        result = mul1 + mul2
    return f"{a} Г— {b} {op_mid} {c} Г— {d} = ? (amallar tartibiga rioya qiling)", result


def ex_mul_div_distributive(grade):
    # Taqsimot qonuni: a Г— (b + c) = aГ—b + aГ—c
    lo_a, hi_a = _rng(grade, None, (3, 15), (5, 30))
    a = random.randint(lo_a, hi_a)
    b = random.randint(2, 20)
    c = random.randint(2, 20)
    return f"Taqsimot qonunidan foydalanib hisoblang: {a} Г— ({b} + {c}) = ?", a * (b + c)


def ex_mul_div_two_step(grade):
    # Ikki bosqichli real hayotiy masala (10-11 sinf uchun kattaroq sonlar)
    lo, hi = _rng(grade, None, None, (10, 60))
    workers = random.randint(lo, hi)
    days = random.randint(3, 20)
    rate = random.randint(2, 9)
    total = workers * days * rate
    return f"{workers} ishchi har biri kuniga {rate} ta detal ishlab chiqaradi. {days} kunda ular jami nechta detal ishlab chiqaradi?", total


GEN_MUL_DIV = [
    (ex_mul_div_plain, ALL_TIERS),
    (ex_mul_div_boxes, {"easy"}),
    (ex_mul_div_share, {"easy"}),
    (ex_mul_div_price, ALL_TIERS),
    (ex_mul_div_combo, ALL_TIERS),
    (ex_mul_div_order_ops, MH_TIERS),
    (ex_mul_div_distributive, MH_TIERS),
    (ex_mul_div_two_step, H_ONLY),
]


# ============================================================
# ---------- percent ----------
# ============================================================
# MUHIM: pct * base har doim 100 ga qoldiqsiz bo'linishi kerak. Shuning uchun
# base doim 20 ga karrali qilib tanlanadi - bu {5,10,15,20,25,50,75} kabi
# foizlarning barchasi uchun aniq (butun) natija kafolatlaydi.
def _percent_base(k_lo, k_hi):
    return 20 * random.randint(k_lo, k_hi)


def ex_percent_discount(grade):
    base = _percent_base(*_rng(grade, (1, 7), (15, 60), (60, 300)))
    pct = random.choice([5, 10, 15, 20, 25, 50])
    return f"{base} so'mlik narsaga {pct}% chegirma qilindi. Chegirma summasi qancha so'm?", base * pct // 100


def ex_percent_increase(grade):
    base = _percent_base(*_rng(grade, (2, 10), (20, 50), (60, 300)))
    pct = random.choice([5, 10, 15, 20, 25])
    return f"Mahsulot narxi {base} so'm edi, keyin {pct}% oshdi. Yangi narx qancha so'm?", base + base * pct // 100


def ex_percent_decrease(grade):
    base = _percent_base(*_rng(grade, (2, 8), (15, 50), (60, 250)))
    pct = random.choice([5, 10, 20, 25])
    return f"Mahsulot narxi {base} so'm edi, keyin {pct}% arzonlashdi. Yangi narx qancha so'm?", base - base * pct // 100


def ex_percent_direct(grade):
    base = _percent_base(*_rng(grade, (1, 7), (10, 35), (40, 150)))
    pct = random.choice([5, 10, 15, 20, 25, 50, 75])
    return f"{base} ning {pct}% i nechaga teng?", base * pct // 100


def ex_percent_of_students(grade):
    total = _percent_base(*_rng(grade, (1, 5), (4, 10), (8, 20)))
    pct = random.choice([10, 20, 25, 50])
    return f"Sinfda {total} ta o'quvchi bor. Ularning {pct}% i qiz bo'lsa, nechta qiz bor?", total * pct // 100


def ex_percent_successive(grade):
    # Ketma-ket ikki marta foiz o'zgarishi - 8-9 sinf uchun klassik masala
    # (natija boshlang'ich foizlarning oddiy yig'indisiga TENG BO'LMASLIGINI tushunish muhim)
    base = _percent_base(*_rng(grade, None, (10, 40), (30, 150)))
    pct1 = random.choice([10, 20, 25, 50])
    dir1 = random.choice(["oshdi", "tushdi"])
    step1 = base + base * pct1 // 100 if dir1 == "oshdi" else base - base * pct1 // 100
    pct2 = random.choice([10, 20])
    dir2 = random.choice(["oshdi", "tushdi"])
    step2 = step1 + step1 * pct2 // 100 if dir2 == "oshdi" else step1 - step1 * pct2 // 100
    return (
        f"Mahsulot narxi {base} so'm edi. Avval {pct1}% ga {dir1}, so'ngra yangi narx yana "
        f"{pct2}% ga {dir2}. Yakuniy narx qancha so'm bo'ladi?",
        step2,
    )


def ex_percent_reverse(grade):
    # A soni B sonining necha foizini tashkil qiladi (teskari masala)
    base = _percent_base(*_rng(grade, None, (5, 20), (10, 50)))
    pct = random.choice([10, 20, 25, 40, 50, 75])
    part = base * pct // 100
    return f"{part} soni {base} sonining necha foizini tashkil qiladi?", pct


def ex_percent_find_whole(grade):
    # Qism va foiz ma'lum, butun sonni topish (teskari masala)
    pct = random.choice([10, 20, 25, 40, 50])
    whole = _percent_base(*_rng(grade, None, (5, 25), (10, 60)))
    part = whole * pct // 100
    return f"Bir sonning {pct}% i {part} ga teng. Shu son nechaga teng?", whole


GEN_PERCENT = [
    (ex_percent_discount, ALL_TIERS),
    (ex_percent_increase, ALL_TIERS),
    (ex_percent_decrease, ALL_TIERS),
    (ex_percent_direct, ALL_TIERS),
    (ex_percent_of_students, {"easy"}),
    (ex_percent_successive, MH_TIERS),
    (ex_percent_reverse, MH_TIERS),
    (ex_percent_find_whole, MH_TIERS),
]


# ============================================================
# ---------- fraction ----------
# ============================================================
def ex_fraction_simple(grade):
    denom = random.choice(_rng(grade, [2, 3, 4], [3, 4, 5, 6, 8], [6, 8, 9, 10, 12]))
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


def ex_fraction_common_denom_add(grade):
    # Turli maxrajli kasrlarni umumiy maxrajga keltirib qo'shish - 8-9 sinf
    # uchun asosiy ko'nikma. Natija butun son chiqishi uchun maxsus tanlanadi.
    d1 = random.choice([2, 3, 4, 5])
    d2 = random.choice([2, 3, 4, 5])
    while d2 == d1:
        d2 = random.choice([2, 3, 4, 5])
    lcm = d1 * d2 // math.gcd(d1, d2)
    n1 = random.randint(1, d1 - 1)
    n2 = random.randint(1, d2 - 1)
    k = random.randint(2, 8)
    total = lcm * k
    part = (n1 * total // d1) + (n2 * total // d2)
    return (
        f"{total} sonining {n1}/{d1} qismi bilan {n2}/{d2} qismining yig'indisi nechaga teng?",
        part,
    )


def ex_fraction_compare(grade):
    # Ikki kasrni taqqoslash - qaysi biri katta (mantiqiy fikrlash, hisoblashsiz emas).
    # DIQQAT: ba'zi kichik maxrajlarda (masalan 2) faqat bitta imkoniyat bo'lgani
    # uchun cheksiz siklga tushib qolmaslik uchun urinishlar soni cheklangan va
    # muvaffaqiyatsiz bo'lsa butunlay yangi juftlik tanlanadi.
    for _ in range(30):
        d1, d2 = random.sample([2, 3, 4, 5, 6, 8, 9, 10], 2)
        n1 = random.randint(1, d1 - 1)
        n2 = random.randint(1, d2 - 1)
        val1 = n1 / d1
        val2 = n2 / d2
        if abs(val1 - val2) >= 0.02:
            bigger = 1 if val1 > val2 else 2
            return f"{n1}/{d1} va {n2}/{d2} kasrlaridan qaysi biri katta? (1-chi bo'lsa 1, 2-chi bo'lsa 2 deb yozing)", bigger
    # Ehtiyot chorasi (amalda deyarli hech qachon bu yerga yetib kelmaydi)
    return "1/2 va 1/3 kasrlaridan qaysi biri katta? (1-chi bo'lsa 1, 2-chi bo'lsa 2 deb yozing)", 1


GEN_FRACTION = [
    (ex_fraction_simple, ALL_TIERS),
    (ex_fraction_nested, ALL_TIERS),
    (ex_fraction_remaining, {"easy"}),
    (ex_fraction_money, {"easy"}),
    (ex_fraction_common_denom_add, MH_TIERS),
    (ex_fraction_compare, MH_TIERS),
]


# ============================================================
# ---------- power ----------
# ============================================================
def ex_power_square(grade):
    lo, hi = _rng(grade, (2, 10), (11, 25), (20, 40))
    a = random.randint(lo, hi)
    return f"{a}ВІ = ?", a * a


def ex_power_cube(grade):
    lo, hi = _rng(grade, (2, 6), (5, 12), (8, 15))
    a = random.randint(lo, hi)
    return f"{a}Ві = ?", a ** 3


def ex_power_sum_then_power(grade):
    a, b = random.randint(2, 9), random.randint(1, 9)
    p = 2 if grade == "easy" else random.choice([2, 3])
    return f"({a}+{b})^{p} = ?", (a + b) ** p


def ex_power_diff_then_square(grade):
    a = random.randint(5, 20)
    b = random.randint(1, a - 1)
    return f"({a}в€’{b})ВІ = ?", (a - b) ** 2


def ex_power_law_mul(grade):
    # aбµђ Г— aвЃї = aбµђвЃєвЃї - daraja qonuni (8-9 sinf algebra dasturi)
    base = random.randint(2, 5)
    m = random.randint(1, 4)
    n = random.randint(1, 4)
    return f"{base}^{m} Г— {base}^{n} ni {base} ning bitta darajasi ko'rinishida yozsangiz, daraja ko'rsatkichi nechaga teng?", m + n


def ex_power_law_div(grade):
    # aбµђ Г· aвЃї = aбµђвЃ»вЃї (m > n bo'lishi shart)
    base = random.randint(2, 5)
    n = random.randint(1, 4)
    m = random.randint(n + 1, n + 5)
    return f"{base}^{m} Г· {base}^{n} ni {base} ning bitta darajasi ko'rinishida yozsangiz, daraja ko'rsatkichi nechaga teng?", m - n


def ex_power_law_value(grade):
    # Daraja qonunini qo'llab, YAKUNIY SON qiymatini hisoblash (kuchliroq)
    base = random.choice([2, 3])
    m = random.randint(1, 3)
    n = random.randint(1, 3)
    return f"{base}^{m} Г— {base}^{n} necha songa teng? (avval daraja qonunini qo'llang)", base ** (m + n)


GEN_POWER = [
    (ex_power_square, ALL_TIERS),
    (ex_power_cube, ALL_TIERS),
    (ex_power_sum_then_power, ALL_TIERS),
    (ex_power_diff_then_square, ALL_TIERS),
    (ex_power_law_mul, MH_TIERS),
    (ex_power_law_div, MH_TIERS),
    (ex_power_law_value, MH_TIERS),
]


# ============================================================
# ---------- sqrt ----------
# ============================================================
PERFECT_SQUARES_EASY = [4, 9, 16, 25, 36, 49, 64, 81, 100]
PERFECT_SQUARES_ALL = [n * n for n in range(2, 26)]


def ex_sqrt_direct(grade):
    pool = _rng(grade, PERFECT_SQUARES_EASY, PERFECT_SQUARES_ALL[:20], PERFECT_SQUARES_ALL)
    a = random.choice(pool)
    return f"в€љ{a} = ?", int(math.sqrt(a))


def ex_sqrt_from_square(grade):
    lo, hi = _rng(grade, (5, 12), (13, 22), (18, 30))
    base = random.randint(lo, hi)
    return f"в€љ{base*base} = ?", base


def ex_sqrt_area_to_side(grade):
    lo, hi = _rng(grade, (3, 10), (10, 20), (15, 28))
    side = random.randint(lo, hi)
    area = side * side
    return f"Yuzasi {area} bo'lgan kvadratning tomoni nechaga teng?", side


def ex_sqrt_product(grade):
    lo, hi = _rng(grade, (2, 6), (4, 12), (8, 18))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"в€љ{a*a} Г— в€љ{b*b} nechaga teng?", a * b


def ex_sqrt_estimate(grade):
    # To'liq kvadrat bo'lmagan son ikkita ketma-ket butun son orasida qayerda
    # joylashganini aniqlash - 8-9 sinf uchun mantiqiy baholash ko'nikmasi
    n = random.randint(4, 29)
    low_root = n
    n_squared_area = random.randint(low_root * low_root + 1, (low_root + 1) * (low_root + 1) - 1)
    return f"в€љ{n_squared_area} soni qaysi ikkita ketma-ket butun son orasida joylashgan? Kichigini yozing.", low_root


def ex_sqrt_simplify(grade):
    # в€љ(aВІГ—b) = aв€љb ko'rinishida soddalashtirish (b - kvadratsiz son) -
    # 10-11 sinf uchun kuchliroq ildiz bilan ishlash ko'nikmasi
    a = random.randint(2, 10)
    b = random.choice([2, 3, 5, 6, 7, 10, 11, 13, 14, 15])
    n = a * a * b
    return f"в€љ{n} sonini aв€љ{b} ko'rinishida soddalashtiring. a nechaga teng?", a


GEN_SQRT = [
    (ex_sqrt_direct, ALL_TIERS),
    (ex_sqrt_from_square, ALL_TIERS),
    (ex_sqrt_area_to_side, ALL_TIERS),
    (ex_sqrt_product, ALL_TIERS),
    (ex_sqrt_estimate, MH_TIERS),
    (ex_sqrt_simplify, H_ONLY),
]


# ============================================================
# ---------- linear_eq ----------
# ============================================================
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
    x = random.randint(2, 20)
    a = random.randint(3, 12)
    c = random.randint(1, 11)
    while c == a:
        c = random.randint(1, 11)
    b = random.randint(1, 30)
    d = a * x + b - c * x
    sign_b = f"+ {b}" if b >= 0 else f"- {abs(b)}"
    sign_d = f"+ {d}" if d >= 0 else f"- {abs(d)}"
    return f"{a}x {sign_b} = {c}x {sign_d}, x = ?", x


def ex_linear_minus(grade):
    x = random.randint(2, 25)
    a = random.randint(3, 12)
    b = random.randint(1, 30)
    c = a * x - b
    return f"{a}x в€’ {b} = {c}, x = ?", x


def ex_linear_parentheses(grade):
    # a(x + b) = c ko'rinishi - qavsni ochish ko'nikmasini talab qiladi
    x = random.randint(2, 20)
    a = random.randint(2, 9)
    b = random.randint(1, 15)
    c = a * (x + b)
    return f"{a}(x + {b}) = {c}, x = ?", x


def ex_linear_parentheses_both(grade):
    # a(x + b) = c(x - d) ko'rinishi (10-11 sinf uchun kuchliroq)
    x = random.randint(2, 15)
    a = random.randint(2, 8)
    c = random.randint(1, 7)
    while c == a:
        c = random.randint(1, 7)
    b = random.randint(1, 15)
    # a(x+b) = cx + ad  =>  ax + ab = cx + cd_target ... hisoblaymiz:
    # a*x + a*b = c*x + rhs_const  =>  rhs_const = a*x + a*b - c*x
    rhs_const = a * x + a * b - c * x
    sign_rhs = f"+ {rhs_const}" if rhs_const >= 0 else f"- {abs(rhs_const)}"
    return f"{a}(x + {b}) = {c}x {sign_rhs}, x = ?", x


def ex_linear_two_step_word(grade):
    # So'z masalasi - chiziqli tenglama shaklida yechish talab qilinadi
    x = random.randint(3, 30)
    a = random.randint(2, 8)
    b = random.randint(5, 50)
    total = a * x + b
    return (
        f"Bir guruh o'quvchi {a} ta avtobusga teng bo'lib chiqdi, har biriga x kishidan "
        f"o'tirdi va yana {b} kishi piyoda ketdi. Agar jami {total} kishi bo'lsa, "
        f"har bir avtobusda nechta kishi bor ({a}x + {b} = {total} tenglamasidan x ni toping)?",
        x,
    )


GEN_LINEAR_EQ = [
    (ex_linear_simple, {"easy"}),
    (ex_linear_ax_b, {"easy"}),
    (ex_linear_both_sides, MH_TIERS),
    (ex_linear_minus, MH_TIERS),
    (ex_linear_parentheses, MH_TIERS),
    (ex_linear_parentheses_both, H_ONLY),
    (ex_linear_two_step_word, MH_TIERS),
]


# ============================================================
# ---------- quad_eq ----------
# ============================================================
def ex_quad_pure(grade):
    lo, hi = _rng(grade, (2, 12), (5, 18), (10, 25))
    x = random.randint(lo, hi)
    return f"xВІ = {x*x} (x > 0), x = ?", x


def _quad_text(r1, r2):
    b, c = -(r1 + r2), r1 * r2
    sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
    sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
    return f"xВІ {sign_b} {sign_c} = 0", b, c


def ex_quad_sum(grade):
    r1, r2 = random.randint(1, 15), random.randint(1, 15)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlarining yig'indisi?", r1 + r2


def ex_quad_product(grade):
    r1, r2 = random.randint(1, 12), random.randint(1, 12)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlarining ko'paytmasi?", r1 * r2


def ex_quad_largest(grade):
    r1, r2 = random.randint(1, 15), random.randint(1, 15)
    while r1 == r2:
        r2 = random.randint(1, 15)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglamaning eng katta ildizi?", max(r1, r2)


def ex_quad_discriminant(grade):
    # Diskriminantni hisoblash - D = bВІ - 4ac (8-9 sinf uchun asosiy ko'nikma)
    a = random.randint(1, 3)
    r1, r2 = random.randint(1, 10), random.randint(1, 10)
    b = -a * (r1 + r2)
    c = a * r1 * r2
    d = b * b - 4 * a * c
    sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
    sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
    coef_a = f"{a}xВІ" if a != 1 else "xВІ"
    return f"{coef_a} {sign_b} {sign_c} = 0 tenglamaning diskriminanti (D = bВІ в€’ 4ac) nechaga teng?", d


def ex_quad_sum_of_squares(grade):
    # x1ВІ + x2ВІ = (x1+x2)ВІ - 2*x1*x2 ayniyati (kuchliroq, hard darajaga mos)
    r1, r2 = random.randint(1, 10), random.randint(1, 10)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlari x1 va x2 bo'lsa, x1ВІ + x2ВІ nechaga teng?", r1 * r1 + r2 * r2


GEN_QUAD_EQ = [
    (ex_quad_pure, MH_TIERS),
    (ex_quad_sum, MH_TIERS),
    (ex_quad_product, MH_TIERS),
    (ex_quad_largest, MH_TIERS),
    (ex_quad_discriminant, MH_TIERS),
    (ex_quad_sum_of_squares, H_ONLY),
]


# ============================================================
# ---------- system_eq (Tenglamalar sistemasi) ----------
# ============================================================
def _make_system(x0, y0):
    """
    Berilgan (x0, y0) yechim uchun tasodifiy koeffitsientli 2 ta chiziqli
    tenglama yaratadi va ularning matnini qaytaradi. Determinant (a*e - b*d)
    nolga teng emasligi ta'minlanadi - shu bois sistema YAGONA yechimga ega.
    """
    while True:
        a, b = random.randint(1, 6), random.randint(1, 6)
        d, e = random.randint(1, 6), random.randint(1, 6)
        if a * e - b * d != 0:
            break
    c = a * x0 + b * y0
    f = d * x0 + e * y0
    line1 = f"{a}x + {b}y = {c}"
    line2 = f"{d}x + {e}y = {f}"
    return line1, line2


def ex_system_find_x(grade):
    x0 = random.randint(-12, 12)
    y0 = random.randint(-12, 12)
    l1, l2 = _make_system(x0, y0)
    return f"{l1}\n{l2}\nBerilgan tenglamalar sistemasidan x ning qiymatini toping.", x0


def ex_system_find_y(grade):
    x0 = random.randint(-12, 12)
    y0 = random.randint(-12, 12)
    l1, l2 = _make_system(x0, y0)
    return f"{l1}\n{l2}\nBerilgan tenglamalar sistemasidan y ning qiymatini toping.", y0


def ex_system_find_sum(grade):
    x0 = random.randint(-10, 10)
    y0 = random.randint(-10, 10)
    l1, l2 = _make_system(x0, y0)
    return f"{l1}\n{l2}\nBerilgan tenglamalar sistemasi yechimida x + y nechaga teng?", x0 + y0


def ex_system_positive_only(grade):
    # Faqat musbat yechimli sistema - hard darajada kattaroq sonlar bilan
    x0 = random.randint(1, 15)
    y0 = random.randint(1, 15)
    l1, l2 = _make_system(x0, y0)
    return f"{l1}\n{l2}\nBerilgan tenglamalar sistemasidan x ning qiymatini toping. (x, y > 0)", x0


def ex_system_word_problem(grade):
    # So'z masalasi ko'rinishida - ikki noma'lumli sistema
    x0 = random.randint(2, 20)
    y0 = random.randint(2, 20)
    total = x0 + y0
    diff_coef = random.randint(2, 5)
    total2 = diff_coef * x0 + y0
    return (
        f"Ikki sonning yig'indisi {total} ga teng. Agar birinchi sonni {diff_coef} "
        f"marta oshirib ikkinchisiga qo'shsak, {total2} hosil bo'ladi. Birinchi son "
        f"(x) nechaga teng?\n(x + y = {total},  {diff_coef}x + y = {total2})",
        x0,
    )


GEN_SYSTEM_EQ = [
    (ex_system_find_x, MH_TIERS),
    (ex_system_find_y, MH_TIERS),
    (ex_system_find_sum, MH_TIERS),
    (ex_system_positive_only, H_ONLY),
    (ex_system_word_problem, MH_TIERS),
]


# ============================================================
# ---------- triangle ----------
# ============================================================
def ex_triangle_area(grade):
    lo, hi = _rng(grade, (4, 12), (10, 25), (20, 45))
    base, height = random.randint(lo, hi), random.randint(lo, hi)
    if (base * height) % 2 != 0:
        height += 1
    return f"Asosi {base}, balandligi {height} bo'lgan uchburchak yuzasi?", base * height // 2


def ex_triangle_perimeter(grade):
    lo, hi = _rng(grade, (5, 15), (12, 30), (25, 60))
    a, b, c = random.randint(lo, hi), random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a}, {b}, {c} bo'lgan uchburchakning perimetri?", a + b + c


def ex_triangle_missing_side(grade):
    lo, hi = _rng(grade, (5, 15), (10, 25), (20, 45))
    a, b, c = random.randint(lo, hi), random.randint(lo, hi), random.randint(lo, hi)
    p = a + b + c
    return f"Uchburchak perimetri {p}. Ikki tomoni {a} va {b} bo'lsa, uchinchi tomoni nechaga teng?", c


def ex_triangle_right_pythagorean(grade):
    # Pifagor teoremasi - to'g'ri burchakli uchburchak (8-9 sinf geometriya)
    triples = [(3, 4, 5), (6, 8, 10), (5, 12, 13), (9, 12, 15), (8, 15, 17), (7, 24, 25), (20, 21, 29)]
    a, b, c = random.choice(triples)
    mult = random.randint(1, 3)
    a, b, c = a * mult, b * mult, c * mult
    if random.random() < 0.5:
        return f"To'g'ri burchakli uchburchakda katetlar {a} va {b}. Gipotenuza nechaga teng? (Pifagor teoremasi)", c
    return f"To'g'ri burchakli uchburchakda gipotenuza {c}, bir kateti {a}. Ikkinchi katet nechaga teng?", b


def ex_triangle_height_from_area(grade):
    lo, hi = _rng(grade, None, (8, 20), (15, 35))
    base = random.randint(lo, hi)
    height = random.randint(lo, hi)
    area = base * height // 2 if (base * height) % 2 == 0 else base * (height + 1) // 2
    height = height if (base * height) % 2 == 0 else height + 1
    return f"Uchburchak yuzasi {area}, asosi {base}. Balandligi nechaga teng?", height


GEN_TRIANGLE = [
    (ex_triangle_area, ALL_TIERS),
    (ex_triangle_perimeter, ALL_TIERS),
    (ex_triangle_missing_side, ALL_TIERS),
    (ex_triangle_right_pythagorean, MH_TIERS),
    (ex_triangle_height_from_area, MH_TIERS),
]


# ============================================================
# ---------- rectangle ----------
# ============================================================
def ex_rect_area(grade):
    lo, hi = _rng(grade, (2, 15), (10, 30), (20, 55))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a} va {b} bo'lgan to'rtburchak yuzasi?", a * b


def ex_rect_perimeter(grade):
    lo, hi = _rng(grade, (2, 15), (10, 30), (20, 55))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"Tomonlari {a} va {b} bo'lgan to'rtburchak perimetri?", 2 * (a + b)


def ex_rect_missing_side(grade):
    lo, hi = _rng(grade, (3, 12), (8, 25), (15, 40))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    area = a * b
    return f"Yuzasi {area}, bir tomoni {a} bo'lgan to'rtburchakning ikkinchi tomonini toping.", b


def ex_rect_diagonal(grade):
    # Diagonal - Pifagor teoremasi orqali (butun sonli uchliklar bilan)
    triples = [(3, 4, 5), (6, 8, 10), (5, 12, 13), (9, 12, 15), (8, 15, 17)]
    a, b, d = random.choice(triples)
    mult = random.randint(1, 3)
    a, b, d = a * mult, b * mult, d * mult
    return f"To'g'ri to'rtburchak tomonlari {a} va {b}. Uning diagonali nechaga teng?", d


def ex_rect_perimeter_from_area_side(grade):
    lo, hi = _rng(grade, None, (8, 22), (15, 35))
    a = random.randint(lo, hi)
    b = random.randint(lo, hi)
    area = a * b
    return f"To'g'ri to'rtburchak yuzasi {area}, bir tomoni {a}. Uning perimetrini toping.", 2 * (a + b)


GEN_RECTANGLE = [
    (ex_rect_area, ALL_TIERS),
    (ex_rect_perimeter, ALL_TIERS),
    (ex_rect_missing_side, ALL_TIERS),
    (ex_rect_diagonal, MH_TIERS),
    (ex_rect_perimeter_from_area_side, MH_TIERS),
]


# ============================================================
# ---------- circle ----------
# ============================================================
def _circle_radius(grade):
    k = random.randint(*_rng(grade, (1, 10), (5, 25), (15, 40)))
    return 7 * k


def ex_circle_area(grade):
    r = _circle_radius(grade)
    return f"Radiusi {r} bo'lgan doira yuzasi (ПЂ=22/7 deb oling)?", int(22 * r * r / 7)


def ex_circle_circumference(grade):
    r = _circle_radius(grade)
    return f"Radiusi {r} bo'lgan doiraning aylana uzunligi (ПЂ=22/7 deb oling)?", int(2 * 22 * r / 7)


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
    return f"Yuzasi {area} bo'lgan doiraning radiusi nechaga teng? (ПЂ=22/7 deb oling)", r


def ex_circle_radius_from_circumference(grade):
    r = _circle_radius(grade)
    circ = int(2 * 22 * r / 7)
    return f"Aylana uzunligi {circ} bo'lgan doiraning radiusi nechaga teng? (ПЂ=22/7 deb oling)", r


GEN_CIRCLE = [
    (ex_circle_area, ALL_TIERS),
    (ex_circle_circumference, ALL_TIERS),
    (ex_circle_radius_from_diameter, {"easy"}),
    (ex_circle_diameter_from_radius, {"easy"}),
    (ex_circle_radius_from_area, MH_TIERS),
    (ex_circle_radius_from_circumference, MH_TIERS),
]


# ============================================================
# ---------- ratio ----------
# ============================================================
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


def ex_ratio_three_part(grade):
    # Uch qismli nisbat - 8-9 sinf uchun kuchliroq
    p1, p2, p3 = random.randint(1, 6), random.randint(1, 6), random.randint(1, 6)
    k = random.randint(2, 10)
    total = (p1 + p2 + p3) * k
    largest = max(p1, p2, p3) * k
    return f"{total} sonini {p1}:{p2}:{p3} nisbatda uchga bo'lganda eng katta qism nechaga teng?", largest


def ex_ratio_scale(grade):
    # Masshtab / xarita masalasi
    scale = random.choice([50, 100, 200, 500])
    map_cm = random.randint(2, 15)
    real = map_cm * scale
    return f"Xaritada masshtab 1:{scale}. Xaritadagi {map_cm} sm masofa haqiqatda necha sm ga teng?", real


GEN_RATIO = [
    (ex_ratio_proportion, ALL_TIERS),
    (ex_ratio_split, ALL_TIERS),
    (ex_ratio_students, {"easy"}),
    (ex_ratio_three_part, MH_TIERS),
    (ex_ratio_scale, MH_TIERS),
]


# ============================================================
# ---------- average ----------
# ============================================================
def ex_average_direct(grade):
    lo, hi = _rng(grade, (1, 20), (10, 60), (20, 100))
    nums = [random.randint(lo, hi) for _ in range(3)]
    while sum(nums) % 3 != 0:
        nums = [random.randint(lo, hi) for _ in range(3)]
    return f"{', '.join(map(str, nums))} sonlarining o'rtacha qiymati?", sum(nums) // 3


def ex_average_sum_from_avg(grade):
    avg = random.randint(10, 70)
    n = random.choice([3, 4, 5, 6])
    return f"{n} ta sonning o'rtacha qiymati {avg}. Bu sonlarning yig'indisi nechaga teng?", avg * n


def ex_average_score(grade):
    n = random.choice([3, 4, 5])
    lo, hi = _rng(grade, (2, 5), (50, 90), (60, 100))
    scores = [random.randint(lo, hi) for _ in range(n)]
    while sum(scores) % n != 0:
        scores = [random.randint(lo, hi) for _ in range(n)]
    return f"O'quvchi {n} ta nazoratdan {', '.join(map(str, scores))} ball oldi. O'rtacha bahosi nechaga teng?", sum(scores) // n


def ex_average_find_missing(grade):
    # (n-1) ta son va kerakli o'rtacha ma'lum, oxirgi noma'lum sonni topish -
    # 8-9 sinf uchun teskari fikrlash talab qiladi
    n = random.choice([3, 4, 5])
    lo, hi = _rng(grade, None, (10, 60), (20, 100))
    known = [random.randint(lo, hi) for _ in range(n - 1)]
    target_avg = random.randint(lo, hi)
    unknown = target_avg * n - sum(known)
    if unknown < 0:
        unknown = abs(unknown) + 5
        target_avg = (sum(known) + unknown) // n
        # aniqlik uchun o'rtacha butun chiqishini kafolatlaymiz
        while (sum(known) + unknown) % n != 0:
            unknown += 1
        target_avg = (sum(known) + unknown) // n
    return (
        f"{', '.join(map(str, known))} sonlariga yana bitta son qo'shilib, {n} ta sonning "
        f"o'rtachasi {target_avg} ga teng bo'lishi kerak. Qo'shiladigan son nechaga teng?",
        unknown,
    )


GEN_AVERAGE = [
    (ex_average_direct, ALL_TIERS),
    (ex_average_sum_from_avg, ALL_TIERS),
    (ex_average_score, {"easy"}),
    (ex_average_find_missing, MH_TIERS),
]


# ============================================================
# ---------- negative ----------
# ============================================================
def ex_negative_add(grade):
    lo, hi = _rng(grade, (-20, -1), (-60, -1), (-99, -1))
    a, b = random.randint(lo, hi), random.randint(1, abs(lo))
    op = random.choice(["+", "-"])
    return f"({a}) {op} {b}", (a + b if op == "+" else a - b)


def ex_negative_mul(grade):
    lo, hi = _rng(grade, (-12, -2), (-18, -2), (-25, -2))
    a, b = random.randint(lo, hi), random.randint(2, 12)
    if random.random() < 0.5:
        b = -b
    return f"({a}) Г— ({b})" if b < 0 else f"({a}) Г— {b}", a * b


def ex_negative_both(grade):
    lo, hi = _rng(grade, (-30, -1), (-60, -1), (-99, -1))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    op = random.choice(["+", "-"])
    return f"({a}) {op} ({b})", (a + b if op == "+" else a - b)


def ex_negative_temperature(grade):
    start = random.randint(-15, 5)
    delta = random.randint(1, 20)
    op = random.choice(["ko'tarildi", "pasaydi"])
    result = start + delta if op == "ko'tarildi" else start - delta
    return f"Havo harorati {start}В°C edi. Kechqurun {delta}В° ga {op}. Hozir harorat necha daraja?", result


def ex_negative_chain(grade):
    # Ko'p qadamli manfiy sonlar zanjiri - 8-9 sinf uchun kuchliroq
    start = random.randint(-30, 30)
    n_ops = random.choice([3, 4])
    val = start
    parts = [str(start)]
    for _ in range(n_ops):
        op = random.choice(["+", "-"])
        num = random.randint(1, 25)
        parts.append(f"{op} {num}")
        val = val + num if op == "+" else val - num
    return " ".join(parts) + " = ?", val


def ex_negative_mul_chain(grade):
    # Bir nechta manfiy/musbat sonlarning ko'paytmasi - belgi qoidasini
    # tushunishni talab qiladi (juft/toq manfiylar soni)
    n_factors = random.choice([3, 4])
    factors = []
    result = 1
    for _ in range(n_factors):
        sign = random.choice([1, -1])
        val = sign * random.randint(1, 6)
        factors.append(val)
        result *= val
    text = " Г— ".join(f"({f})" if f < 0 else str(f) for f in factors)
    return f"{text} = ?", result


GEN_NEGATIVE = [
    (ex_negative_add, ALL_TIERS),
    (ex_negative_mul, ALL_TIERS),
    (ex_negative_both, ALL_TIERS),
    (ex_negative_temperature, {"easy"}),
    (ex_negative_chain, MH_TIERS),
    (ex_negative_mul_chain, MH_TIERS),
]


# ============================================================
# ---------- speed ----------
# ============================================================
def ex_speed_distance(grade):
    lo_s, hi_s = _rng(grade, (10, 30), (40, 90), (70, 160))
    speed, time = random.randint(lo_s, hi_s), random.randint(1, 8)
    return f"Tezligi {speed} km/soat bo'lgan mashina {time} soatda necha km yo'l bosadi?", speed * time


def ex_speed_find_speed(grade):
    time = random.randint(2, 8)
    lo_s, hi_s = _rng(grade, (10, 40), (40, 100), (60, 160))
    speed = random.randint(lo_s, hi_s)
    distance = speed * time
    return f"Mashina {distance} km yo'lni {time} soatda bosib o'tdi. Uning tezligi necha km/soat?", speed


def ex_speed_find_time(grade):
    lo_s, hi_s = _rng(grade, (10, 40), (40, 100), (60, 160))
    speed = random.randint(lo_s, hi_s)
    time = random.randint(1, 8)
    distance = speed * time
    return f"{distance} km masofani {speed} km/soat tezlik bilan necha soatda bosib o'tish mumkin?", time


def ex_speed_meeting(grade):
    # Qarama-qarshi harakat - ikki obyekt bir-biriga tomon yuradi (8-9 sinf klassik masalasi)
    v1 = random.randint(40, 90)
    v2 = random.randint(40, 90)
    time = random.randint(2, 6)
    distance = (v1 + v2) * time
    return (
        f"Ikki shahar orasidagi masofa {distance} km. Ikkita mashina bir vaqtda bir-biriga "
        f"tomon yo'lga chiqdi: biri {v1} km/soat, ikkinchisi {v2} km/soat tezlikda. "
        f"Ular necha soatdan keyin uchrashadi?",
        time,
    )


def ex_speed_catchup(grade):
    # Quvib o'tish masalasi (bir yo'nalishda, biri oldinda)
    v_slow = random.randint(30, 60)
    v_fast = v_slow + random.randint(10, 40)
    time = random.randint(2, 6)
    head_start = v_slow * time  # sekin ketayotgan qancha oldinda
    catchup_time = head_start // (v_fast - v_slow)
    while head_start % (v_fast - v_slow) != 0:
        time += 1
        head_start = v_slow * time
        catchup_time = head_start // (v_fast - v_slow)
    return (
        f"Sekin mashina {v_slow} km/soat tezlikda {time} soat oldin yo'lga chiqqan. "
        f"Endi undan {head_start} km orqada turgan tez mashina {v_fast} km/soat tezlikda yo'lga chiqdi. "
        f"Tez mashina sekin mashinani necha soatdan keyin quvib yetadi?",
        catchup_time,
    )


GEN_SPEED = [
    (ex_speed_distance, ALL_TIERS),
    (ex_speed_find_speed, ALL_TIERS),
    (ex_speed_find_time, ALL_TIERS),
    (ex_speed_meeting, MH_TIERS),
    (ex_speed_catchup, H_ONLY),
]


# ============================================================
# ---------- bank_percent ----------
# ============================================================
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
    result = _bank_deposit(grade) // 10
    if result == 0:
        result = 10
    deposit = result * 100 // pct
    return f"Bankka qo'yilgan pulga {pct}% foiz qo'shilganda {result} so'm foiz hosil bo'ldi. Boshlang'ich depozit qancha so'm edi?", deposit


def ex_bank_two_years(grade):
    deposit = _bank_deposit(grade)
    pct = random.choice([2, 4, 5, 8, 10])
    total = deposit + 2 * (deposit * pct // 100)
    return f"{deposit} so'm depozitga har yili {pct}% oddiy foiz qo'shib borilsa, 2 yildan keyin hisobda qancha so'm bo'ladi?", total


def ex_bank_compound(grade):
    # Murakkab (bir-biriga qo'shiladigan) foiz - faqat hard daraja uchun
    deposit = _bank_deposit(grade)
    pct = random.choice([10, 20, 25])  # butun natija chiqishi uchun "toza" foizlar
    year1 = deposit + deposit * pct // 100
    year2 = year1 + year1 * pct // 100
    return f"{deposit} so'm depozitga har yili {pct}% murakkab foiz qo'shilsa (foizga ham foiz qo'shilib boriladi), 2 yildan keyin hisobda qancha so'm bo'ladi?", year2


GEN_BANK_PERCENT = [
    (ex_bank_interest, MH_TIERS),
    (ex_bank_total, MH_TIERS),
    (ex_bank_find_deposit, MH_TIERS),
    (ex_bank_two_years, MH_TIERS),
    (ex_bank_compound, H_ONLY),
]


# ============================================================
# ---------- trig ----------
# ============================================================
TRIG_PERCENT_FACTS = [
    ("sin(0В°)", 0), ("cos(90В°)", 0),
    ("sin(30В°)", 50), ("cos(60В°)", 50),
    ("sin(90В°)", 100), ("cos(0В°)", 100),
]

TRIG_QUESTION_TEMPLATES = [
    "{q} ning qiymati necha foizga teng? (sin(90В°) = 100% deb hisoblang)",
    "{q} nechaga teng, foiz ko'rinishida ayting? (masalan cos(0В°) = 100%)",
    "Agar sin(90В°) = 100% desak, {q} necha foizga teng bo'ladi?",
]


def ex_trig_value(grade):
    q, pct = random.choice(TRIG_PERCENT_FACTS)
    template = random.choice(TRIG_QUESTION_TEMPLATES)
    return template.format(q=q), pct


def ex_trig_identity(grade):
    known = random.choice(["sin", "cos"])
    other = "cos" if known == "sin" else "sin"
    return (
        f"sinВІО± + cosВІО± = 1 ayniyatiga ko'ra, agar {known}ВІО± = 0 bo'lsa, "
        f"{other}ВІО± nechaga teng?",
        1,
    )


def ex_trig_tan(grade):
    angle = random.choice([0, 45])
    ans = 0 if angle == 0 else 1
    return f"tan({angle}В°) qiymatini toping. (0 yoki 1)", ans


def ex_trig_pythagorean(grade):
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


def ex_trig_sum_angles(grade):
    # Uchburchak burchaklari yig'indisi 180В° - trigonometriyaga tayyorgarlik
    a1 = random.randint(20, 90)
    a2 = random.randint(20, 150 - a1)  # a1+a2 <= 150 bo'lgani uchun a3 >= 30 (har doim musbat)
    a3 = 180 - a1 - a2
    return f"Uchburchak burchaklaridan ikkitasi {a1}В° va {a2}В°. Uchinchi burchak nechaga teng?", a3


TRIG_EQUATION_FACTS = [
    ("sin", 0, 0), ("sin", 50, 30), ("sin", 100, 90),
    ("cos", 100, 0), ("cos", 50, 60), ("cos", 0, 90),
    ("tan", 0, 0), ("tan", 100, 45),
]


def ex_trig_equation(grade):
    # Oddiy trigonometrik tenglamani yechish (10-11 sinf) - qiymatlar foiz
    # ko'rinishida berilgani uchun natija ANIQ va butun son bo'ladi
    func, pct, angle = random.choice(TRIG_EQUATION_FACTS)
    return f"{func}(xВ°) = {pct}% tenglamani yeching (0В° в‰¤ x в‰¤ 90В°, sin(90В°)=100% deb hisoblang). x = ?", angle


GEN_TRIG = [
    (ex_trig_value, MH_TIERS),
    (ex_trig_identity, MH_TIERS),
    (ex_trig_tan, MH_TIERS),
    (ex_trig_pythagorean, MH_TIERS),
    (ex_trig_sum_angles, MH_TIERS),
    (ex_trig_equation, H_ONLY),
]


# ============================================================
# ---------- log ----------
# ============================================================
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
    base = random.choice(LOG_BASES)
    p1, p2 = random.randint(1, 3), random.randint(1, 3)
    return (
        f"log{base}({base**p1}) + log{base}({base**p2}) yig'indisi log{base}(x) ko'rinishida "
        f"yozilsa, x nechaga teng?",
        base ** (p1 + p2),
    )


GEN_LOG = [
    (ex_log_direct, H_ONLY),
    (ex_log_find_base, H_ONLY),
    (ex_log_find_power, H_ONLY),
    (ex_log_of_one, H_ONLY),
    (ex_log_of_self, H_ONLY),
    (ex_log_addition_rule, H_ONLY),
]


# ============================================================
# ---------- expo_eq (Ko'rsatkichli tenglama) ----------
# ============================================================
EXPO_BASES = [2, 3, 5, 7]


def ex_expo_direct(grade):
    # a^x = a^n ko'rinishida to'g'ridan-to'g'ri (asoslar bir xil)
    base = random.choice(EXPO_BASES)
    n = random.randint(1, 6)
    return f"{base}^x = {base**n} tenglamani yeching. x = ?", n


def ex_expo_different_base(grade):
    # a^x = b ko'rinishida, lekin b ni a ning darajasi sifatida yozish kerak
    # (masalan 8^x = 64 -> 8=2^3 emas, to'g'ridan-to'g'ri 8^x=8^2 shaklida ham
    # bo'lishi mumkin - shuning uchun bu yerda asosni ATAYLAB "boshqa" son
    # sifatida ko'rsatamiz, lekin u tanlangan asosning aniq darajasi bo'ladi)
    base = random.choice(EXPO_BASES)
    power_of_base = random.randint(2, 3)  # masalan 2^2=4, 3^2=9 - yangi "asos"
    new_base = base ** power_of_base
    n = random.randint(1, 4)
    # new_base^x = base^(power_of_base * x) = base^(power_of_base * n)
    rhs = base ** (power_of_base * n)
    return f"{new_base}^x = {rhs} tenglamani yeching (avval ikkala tomonni {base} asosiga keltiring). x = ?", n


def ex_expo_shift(grade):
    # a^(x+k) = a^n ko'rinishi - qo'shimcha algebraik qadam talab qiladi
    base = random.choice(EXPO_BASES)
    k = random.randint(1, 5)
    x = random.randint(1, 8)
    n = x + k
    return f"{base}^(x+{k}) = {base**n} tenglamani yeching. x = ?", x


def ex_expo_product_rule(grade):
    # a^x * a^k = a^n ko'rinishi - daraja qonunidan foydalanish kerak
    base = random.choice(EXPO_BASES)
    k = random.randint(1, 4)
    x = random.randint(1, 6)
    n = x + k
    return f"{base}^x Г— {base}^{k} = {base**n} tenglamani yeching. x = ?", x


def ex_expo_divide_rule(grade):
    # a^x / a^k = a^n ko'rinishi
    base = random.choice(EXPO_BASES)
    k = random.randint(1, 4)
    n = random.randint(1, 5)
    x = n + k
    return f"{base}^x Г· {base}^{k} = {base**n} tenglamani yeching. x = ?", x


GEN_EXPO_EQ = [
    (ex_expo_direct, H_ONLY),
    (ex_expo_different_base, H_ONLY),
    (ex_expo_shift, H_ONLY),
    (ex_expo_product_rule, H_ONLY),
    (ex_expo_divide_rule, H_ONLY),
]


# ============================================================
# ---------- arith_prog ----------
# ============================================================
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


def ex_arith_find_n(grade):
    # Teskari masala: a_n ma'lum, n ni topish (kuchliroq mantiqiy fikrlash)
    a1, d, n = random.randint(1, 10), random.randint(2, 8), random.randint(4, 12)
    an = a1 + (n - 1) * d
    return f"Arifmetik progressiya: a1={a1}, d={d}. Agar a_n = {an} bo'lsa, n nechaga teng?", n


GEN_ARITH_PROG = [
    (ex_arith_next, MH_TIERS),
    (ex_arith_nth, MH_TIERS),
    (ex_arith_sum, MH_TIERS),
    (ex_arith_find_d, MH_TIERS),
    (ex_arith_find_n, MH_TIERS),
]


# ============================================================
# ---------- geom_prog ----------
# ============================================================
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


def ex_geom_find_n(grade):
    a1, q, n = random.randint(1, 4), random.randint(2, 3), random.randint(2, 5)
    an = a1 * (q ** (n - 1))
    return f"Geometrik progressiya: a1={a1}, q={q}. Agar a_n = {an} bo'lsa, n nechaga teng?", n


def ex_geom_infinite_sum(grade):
    # Cheksiz kamayuvchi geometrik progressiya yig'indisi: S = a1 Г· (1 в€’ q), |q| < 1.
    # Natija butun son chiqishi uchun q = 1/k va a1 = mГ—(kв€’1) qilib tanlanadi:
    # S = a1 Г· (1 в€’ 1/k) = a1Г—k Г· (kв€’1) = mГ—(kв€’1)Г—k Г· (kв€’1) = mГ—k
    k = random.randint(2, 6)
    m = random.randint(1, 8)
    a1 = m * (k - 1)
    s = m * k
    return f"Cheksiz kamayuvchi geometrik progressiya: a1={a1}, q=1/{k}. Uning yig'indisi (S) nechaga teng?", s


GEN_GEOM_PROG = [
    (ex_geom_next, MH_TIERS),
    (ex_geom_nth, MH_TIERS),
    (ex_geom_sum, MH_TIERS),
    (ex_geom_find_q, MH_TIERS),
    (ex_geom_find_n, MH_TIERS),
    (ex_geom_infinite_sum, H_ONLY),
]


# ============================================================
# ---------- combinatorics (Kombinatorika) ----------
# ============================================================
def ex_combo_factorial(grade):
    n = random.randint(3, 9)
    return f"{n}! (n faktorial) nechaga teng?", math.factorial(n)


def ex_combo_permutation(grade):
    # O'rin almashtirish: barcha n ta elementni tartiblash soni = n!
    n = random.randint(3, 8)
    obj = random.choice(["kitob", "rasm", "gul dastasi", "medal", "o'quvchi"])
    return f"{n} ta har xil {obj}ni qatorga necha xil usulda tizib qo'yish mumkin?", math.factorial(n)


def ex_combo_arrangement(grade):
    # Joylashtirish A_n^k = n! / (n-k)! - tartib MUHIM
    n = random.randint(4, 12)
    k = random.randint(2, min(6, n - 1))
    result = math.perm(n, k)
    return f"{n} ta o'quvchidan {k} tasini (1-o'rin, 2-o'rin, ... tartib bilan) tanlab, navbat bilan sahnaga chiqarish kerak. Nechta xil usul bor? (A_{n}^{k})", result


def ex_combo_combination(grade):
    # Kombinatsiya C_n^k = n! / (k!(n-k)!) - tartib MUHIM EMAS
    n = random.randint(4, 15)
    k = random.randint(2, min(7, n - 1))
    result = math.comb(n, k)
    return f"{n} kishidan iborat guruhdan {k} kishilik komissiya (tartibsiz) necha xil usulda tanlanishi mumkin? (C_{n}^{k})", result


def ex_combo_multiplication_rule(grade):
    # Ko'paytirish qoidasi (asosiy sanash printsipi)
    a = random.randint(2, 8)
    b = random.randint(2, 8)
    c = random.randint(2, 7)
    item1, item2, item3 = random.sample(
        ["ko'ylak", "shim", "poyabzal", "shlyapa", "rang", "model", "o'lcham", "material", "aksessuar"], 3
    )
    return (
        f"{a} xil {item1}, {b} xil {item2} va {c} xil {item3} bor. Ulardan bittadan tanlab, "
        f"nechta turli kombinatsiya hosil qilish mumkin?",
        a * b * c,
    )


def ex_combo_committee_with_roles(grade):
    # Guruhdan aynan bitta lavozimga (masalan rais) tanlash - Arrangement mantig'ining
    # yana bir ko'rinishi, savol matni butunlay boshqacha
    n = random.randint(5, 15)
    return f"{n} kishilik jamoadan bitta rais va bitta kotib (ikkalasi ham har xil kishi) necha xil usulda tanlanadi?", n * (n - 1)



GEN_COMBINATORICS = [
    (ex_combo_factorial, H_ONLY),
    (ex_combo_permutation, H_ONLY),
    (ex_combo_arrangement, H_ONLY),
    (ex_combo_combination, H_ONLY),
    (ex_combo_multiplication_rule, H_ONLY),
    (ex_combo_committee_with_roles, H_ONLY),
]


# ==================== 5-SINF YANGI GENERATORLARI ====================
# Eski 5-7 sinf generatorlari ishlatilmaydi. 5-sinf uchun alohida,
# mazmuni turlicha va javobi butun son bo'ladigan generatorlar shu yerda.
# Har bir funksiya (savol_matni, javob) qaytaradi.


def _fifth_avg_numbers(count=3, lo=10, hi=60):
    avg = random.randint(lo, hi)
    # Yig'indisi aynan count * avg bo'ladigan sonlar tuziladi.
    nums = [avg] * count
    for _ in range(40):
        nums = [random.randint(max(1, lo), hi) for _ in range(count - 1)]
        last = count * avg - sum(nums)
        if max(1, lo) <= last <= hi:
            nums.append(last)
            random.shuffle(nums)
            return nums
    return [avg] * count


# ---------- 5-sinf qo'shish/ayirish ----------
def f5_add_sub_direct(grade):
    a = random.randint(120, 4500)
    b = random.randint(120, 2800)
    c = random.randint(50, 1600)
    if random.choice([True, False]):
        return f"{a} + {b} в€’ {c} = ?", a + b - c
    total = a + b
    return f"{total} в€’ {a} в€’ {c} = ?", total - a - c


def f5_add_sub_missing(grade):
    x = random.randint(50, 900)
    known = random.randint(20, x - 1)
    total = x + known
    return f"в–Ў + {known} = {total}. в–Ў o'rniga qaysi son keladi?", x


def f5_add_sub_difference(grade):
    a = random.randint(250, 5000)
    b = random.randint(80, a - 1)
    return f"Kutubxonada {a} ta kitob bor. Shundan {b} tasi badiiy kitob. Qolgan kitoblar soni nechta?", a - b


def f5_add_sub_shopping(grade):
    p1 = random.randint(2, 15) * 1000
    p2 = random.randint(2, 12) * 1000
    p3 = random.randint(1, 8) * 1000
    paid = ((p1 + p2 + p3) // 10000 + 2) * 10000
    return (f"Do'konda daftar {p1} so'm, qalam {p2} so'm va kitob {p3} so'm turadi. "
            f"{paid} so'm berilsa, qancha qaytim olinadi?"), paid - (p1 + p2 + p3)


def f5_add_sub_two_step(grade):
    start = random.randint(80, 500)
    added = random.randint(30, 250)
    used = random.randint(20, added + 30)
    result = start + added - used
    return (f"Omborda {start} kg guruch bor edi. Yana {added} kg keltirildi, "
            f"so'ng {used} kg sotildi. Omborda necha kg qoldi?"), result


def f5_add_sub_compare(grade):
    a = random.randint(200, 3000)
    diff = random.randint(20, min(500, a - 1))
    b = a - diff
    return f"{a} va {b} sonlarining ayirmasi nechaga teng?", diff


GEN_F5_ADD_SUB = [
    (f5_add_sub_direct, {"easy"}),
    (f5_add_sub_missing, {"easy"}),
    (f5_add_sub_difference, {"easy"}),
    (f5_add_sub_shopping, {"easy"}),
    (f5_add_sub_two_step, {"easy"}),
    (f5_add_sub_compare, {"easy"}),
    (ex_add_sub_plain, EM_TIERS),
    (ex_add_sub_balance, MH_TIERS),
    (ex_add_sub_missing_term, MH_TIERS),
]


# ---------- 5-sinf ko'paytirish/bo'lish ----------
def f5_mul_div_direct(grade):
    a = random.randint(12, 180)
    b = random.randint(2, 12)
    if random.choice([True, False]):
        return f"{a} Г— {b} = ?", a * b
    total = a * b
    return f"{total} Г· {b} = ?", a


def f5_mul_div_boxes(grade):
    boxes = random.randint(4, 18)
    each = random.randint(6, 35)
    return f"{boxes} ta qutining har birida {each} tadan qalam bor. Jami nechta qalam bor?", boxes * each


def f5_mul_div_share(grade):
    groups = random.randint(3, 12)
    each = random.randint(5, 30)
    total = groups * each
    item = random.choice(["daftar", "qalam", "olma", "kitob", "konfet"])
    return f"{total} ta {item} {groups} ta o'quvchiga teng taqsimlandi. Har biriga nechtadan tegadi?", each


def f5_mul_div_price(grade):
    price = random.randint(3, 25) * 1000
    count = random.randint(2, 12)
    return f"1 ta daftar {price} so'm. {count} ta daftar uchun qancha pul kerak?", price * count


def f5_mul_div_remainder(grade):
    divisor = random.randint(3, 9)
    quotient = random.randint(5, 30)
    remainder = random.randint(1, divisor - 1)
    dividend = divisor * quotient + remainder
    return f"{dividend} ni {divisor} ga bo'lganda qoldiq nechaga teng?", remainder


def f5_mul_div_order(grade):
    a = random.randint(2, 12)
    b = random.randint(2, 10)
    c = random.randint(2, 20)
    result = a * b + c
    return f"{a} Г— {b} + {c} = ? (avval ko'paytirish bajariladi)", result


GEN_F5_MUL_DIV = [
    (f5_mul_div_direct, {"easy"}),
    (f5_mul_div_boxes, {"easy"}),
    (f5_mul_div_share, {"easy"}),
    (f5_mul_div_price, {"easy"}),
    (f5_mul_div_remainder, {"easy"}),
    (f5_mul_div_order, {"easy"}),
    (ex_mul_div_plain, EM_TIERS),
    (ex_mul_div_price, ALL_TIERS),
    (ex_mul_div_order_ops, MH_TIERS),
    (ex_mul_div_distributive, MH_TIERS),
    (ex_mul_div_two_step, H_ONLY),
]


# ---------- 5-sinf kasrlar ----------
def f5_fraction_part(grade):
    den = random.choice([2, 3, 4, 5, 10])
    num = random.randint(1, den - 1)
    whole = den * random.randint(3, 30)
    return f"{whole} sonining {num}/{den} qismi nechaga teng?", whole * num // den


def f5_fraction_remaining(grade):
    den = random.choice([2, 3, 4, 5, 10])
    used_num = random.randint(1, den - 1)
    total = den * random.randint(3, 25)
    used = total * used_num // den
    return f"{total} ta daftarining {used_num}/{den} qismi tarqatildi. Nechta daftar qoldi?", total - used


def f5_fraction_same_den_add(grade):
    den = random.choice([2, 3, 4, 5, 6, 8])
    n1 = random.randint(1, den - 1)
    n2 = random.randint(1, den - n1)
    total = den * random.randint(3, 20)
    return (f"{total} ning {n1}/{den} qismi va {n2}/{den} qismi yig'indisi nechaga teng?",
            total * (n1 + n2) // den)


def f5_fraction_compare(grade):
    den = random.choice([3, 4, 5, 6, 8, 10])
    n1 = random.randint(1, den - 1)
    n2 = random.randint(1, den - 1)
    while n1 == n2:
        n2 = random.randint(1, den - 1)
    bigger = 1 if n1 > n2 else 2
    return f"{n1}/{den} va {n2}/{den} kasrlaridan qaysi biri katta? (1 yoki 2)", bigger


def f5_fraction_word(grade):
    den = random.choice([2, 4, 5, 10])
    num = random.randint(1, den - 1)
    total = den * random.randint(4, 20)
    used = total * num // den
    item = random.choice(["olma", "kitob", "gul", "shar"])
    return f"Savatda {total} ta {item} bor. Uning {num}/{den} qismi sotildi. Nechta {item} sotildi?", used


GEN_F5_FRACTION = [
    (f5_fraction_part, {"easy"}),
    (f5_fraction_remaining, {"easy"}),
    (f5_fraction_same_den_add, {"easy"}),
    (f5_fraction_compare, {"easy"}),
    (f5_fraction_word, {"easy"}),
    (ex_fraction_simple, EM_TIERS),
    (ex_fraction_nested, MH_TIERS),
    (ex_fraction_common_denom_add, MH_TIERS),
    (ex_fraction_compare, MH_TIERS),
]


# ---------- 5-sinf foizlar ----------
def f5_percent_direct(grade):
    base = random.randint(2, 50) * 100
    pct = random.choice([10, 20, 25, 50])
    return f"{base} ning {pct}% i nechaga teng?", base * pct // 100


def f5_percent_discount(grade):
    price = random.randint(2, 30) * 10000
    pct = random.choice([10, 20, 25, 50])
    discount = price * pct // 100
    return f"Narxi {price} so'm bo'lgan buyumga {pct}% chegirma berildi. Chegirma qancha?", discount


def f5_percent_students(grade):
    total = random.randint(2, 20) * 10
    pct = random.choice([10, 20, 30, 40, 50])
    girls = total * pct // 100
    return f"Sinfda {total} nafar o'quvchi bor. Ularning {pct}% i qizlar. Nechta qiz bor?", girls


def f5_percent_increase(grade):
    base = random.randint(2, 20) * 1000
    pct = random.choice([10, 20, 25])
    return f"{base} so'm narx {pct}% ga oshdi. Yangi narx qancha?", base + base * pct // 100


def f5_percent_find_whole(grade):
    pct = random.choice([10, 20, 25, 50])
    whole = random.randint(2, 20) * 100
    part = whole * pct // 100
    return f"Bir sonning {pct}% i {part} ga teng. Shu sonni toping.", whole


GEN_F5_PERCENT = [
    (f5_percent_direct, {"easy"}),
    (f5_percent_discount, {"easy"}),
    (f5_percent_students, {"easy"}),
    (f5_percent_increase, {"easy"}),
    (f5_percent_find_whole, {"easy"}),
    (ex_percent_successive, MH_TIERS),
    (ex_percent_reverse, MH_TIERS),
]


# ---------- 5-sinf daraja (kvadrat/kub) ----------
def f5_power_square(grade):
    a = random.randint(2, 25)
    return f"{a}ВІ = ?", a * a


def f5_power_cube(grade):
    a = random.randint(2, 10)
    return f"{a}Ві = ?", a ** 3


def f5_power_missing_square(grade):
    a = random.randint(2, 20)
    return f"Qaysi sonning kvadrati {a*a} ga teng?", a


def f5_power_expression(grade):
    a = random.randint(2, 10)
    b = random.randint(2, 8)
    return f"{a}ВІ + {b}ВІ = ?", a * a + b * b


GEN_F5_POWER = [
    (f5_power_square, {"easy"}),
    (f5_power_cube, {"easy"}),
    (f5_power_missing_square, {"easy"}),
    (f5_power_expression, {"easy"}),
    (ex_power_law_mul, MH_TIERS),
    (ex_power_law_div, MH_TIERS),
    (ex_power_law_value, MH_TIERS),
]


# ---------- 5-sinf nisbat ----------
def f5_ratio_scale(grade):
    a = random.randint(2, 12)
    b = random.randint(2, 12)
    k = random.randint(2, 6)
    return f"{a}:{b} nisbatning ikkala hadi {k} marta oshirilsa, yangi ikkinchi had nechaga teng?", b * k


def f5_ratio_split(grade):
    p, q = random.randint(1, 5), random.randint(1, 5)
    total_parts = p + q
    part = random.randint(3, 20)
    total = total_parts * part
    first = p * part
    return f"{total} ta buyum {p}:{q} nisbatda ikki guruhga bo'lindi. Birinchi guruhda nechta buyum bor?", first


def f5_ratio_students(grade):
    boys_part = random.randint(1, 4)
    girls_part = random.randint(1, 4)
    unit = random.randint(3, 12)
    total = (boys_part + girls_part) * unit
    return f"Sinfda o'g'il va qizlar soni {boys_part}:{girls_part} nisbatda. Jami {total} o'quvchi bo'lsa, o'g'il bolalar nechta?", boys_part * unit


def f5_ratio_equal(grade):
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    k = random.randint(2, 7)
    return f"{a}:{b} nisbatga teng nisbatda birinchi had {a*k} bo'lsa, ikkinchi had nechaga teng?", b * k


GEN_F5_RATIO = [
    (f5_ratio_scale, {"easy"}),
    (f5_ratio_split, {"easy"}),
    (f5_ratio_students, {"easy"}),
    (f5_ratio_equal, {"easy"}),
    (ex_ratio_three_part, MH_TIERS),
    (ex_ratio_scale, MH_TIERS),
]


# ---------- 5-sinf o'rtacha qiymat ----------
def f5_average_three(grade):
    nums = _fifth_avg_numbers(3, 10, 80)
    return f"{', '.join(map(str, nums))} sonlarining o'rtacha qiymati nechaga teng?", sum(nums) // 3


def f5_average_four(grade):
    nums = _fifth_avg_numbers(4, 10, 70)
    return f"{', '.join(map(str, nums))} sonlarining o'rtacha qiymati nechaga teng?", sum(nums) // 4


def f5_average_score(grade):
    avg = random.randint(3, 5)
    scores = [avg, avg, avg]
    while True:
        scores = [random.randint(2, 5) for _ in range(3)]
        if sum(scores) % 3 == 0:
            break
    return f"O'quvchining uchta bahosi {scores[0]}, {scores[1]}, {scores[2]}. O'rtacha bahosi nechaga teng?", sum(scores) // 3


def f5_average_sum(grade):
    n = random.choice([3, 4, 5])
    avg = random.randint(10, 50)
    return f"{n} ta sonning o'rtacha qiymati {avg}. Ularning yig'indisi nechaga teng?", n * avg


GEN_F5_AVERAGE = [
    (f5_average_three, {"easy"}),
    (f5_average_four, {"easy"}),
    (f5_average_score, {"easy"}),
    (f5_average_sum, {"easy"}),
    (ex_average_find_missing, MH_TIERS),
]


# ---------- 5-sinf sodda tenglamalar ----------
def f5_eq_add(grade):
    x = random.randint(2, 100)
    b = random.randint(1, 100)
    return f"x + {b} = {x+b}. x = ?", x


def f5_eq_sub(grade):
    x = random.randint(2, 100)
    b = random.randint(1, x)
    return f"x в€’ {b} = {x-b}. x = ?", x


def f5_eq_mul(grade):
    x = random.randint(2, 25)
    a = random.randint(2, 10)
    return f"{a} Г— x = {a*x}. x = ?", x


def f5_eq_div(grade):
    x = random.randint(2, 30)
    a = random.randint(2, 10)
    return f"x Г· {a} = {x}. x = ?", x * a


def f5_eq_word(grade):
    x = random.randint(5, 60)
    extra = random.randint(5, 40)
    total = x + extra
    return f"Bir sonning ustiga {extra} qo'shilsa, {total} hosil bo'ladi. Noma'lum sonni toping.", x


GEN_F5_LINEAR = [
    (f5_eq_add, {"easy"}),
    (f5_eq_sub, {"easy"}),
    (f5_eq_mul, {"easy"}),
    (f5_eq_div, {"easy"}),
    (f5_eq_word, {"easy"}),
    (ex_linear_both_sides, MH_TIERS),
    (ex_linear_minus, MH_TIERS),
    (ex_linear_parentheses, MH_TIERS),
    (ex_linear_parentheses_both, H_ONLY),
    (ex_linear_two_step_word, MH_TIERS),
]


# ---------- 5-sinf uchburchak ----------
def f5_triangle_perimeter(grade):
    a = random.randint(4, 15)
    b = random.randint(4, 15)
    c = random.randint(max(3, abs(a-b)+1), a+b-1)
    return f"Tomonlari {a} sm, {b} sm va {c} sm bo'lgan uchburchak perimetri nechaga teng?", a+b+c


def f5_triangle_area(grade):
    base = random.randint(4, 20)
    height = random.randint(2, 20)
    if base * height % 2:
        height += 1
    return f"Asosi {base} sm, balandligi {height} sm bo'lgan uchburchak yuzasi nechaga teng?", base*height//2


def f5_triangle_missing_side(grade):
    a = random.randint(5, 15)
    b = random.randint(5, 15)
    c = random.randint(max(3, abs(a-b)+1), a+b-1)
    p = a+b+c
    return f"Uchburchak perimetri {p} sm. Ikki tomoni {a} sm va {b} sm. Uchinchi tomoni nechaga teng?", c


def f5_triangle_equal(grade):
    side = random.randint(4, 18)
    return f"Teng tomonli uchburchakning bir tomoni {side} sm. Perimetri nechaga teng?", side*3


GEN_F5_TRIANGLE = [
    (f5_triangle_perimeter, {"easy"}),
    (f5_triangle_area, {"easy"}),
    (f5_triangle_missing_side, {"easy"}),
    (f5_triangle_equal, {"easy"}),
    (ex_triangle_right_pythagorean, MH_TIERS),
    (ex_triangle_height_from_area, MH_TIERS),
]


# ---------- 5-sinf to'g'ri to'rtburchak ----------
def f5_rect_area(grade):
    a = random.randint(3, 25)
    b = random.randint(3, 25)
    return f"Tomonlari {a} sm va {b} sm bo'lgan to'g'ri to'rtburchak yuzasi nechaga teng?", a*b


def f5_rect_perimeter(grade):
    a = random.randint(3, 25)
    b = random.randint(3, 25)
    return f"Tomonlari {a} sm va {b} sm bo'lgan to'g'ri to'rtburchak perimetri nechaga teng?", 2*(a+b)


def f5_rect_missing_side(grade):
    a = random.randint(3, 20)
    b = random.randint(3, 20)
    area = a*b
    return f"To'g'ri to'rtburchak yuzasi {area} smВІ, bir tomoni {a} sm. Ikkinchi tomoni nechaga teng?", b


def f5_rect_word(grade):
    a = random.randint(5, 20)
    b = random.randint(5, 20)
    return f"Bog'ning uzunligi {a} m, eni {b} m. Uni to'liq o'rash uchun necha metr chegara kerak?", 2*(a+b)


GEN_F5_RECTANGLE = [
    (f5_rect_area, {"easy"}),
    (f5_rect_perimeter, {"easy"}),
    (f5_rect_missing_side, {"easy"}),
    (f5_rect_word, {"easy"}),
    (ex_rect_diagonal, MH_TIERS),
    (ex_rect_perimeter_from_area_side, MH_TIERS),
]


# ---------- 5-sinf tezlik-vaqt-masofa ----------
def f5_speed_distance(grade):
    speed = random.randint(20, 80)
    time = random.randint(2, 6)
    return f"Velosipedchi {speed} km/soat tezlikda {time} soat yurdi. Qancha masofa bosib o'tdi?", speed*time


def f5_speed_find_speed(grade):
    time = random.randint(2, 8)
    speed = random.randint(20, 70)
    distance = speed*time
    return f"Mashina {distance} km yo'lni {time} soatda bosib o'tdi. Uning tezligi qancha?", speed


def f5_speed_find_time(grade):
    speed = random.randint(20, 80)
    time = random.randint(2, 6)
    distance = speed*time
    return f"Poyezd {distance} km yo'lni {speed} km/soat tezlikda bosib o'tdi. Yo'lda necha soat bo'ldi?", time


def f5_speed_word(grade):
    speed = random.randint(10, 50)
    time = random.randint(2, 6)
    return f"Sayohatchi har soatda {speed} km yuradi. {time} soatda necha km yuradi?", speed*time


GEN_F5_SPEED = [
    (f5_speed_distance, {"easy"}),
    (f5_speed_find_speed, {"easy"}),
    (f5_speed_find_time, {"easy"}),
    (f5_speed_word, {"easy"}),
    (ex_speed_meeting, MH_TIERS),
    (ex_speed_catchup, H_ONLY),
]


# 5-sinf uchun yangi generatorlarni tegishli mavzularga ulaymiz.
GEN_ADD_SUB = GEN_F5_ADD_SUB
GEN_MUL_DIV = GEN_F5_MUL_DIV
GEN_FRACTION = GEN_F5_FRACTION
GEN_PERCENT = GEN_F5_PERCENT
GEN_POWER = GEN_F5_POWER
GEN_RATIO = GEN_F5_RATIO
GEN_AVERAGE = GEN_F5_AVERAGE
GEN_LINEAR_EQ = GEN_F5_LINEAR
GEN_TRIANGLE = GEN_F5_TRIANGLE
GEN_RECTANGLE = GEN_F5_RECTANGLE
GEN_SPEED = GEN_F5_SPEED

HINTS.update({
    "add_sub": "Avval qavs bo'lmasa amallarni chapdan o'ngga tartib bilan bajaring. Noma'lum hadni topishda teskari amalni qo'llang.",
    "mul_div": "Ko'paytirish va bo'lishni tekshirish uchun teskari amalni bajaring. Qoldiqli bo'lishda qoldiq bo'luvchidan kichik bo'ladi.",
    "fraction": "Sonni maxrajga bo'lib, suratga ko'paytiring. Bir xil maxrajli kasrlarda suratlarni taqqoslash oson.",
    "percent": "10% вЂ” sonning o'ndan biri, 50% вЂ” yarmi, 25% вЂ” choragi. Zarur bo'lsa foizni 100 ga bo'lib hisoblang.",
    "power": "Kvadrat вЂ” sonni o'ziga bir marta ko'paytirish; kub вЂ” sonni o'ziga ikki marta ko'paytirish.",
    "ratio": "Nisbatdagi bir qism qiymatini topib, kerakli qismlar soniga ko'paytiring.",
    "average": "Barcha sonlarni qo'shing va nechta son bo'lsa, shunga bo'ling.",
    "linear_eq": "Noma'lumni yolg'iz qoldirish uchun teskari amalni bajaring.",
    "triangle": "Perimetr вЂ” uchala tomon yig'indisi. Yuzasi = asos Г— balandlik Г· 2.",
    "rectangle": "Perimetr = 2 Г— (uzunlik + eni), yuza = uzunlik Г— eni.",
    "speed": "Masofa = tezlik Г— vaqt; tezlik = masofa Г· vaqt; vaqt = masofa Г· tezlik.",
})

TOPIC_GENERATORS = {
    "add_sub": GEN_ADD_SUB,
    "mul_div": GEN_MUL_DIV,
    "percent": GEN_PERCENT,
    "fraction": GEN_FRACTION,
    "power": GEN_POWER,
    "sqrt": GEN_SQRT,
    "linear_eq": GEN_LINEAR_EQ,
    "quad_eq": GEN_QUAD_EQ,
    "system_eq": GEN_SYSTEM_EQ,
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
    "expo_eq": GEN_EXPO_EQ,
    "arith_prog": GEN_ARITH_PROG,
    "geom_prog": GEN_GEOM_PROG,
    "combinatorics": GEN_COMBINATORICS,
}


def generate_example(user_id, topic, grade="medium"):
    """
    Berilgan mavzu/daraja uchun misol yaratadi. Faqat SHU DARAJAGA mos
    (tiers to'plamida bor) generatorlar orasidan tasodifiy tanlanadi - shu
    tufayli 8-9 va 10-11 sinf o'quvchisiga hech qachon 5-7 sinf darajasidagi
    "bolalarcha" misol chiqmaydi. Foydalanuvchining shu mavzudagi so'nggi
    savollari bilan solishtirib, TAKRORLANMAYDIGAN savol qaytariladi.
    """
    all_generators = TOPIC_GENERATORS[topic]
    generators = [func for func, tiers in all_generators if grade in tiers]
    if not generators:
        # Ehtiyot chorasi: agar shu darajaga mos generator bo'lmasa, hammasidan foydalanamiz
        generators = [func for func, _ in all_generators]

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
        # Barcha kombinatsiyalar tugagan bo'lsa (masalan trig/log kabi
        # cheklangan mavzularda) - eng eski tarixni tozalab, yangidan boshlaymiz
        history = set()
        gen_func = random.choice(generators)
        text, answer = gen_func(grade)

    save_to_history(user_id, topic, text)
    return text, answer


def get_hint_keyboard(topic):
    builder = InlineKeyboardBuilder()
    builder.button(text="рџ’Ў Yordam", callback_data=f"hint_{topic}")
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
    builder.button(text="рџ’Ў Yordam", callback_data=f"hint_{topic}")
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
            f"вЏ±пёЏ Vaqt tugadi!\n\n"
            f"60 soniyada siz {count} ta misolni to'g'ri yechdingiz! рџЋ‰\n\n"
            f"Yana urinish uchun /speedtest yozing."
        )


# ==================== HANDLERS ====================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    get_user(message.from_user.id, message.from_user.first_name)
    await message.answer(
        f"Salom, {message.from_user.first_name}! рџ¤–\n\n"
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
        f"Endi mavzuni tanlang, yoki pastdagi рџ“‹ Menu tugmasidan barcha buyruqlarni ko'ring:"
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
        f"рџ“ќ Misol: {example_text}\n\n"
        f"To'g'ri javobni tanlang рџ‘‡\n\n"
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
        result_text = f"{random.choice(MOTIVATIONS)} рџ”Ґ Streak: {new_streak} kun"
    else:
        update_user(user_id, wrong=user["wrong"] + 1)
        update_topic_stat(user_id, topic, wrong_delta=1)
        old_question = user["current_question"] or "?"
        log_mistake(user_id, topic, old_question, correct_answer)
        solution_hint = HINTS.get(topic, "")
        result_text = f"вќЊ Noto'g'ri. To'g'ri javob: {correct_answer}\nрџ“– Yechim: {solution_hint}"

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
        f"рџ“ќ Keyingi misol{topic_label}: {example_text}\n\n"
        f"To'g'ri javobni tanlang рџ‘‡",
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
        f"рџЋІ Aralash rejim yoqildi! Har safar boshqa mavzudan savol keladi.\n\n"
        f"рџ“ќ Misol ({TOPICS[topic]}): {example_text}\n\n"
        f"To'g'ri javobni tanlang рџ‘‡",
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
        f"в­ђ Challenge rejimi! Faqat eng qiyin darajadagi savollar.\n\n"
        f"рџ“ќ Misol ({TOPICS[topic]}): {example_text}\n\n"
        f"To'g'ri javobni tanlang рџ‘‡",
        reply_markup=get_answer_keyboard(topic, options)
    )


@dp.message(Command("formula"))
async def formula_handler(message: types.Message):
    await message.answer("рџ“ Qaysi mavzu formulasini ko'rmoqchisiz?", reply_markup=get_formula_keyboard())


@dp.callback_query(F.data.startswith("formula_"))
async def formula_chosen(callback: types.CallbackQuery):
    topic = callback.data.replace("formula_", "")
    text = FORMULAS.get(topic, "Formula topilmadi.")
    await callback.message.answer(f"рџ“ {text}")
    await callback.answer()


@dp.message(Command("mistakes"))
async def mistakes_handler(message: types.Message):
    user_id = message.from_user.id
    rows = get_recent_mistakes(user_id, limit=8)

    if not rows:
        await message.answer("рџ“– Hali xatolaringiz yo'q. Zo'r natija!")
        return

    text = "рџ“– Oxirgi xatolaringiz:\n\n"
    for topic, question, correct_answer in rows:
        topic_name = TOPICS.get(topic, topic)
        text += f"вЂў {topic_name}: {question} в†’ to'g'ri javob: {correct_answer}\n"
    text += "\nShu mavzularni qayta mashq qilish uchun Menu orqali /topics ni tanlang."

    await message.answer(text)


@dp.message(Command("topweek"))
async def topweek_handler(message: types.Message):
    rows = get_weekly_top(10)

    if not rows:
        await message.answer("Bu hafta hali hech kim mashq qilmagan.")
        return

    text = "рџ“… Haftalik reyting:\n\n"
    medals = ["рџҐ‡", "рџҐ€", "рџҐ‰"]
    for i, (name, week_correct) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {name} вЂ” {week_correct} ta to'g'ri\n"

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

    text = "рџ“Љ Bilim xaritangiz:\n\n"
    for topic, correct, wrong in rows_sorted:
        total = correct + wrong
        pct = round(correct / total * 100) if total > 0 else 0
        topic_name = TOPICS.get(topic, topic)
        filled = pct // 10
        bar = "рџџ©" * filled + "в¬њ" * (10 - filled)
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
        f"рџ“Љ Sizning statistikangiz:\n\n"
        f"вњ… To'g'ri: {correct}\n"
        f"вќЊ Noto'g'ri: {wrong}\n"
        f"рџЋЇ Aniqlik: {percent}%\n"
        f"рџ”Ґ Streak: {streak} kun"
    )


@dp.message(Command("top"))
async def top_handler(message: types.Message):
    top_users = get_top_users(10)

    if not top_users:
        await message.answer("Hozircha hech kim mashq qilmagan.")
        return

    text = "рџЏ† Eng yaxshi 10 ta ishtirokchi:\n\n"
    medals = ["рџҐ‡", "рџҐ€", "рџҐ‰"]
    for i, (name, correct, wrong) in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {name} вЂ” {correct} ta to'g'ri\n"

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
        f"рџЏ« Guruh yaratildi: {name}\n\n"
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

    await message.answer(f"вњ… Siz \"{group[2]}\" guruhiga qo'shildingiz!")


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
        text += f"рџЏ« {name} (kod: {code})\n"
        if not students:
            text += "   Hali o'quvchi qo'shilmagan.\n\n"
            continue
        for first_name, correct, wrong in students:
            total = correct + wrong
            pct = round(correct / total * 100) if total > 0 else 0
            text += f"   вЂў {first_name} вЂ” {correct} to'g'ri ({pct}%)\n"
        text += "\n"

    await message.answer(text)


@dp.message(Command("path"))
async def path_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)
    statuses, current_topic = get_path_status(user_id)

    text = "рџ“љ O'quv yo'lingiz:\n\n"
    for t, status in statuses:
        icon = {"done": "вњ…", "current": "в–¶пёЏ", "locked": "рџ”’"}[status]
        text += f"{icon} {TOPICS[t]}\n"

    text += f"\nHozirgi bosqich: {TOPICS[current_topic]}\n(Bir bosqichni ochish uchun kamida 5 ta savolda 70% to'g'ri javob bering)"

    builder = InlineKeyboardBuilder()
    builder.button(text="в–¶пёЏ Boshlash", callback_data=f"pathstart_{current_topic}")
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
        f"рџ”— Sizning shaxsiy kodingiz: `{code}`\n\n"
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
        await message.answer("Bu sizning o'z kodingiz рџ™‚ Do'stingizning kodini kiriting.")
        return

    me = get_user(message.from_user.id, message.from_user.first_name)
    me_total = me["correct"] + me["wrong"]
    me_pct = round(me["correct"] / me_total * 100) if me_total > 0 else 0
    other_total = other["correct"] + other["wrong"]
    other_pct = round(other["correct"] / other_total * 100) if other_total > 0 else 0

    await message.answer(
        f"рџ”— Taqqoslash:\n\n"
        f"рџ‘¤ {me['first_name']}: {me['correct']} to'g'ri, {me_pct}% aniqlik, рџ”Ґ{me['streak']} kun\n"
        f"рџ‘¤ {other['first_name']}: {other['correct']} to'g'ri, {other_pct}% aniqlik, рџ”Ґ{other['streak']} kun"
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
        f"вЏ±пёЏ Tezlik testi boshlandi! 60 soniyada nechta misol yecha olasiz?\n\n"
        f"рџ“ќ {example_text} = ?"
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
            await message.answer("вњ… To'g'ri!")
        else:
            await message.answer(f"вќЊ Noto'g'ri. To'g'ri javob: {correct_answer}")

        example_text, answer = generate_example(user_id, "add_sub", "medium")
        update_user(user_id, current_answer=answer)
        await message.answer(f"рџ“ќ {example_text} = ?")
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
                        "рџ”” Bugun hali mashq qilmadingiz!\n\n"
                        "5 daqiqa vaqt ajratib, bilimingizni mustahkamlang рџ’Є\n"
                        "Menu tugmasidan /topics ni tanlang va boshlang!"
                    )
                except Exception:
                    pass  # foydalanuvchi botni bloklagan bo'lishi mumkin


async def set_bot_commands():
    commands = [
        types.BotCommand(command="start", description="Botni ishga tushirish"),
        types.BotCommand(command="topics", description="рџ“љ Mavzular ro'yxati"),
        types.BotCommand(command="path", description="рџ—єпёЏ O'quv yo'lim"),
        types.BotCommand(command="random", description="рџЋІ Aralash rejim"),
        types.BotCommand(command="challenge", description="в­ђ Qiyin savollar"),
        types.BotCommand(command="speedtest", description="вЏ±пёЏ Tezlik testi"),
        types.BotCommand(command="formula", description="рџ§® Formulalar bazasi"),
        types.BotCommand(command="mistakes", description="рџ“– Xatolaringiz"),
        types.BotCommand(command="map", description="рџ“Љ Bilim xaritangiz"),
        types.BotCommand(command="stats", description="рџ“€ Statistikangiz"),
        types.BotCommand(command="top", description="рџЏ† Umumiy reyting"),
        types.BotCommand(command="topweek", description="рџ“… Haftalik reyting"),
        types.BotCommand(command="mycode", description="рџ”— Shaxsiy kodim"),
        types.BotCommand(command="compare", description="рџ”— Do'st bilan solishtirish"),
        types.BotCommand(command="creategroup", description="рџЏ« Guruh yaratish (o'qituvchi)"),
        types.BotCommand(command="joingroup", description="рџЏ« Guruhga qo'shilish"),
        types.BotCommand(command="mystudents", description="рџЏ« O'quvchilarim (o'qituvchi)"),
        types.BotCommand(command="level", description="рџЋ“ Sinf darajasi"),
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
