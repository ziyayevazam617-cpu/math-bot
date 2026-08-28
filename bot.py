# -*- coding: utf-8 -*-
import asyncio
import random
import math
import json
import re
import sqlite3
import os
from datetime import date, timedelta, datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ==================== DATABASE ====================
DB_NAME = "bot_database.db"

# Har bir mavzu uchun tarixda saqlanadigan so'nggi savollar soni
# (shu miqdordagi so'nggi savollar takrorlanmaydi)
HISTORY_LIMIT = 300


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
        CREATE TABLE IF NOT EXISTS daily_stats (
            user_id INTEGER,
            day TEXT,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
    conn.commit()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_schedule (
            user_id INTEGER,
            topic TEXT,
            stage INTEGER DEFAULT 0,
            next_review TEXT,
            PRIMARY KEY (user_id, topic)
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
        "linear_eq": "🔤 Chiziqli tenglama",
            "triangle": "🔺 Uchburchak",
    "rectangle": "▭ To'rtburchak",
    "circle": "⭕ Doira",
    "ratio": "⚖️ Nisbat",
    "average": "📊 O'rtacha qiymat",
    "negative": "➖ Manfiy sonlar",
    "speed": "🚗 Tezlik-vaqt-masofa",
            "log": "📈 Logarifm",
    "expo_eq": "📶 Ko'rsatkichli tenglama",
            "combinatorics": "🎲 Kombinatorika",
}


GRADE_LABELS = {
    "easy": "🟢 5-sinf",
    "sixth": "🔵 6-sinf",
    "seventh": "🟠 7-sinf",
    "eighth": "🟣 8-sinf",
    "medium": "🟡 9-sinf",
    "hard": "🔴 10-sinf",
}

# 5–9-sinflar ketma-ket va mustaqil darajalar sifatida ishlaydi.
# 10-sinf menyuga alohida daraja sifatida qo‘shilgan; 11-sinf hozircha menyuda yo‘q.
GRADE_TOPICS = {
    "easy": [
        "add_sub", "mul_div", "fraction", "percent", "power",
        "ratio", "average", "linear_eq", "triangle", "rectangle", "speed",
    ],
    "sixth": [
        "add_sub", "mul_div", "divisibility", "prime_numbers", "gcd_lcm",
        "decimal", "fraction", "mixed_fraction", "percent", "ratio",
        "proportion", "negative", "power", "coordinates", "expression",
        "linear_eq", "triangle", "rectangle", "circle", "speed", "average",
    ],
    "seventh": [
        "integer7", "fraction7", "algebra_value7", "monomial7", "identity7",
        "linear7", "inequality7", "function7", "coordinate7", "angles7",
        "triangle7", "geometry7", "ratio_percent7", "sequence7",
        "combinatorics7", "logic7", "probability7",
    ],
    "eighth": [
        "rational8", "power8", "root8", "monomial8", "polynomial8", "linear8",
        "system8", "function8", "factor8", "quad8", "ineq8", "similar8",
        "pyth8", "area8", "circle8", "prob8", "stats8", "ratio8", "logic8",
        "speed8",
    ],
    "hard": [
        "rational10", "power10", "radical10", "log10", "expo10",
        "trig10", "trig_eq10", "system10", "quad10", "quad_ineq10",
        "function10", "sequence10", "arith10", "geom10",
        "combinatorics10", "probability10", "statistics10",
        "analytic10", "word10", "logic10",
    ],
}

# 9-sinf uchun alohida mavzular quyida qo'shiladi.
GRADE_TOPICS["medium"] = []

def topics_for_grade(grade):
    return GRADE_TOPICS.get(grade, GRADE_TOPICS["medium"])

HINTS = {
    "add_sub": "Sonlarni raqam ustuniga joylab, o'ngdan chapga qo'shing/ayiring.",
    "mul_div": "Ko'paytirish jadvalini eslang, bo'lishda qaysi songa necha marta sig'ishini toping.",
    "percent": "Foizni 100 ga bo'lib, songa ko'paytiring: son × foiz ÷ 100.",
    "fraction": "Avval sonni maxrajga bo'ling, keyin suratga ko'paytiring.",
    "power": "Daraja - sonni o'zi bilan necha marta ko'paytirish kerakligini bildiradi.",
        "linear_eq": "Avval erkin hadni ikkala tomondan ayiring, keyin x oldidagi songa bo'ling.",
            "triangle": "Uchburchak yuzasi = (asos × balandlik) ÷ 2.",
    "rectangle": "To'rtburchak yuzasi = tomon × tomon.",
    "circle": "Doira yuzasi = π × r × r.",
    "ratio": "Nisbatning ikkala tomonini bir xil songa ko'paytiring yoki bo'ling.",
    "average": "Barcha sonlarni qo'shib, sonlar soniga bo'ling.",
    "negative": "Manfiy sonlar bilan ishlashda son o'qini tasavvur qiling.",
    "speed": "Masofa = tezlik × vaqt.",
            "rational10": "Kasrlarni umumiy maxrajga keltiring va natijani qisqartiring.",
    "power10": "Bir xil asosli darajalarni bo‘lishda darajalar ayiriladi: a^m/a^n=a^(m-n).",
    "radical10": "Ildiz ostidan to‘liq kvadrat ko‘paytuvchini tashqariga chiqaring.",
    "log10": "log_a(b)=c degani a^c=b degani.",
    "expo10": "Avval ikki tomonni bir xil asosli darajalar ko‘rinishiga keltiring.",
    "trig10": "sin=qarshi katet/gipotenuza, cos=yopishgan katet/gipotenuza, tan=qarshi/yopishgan katet.",
    "trig_eq10": "Asosiy trigonometrik qiymatlarni va berilgan oraliqni hisobga oling.",
    "system10": "Tenglamalarni qo‘shish yoki o‘rniga qo‘yish usuli bilan yeching.",
    "quad10": "Diskriminant yoki Vieta teoremasidan foydalaning.",
    "quad_ineq10": "Parabola ishorasini va ildizlar orasidagi/orasidagi tashqi intervallarni tekshiring.",
    "function10": "x o‘rniga berilgan qiymatni qo‘yib, ifodani hisoblang.",
    "sequence10": "Arifmetik ketma-ketlikda a_n=a_1+(n-1)d.",
    "arith10": "S_n=n(a_1+a_n)/2 formulasidan foydalaning.",
    "geom10": "Geometrik ketma-ketlikda a_n=a_1*q^(n-1).",
    "combinatorics10": "Tanlashda tartib muhim bo‘lmasa C(n,k) formulasidan foydalaning.",
    "probability10": "Ehtimollik = qulay holatlar soni / barcha teng imkoniyatli holatlar soni.",
    "statistics10": "O‘rtacha = yig‘indi / kuzatuvlar soni.",
    "analytic10": "Ikki nuqta orasidagi masofada Pifagor teoremasidan foydalaning.",
    "word10": "Har bir ishchining bir soatlik unumdorligini qo‘shing: 1/T=1/t1+1/t2.",
    "logic10": "Shartlarni ketma-ket yozing, taxminni tekshiring va faqat shartlarni qanoatlantiradigan javobni tanlang.",
                    }

# Qaysi mavzularda manfiy javob/variant mantiqan to'g'ri kelishi mumkin
NEGATIVE_ALLOWED_TOPICS = {"negative", "linear_eq", "system_eq"}


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
        "• a + 0 = a,  a − 0 = a,  a − a = 0\n\n"
        "📌 Qoida: ko'p xonali sonlarni qo'shish/ayirishda raqamlarni o'ngdan chapga, "
        "xona-xona (birlik, o'nlik, yuzlik...) tekislab yozing."
    ),
    "mul_div": (
        "✖️ KO'PAYTIRISH VA BO'LISH\n\n"
        "• a × b = ko'paytma,  a ÷ b = bo'linma (b ≠ 0)\n"
        "• a × b = b × a (o'rin almashtirish qonuni)\n"
        "• (a × b) × c = a × (b × c) (guruhlash qonuni)\n"
        "• a × (b + c) = a×b + a×c (taqsimot qonuni)\n"
        "• a × 1 = a,  a × 0 = 0,  a ÷ 1 = a,  a ÷ a = 1 (a ≠ 0)\n"
        "• Bo'linma tekshiruvi: bo'linuvchi = bo'luvchi × natija + qoldiq"
    ),
    "percent": (
        "% FOIZLAR (Yakkabog' formula kitobi asosida)\n\n"
        "• a sonining P foizi: (P/100)·a\n"
        "• P foizi a ga teng bo'lgan son: (100·a)/P, ya'ni (P/100)x = a tenglamadan\n"
        "• a soni b sonining necha foizi: (a/b)·100%\n"
        "• a soni P% ga oshganda: (100+P)/100 · a = (1 + P/100)·a\n"
        "• a soni P% ga kamayganda: (100−P)/100 · a = (1 − P/100)·a\n"
        "• a soni ketma-ket P% dan n marta oshganda: a·(1 + P/100)ⁿ\n"
        "• a soni ketma-ket P% dan n marta kamayganda: a·(1 − P/100)ⁿ\n\n"
        "Qo'shimcha (a > b shart uchun):\n"
        "• a soni b sonidan necha foizga ortiq: (a/b − 1)·100%\n"
        "• b soni a sonidan necha foizga kam: (1 − b/a)·100%\n"
        "• a soni avval P₁% ga, so'ng P₂% ga oshsa: yangi qiymat = a·(1+P₁/100)·(1+P₂/100)"
    ),
    "fraction": (
        "½ KASRLAR VA QISMLAR (formula kitobi asosida)\n\n"
        "• a ning m/n qismi = (m/n)·a\n"
        "• a soni o'zining m/n qismiga ortadi: a + (m/n)a = (1 + m/n)·a\n"
        "• a soni o'zining m/n qismiga kamayadi: a − (m/n)a = (1 − m/n)·a\n"
        "• Bir xil maxrajda: a/c + b/c = (a+b)/c\n"
        "• Har xil maxrajda: a/b + c/d = (ad+bc)/(bd)\n"
        "• Ko'paytirish: (a/b)·(c/d) = (ac)/(bd)\n"
        "• Bo'lish: (a/b) ÷ (c/d) = (a/b)·(d/c) = (ad)/(bc)\n"
        "• Aralash sonni kasrga aylantirish: a b/c = (a·c+b)/c\n"
        "• Davriy kasrni oddiy kasrga aylantirish: 0,(a) = a/9,  0,(ab) = ab/99,  1,(a) = (10+a−1)/9"
    ),
    "power": (
        "xⁿ DARAJANING XOSSALARI (formula kitobi asosida)\n\n"
        "1) a⁰ = 1 (a ≠ 0)          7) aᵖ : aᵠ = aᵖ⁻ᵠ\n"
        "2) a¹ = a                  8) (aᵖ)ᵠ = aᵖ·ᵠ\n"
        "3) a⁻ⁿ = 1/aⁿ (a≠0)        9) (a·b)ᵖ = aᵖ·bᵖ\n"
        "4) aˡ/ᵗ = ᵗ√(aˡ)           10) (aᵐ·bⁿ)ᵗ = aᵐᵗ·bⁿᵗ\n"
        "5) a⁻ˡ/ᵗ = 1/ᵗ√(aˡ)        11) (a/b)ᵖ = aᵖ/bᵖ\n"
        "6) aᵖ · aᵠ = aᵖ⁺ᵠ           12) (aᵐ/bⁿ)ᵗ = aᵐᵗ/bⁿᵗ\n\n"
        "Qisqa ko'paytirish formulalari:\n"
        "• (a+b)² = a² + 2ab + b²\n"
        "• (a−b)² = a² − 2ab + b²\n"
        "• a² − b² = (a−b)(a+b)\n"
        "• (a+b)³ = a³ + 3a²b + 3ab² + b³ = a³ + b³ + 3ab(a+b)\n"
        "• (a−b)³ = a³ − 3a²b + 3ab² − b³ = a³ − b³ − 3ab(a−b)\n"
        "• a³ + b³ = (a+b)(a² − ab + b²)\n"
        "• a³ − b³ = (a−b)(a² + ab + b²)\n"
        "• Qo'shimcha: (a−b)³+(b−c)³+(c−a)³ = 3(a−b)(b−c)(c−a)"
    ),
        "linear_eq": (
        "🔤 CHIZIQLI TENGLAMA (formula kitobi asosida)\n\n"
        "• Umumiy ko'rinish: ax + b = 0, yoki ax = −b\n"
        "1) a = 0, b ≠ 0 — ildizga ega emas\n"
        "2) a = 0, b = 0 — cheksiz ko'p ildizga ega (0·x = 0)\n"
        "3) a ≠ 0, b ∈ R — x = −b/a ga teng yagona ildizga ega\n\n"
        "• Tenglamaning ikkala tomoniga bir xil son qo'shish/ayirish yechimni o'zgartirmaydi\n"
        "• Tenglamaning ikkala tomonini bir xil (nolmas) songa ko'paytirish/bo'lish yechimni o'zgartirmaydi"
    ),
            "triangle": (
        "🔺 UCHBURCHAK (formula kitobi asosida)\n\n"
        "• Uchburchak tengsizligi: a < b+c, b < a+c, c < a+b\n"
        "• Burchaklar yig'indisi = 180°\n"
        "• Perimetr P = a+b+c, yarim perimetr p = (a+b+c)/2\n"
        "• Yuza: S = a·hₐ/2 = b·h_b/2 = c·h_c/2\n"
        "• Geron formulasi: S = √(p(p−a)(p−b)(p−c))\n"
        "• S = (1/2)ab·sinγ  (γ — a va b orasidagi burchak)\n"
        "• Sinuslar teoremasi: a/sinα = b/sinβ = c/sinγ = 2R\n"
        "• Kosinuslar teoremasi: a² = b²+c²−2bc·cosα\n"
        "• To'g'ri burchaklida (c — gipotenuza): a²+b² = c² (Pifagor)\n"
        "  sinα = qarshi katet/gipotenuza, cosα = yopishgan katet/gipotenuza\n"
        "• Teng tomonli uchburchakda: S = (a²√3)/4, barcha burchak 60°, h = (a√3)/2\n"
        "• Ichki chizilgan aylana radiusi: r = S/p,  Tashqi chizilgan: R = abc/(4S)\n"
        "• Uchburchak turlari (c — eng katta tomon): c²=a²+b² to'g'ri burchakli, "
        "c²<a²+b² o'tkir burchakli, c²>a²+b² o'tmas burchakli"
    ),
    "rectangle": (
        "▭ TO'G'RI TO'RTBURCHAK (formula kitobi asosida)\n\n"
        "• Yuza S = a×b,  Perimetr P = 2(a+b)\n"
        "• Diagonal: d = √(a²+b²)\n"
        "• Diagonal orqali yuza: S = (d²·sinφ)/2  (φ — diagonallar orasidagi burchak)\n"
        "• Tashqi chizilgan aylana radiusi: R = d/2 = √(a²+b²)/2\n"
        "• Kvadrat uchun (a=b): S=a², P=4a, d=a√2, r=a/2 (ichki), R=(a√2)/2 (tashqi)"
    ),
    "circle": (
        "⭕ AYLANA VA DOIRA (formula kitobi asosida)\n\n"
        "• Diametr d = 2r\n"
        "• Aylana uzunligi l = 2πr = πd\n"
        "• Doira yuzasi S = πr² = πd²/4\n"
        "• Sektor yoyi (gradusda): l = πrα°/180°; (radianda): l = rα\n"
        "• Sektor yuzasi (gradusda): S = πr²α°/360°; (radianda): S = r²α/2\n"
        "• Segment yuzasi (radianda): S = r²(α − sinα)/2\n"
        "• Kesuvchilar xossasi: AN·BN = CN·DN (aylana ichida kesishuvchi vatarlar)\n"
        "• Urinma va kesuvchi: CN² = AN·BN\n"
        "• π ≈ 3.14 yoki 22/7 (taqribiy)"
    ),
    "ratio": (
        "⚖️ NISBAT VA PROPORTSIYA (formula kitobi asosida)\n\n"
        "• a:b = c:d, yoki a/b = c/d (proportsiya) ⇒ a·d = b·c\n"
        "• a sonini m:n:k nisbatda proportsional bo'laklarga ajratish:\n"
        "  1-qism = a·m/(m+n+k), 2-qism = a·n/(m+n+k), 3-qism = a·k/(m+n+k)\n"
        "• Teskari proportsional bo'laklarga ajratish (1/m : 1/n : 1/k nisbatda):\n"
        "  1-qism = a·(1/m)/(1/m+1/n+1/k), va h.k.\n"
        "• To'g'ri proportsionallik: y = k·x\n"
        "• Teskari proportsionallik: y = k/x"
    ),
    "average": (
        "📊 O'RTA QIYMATLAR (formula kitobi asosida)\n\n"
        "x₁, x₂, ..., xₙ sonlari uchun:\n"
        "• O'rta arifmetik = (x₁+x₂+...+xₙ)/n\n"
        "• O'rta geometrik = ⁿ√(x₁·x₂·...·xₙ)\n\n"
        "a va b sonlari uchun:\n"
        "• O'rta arifmetik = (a+b)/2\n"
        "• O'rta geometrik = √(a·b)\n\n"
        "• Yig'indi = o'rtacha × sonlar soni\n"
        "• Noma'lum son = (o'rtacha × soni) − (ma'lum sonlar yig'indisi)"
    ),
    "negative": (
        "➖ MANFIY SONLAR\n\n"
        "• (−a) + (−b) = −(a+b)\n"
        "• (−a) − b = −(a+b)\n"
        "• a − (−b) = a + b\n"
        "• (−a) + b = b − a\n"
        "• (−a)×(−b) = a×b (manfiy×manfiy = musbat)\n"
        "• (−a)×b = −(a×b) (manfiy×musbat = manfiy)\n"
        "• (−a)÷(−b) = a÷b\n"
        "• (−a)÷b = −(a÷b)"
    ),
    "speed": (
        "🚗 TEZLIK-VAQT-MASOFA (formula kitobi asosida)\n\n"
        "• Masofa S = V×T,  Tezlik V = S÷T,  Vaqt T = S÷V\n"
        "• Qarama-qarshi harakatda (bir-biriga qarab): yaqinlashish tezligi = V₁+V₂\n"
        "• Bir yo'nalishda quvib o'tishda: V(farq) = V₁−V₂\n"
        "• Oqim bo'yicha (suvda) harakatda: tezlik = qayiqning turg'un suvdagi tezligi + oqim tezligi\n"
        "• Oqimga qarshi harakatda: tezlik = qayiqning turg'un suvdagi tezligi − oqim tezligi"
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
# Har bir generator funksiya (savol_matni, javob) qaytaradi va qaysi sinf
# darajalarida ("easy"=5-7, "medium"=9, "hard"=10-11) ishlatilishi mumkinligini
# bildiruvchi TIERS to'plamiga ega bo'ladi.
#
# MUHIM PRINSIP: "medium" va "hard" darajalar uchun FAQAT o'sha darajaga mos
# KUCHLIROQ va MANTIQIY jihatdan chuqurroq generatorlar ishlatiladi - oddiy/bolalarcha
# ("necha ta olma qoldi" kabi) misollar faqat "easy" darajada qoladi. Bu orqali
# 9-sinf o'quvchisiga hech qachon 5-7 sinf darajasidagi oddiy misol chiqmaydi.

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
        return f"{a} − {b}", a - b
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
    # Bank hisobi / byudjet kontekstida - 9-sinf uchun jiddiyroq mavzu
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
    # hadni topish" - 9-sinf uchun mantiqiy fikrlashni talab qiladi
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
        return f"{a} × {b}", a * b
    return f"{a*b} ÷ {b}", a


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
    return f"({a} × {b}) ÷ {c}", (a * b) // c


def ex_mul_div_order_ops(grade):
    # Amallar tartibi (avval ko'paytirish/bo'lish, keyin qo'shish/ayirish) -
    # 9-sinf uchun muhim algebraik ko'nikma
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
    return f"{a} × {b} {op_mid} {c} × {d} = ? (amallar tartibiga rioya qiling)", result


def ex_mul_div_distributive(grade):
    # Taqsimot qonuni: a × (b + c) = a×b + a×c
    lo_a, hi_a = _rng(grade, None, (3, 15), (5, 30))
    a = random.randint(lo_a, hi_a)
    b = random.randint(2, 20)
    c = random.randint(2, 20)
    return f"Taqsimot qonunidan foydalanib hisoblang: {a} × ({b} + {c}) = ?", a * (b + c)


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



# ==================== DAILY STATS / GOAL ====================
DAILY_GOAL = 10

def log_daily_answer(user_id, is_correct):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT correct, wrong FROM daily_stats WHERE user_id = ? AND day = ?",
        (user_id, today)
    )
    row = cursor.fetchone()
    if row is None:
        correct = 1 if is_correct else 0
        wrong = 0 if is_correct else 1
        cursor.execute(
            "INSERT INTO daily_stats (user_id, day, correct, wrong) VALUES (?, ?, ?, ?)",
            (user_id, today, correct, wrong)
        )
    else:
        correct, wrong = row
        correct += 1 if is_correct else 0
        wrong += 0 if is_correct else 1
        cursor.execute(
            "UPDATE daily_stats SET correct = ?, wrong = ? WHERE user_id = ? AND day = ?",
            (correct, wrong, user_id, today)
        )
    conn.commit()
    conn.close()


def get_today_correct(user_id):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT correct FROM daily_stats WHERE user_id = ? AND day = ?",
        (user_id, today)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def get_last_n_days_stats(user_id, n=7):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    results = []
    for d in days:
        cursor.execute(
            "SELECT correct FROM daily_stats WHERE user_id = ? AND day = ?",
            (user_id, d)
        )
        row = cursor.fetchone()
        results.append((d, row[0] if row else 0))
    conn.close()
    return results


def draw_progress_image(user_id):
    data = get_last_n_days_stats(user_id, 7)
    labels = []
    values = []
    names = ["Dush", "Sesh", "Chor", "Pay", "Jum", "Shan", "Yak"]
    for iso_day, correct in data:
        y, m, d = map(int, iso_day.split("-"))
        labels.append(names[date(y, m, d).weekday()])
        values.append(correct)

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar(labels, values)
    ax.set_title("Oxirgi 7 kunlik to'g'ri javoblar")
    ax.set_ylabel("To'g'ri javoblar")
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ==================== SPACED REPETITION ====================
REVIEW_INTERVALS = {0: 1, 1: 3, 2: 7}

def schedule_review(user_id, topic, stage=0):
    days_ahead = REVIEW_INTERVALS.get(stage, 1)
    next_review = (date.today() + timedelta(days=days_ahead)).isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT topic FROM review_schedule WHERE user_id = ? AND topic = ?",
        (user_id, topic)
    )
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO review_schedule (user_id, topic, stage, next_review) VALUES (?, ?, ?, ?)",
            (user_id, topic, stage, next_review)
        )
    else:
        cursor.execute(
            "UPDATE review_schedule SET stage = ?, next_review = ? WHERE user_id = ? AND topic = ?",
            (stage, next_review, user_id, topic)
        )
    conn.commit()
    conn.close()


def get_due_reviews(user_id):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT topic, stage FROM review_schedule WHERE user_id = ? AND next_review <= ? ORDER BY next_review",
        (user_id, today)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_review_stage(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stage FROM review_schedule WHERE user_id = ? AND topic = ?",
        (user_id, topic)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def remove_review(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM review_schedule WHERE user_id = ? AND topic = ?",
        (user_id, topic)
    )
    conn.commit()
    conn.close()


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
    # Ketma-ket ikki marta foiz o'zgarishi - 9-sinf uchun klassik masala
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
    # Turli maxrajli kasrlarni umumiy maxrajga keltirib qo'shish - 9-sinf
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
    return f"{a}² = ?", a * a


def ex_power_cube(grade):
    lo, hi = _rng(grade, (2, 6), (5, 12), (8, 15))
    a = random.randint(lo, hi)
    return f"{a}³ = ?", a ** 3


def ex_power_sum_then_power(grade):
    a, b = random.randint(2, 9), random.randint(1, 9)
    p = 2 if grade == "easy" else random.choice([2, 3])
    return f"({a}+{b})^{p} = ?", (a + b) ** p


def ex_power_diff_then_square(grade):
    a = random.randint(5, 20)
    b = random.randint(1, a - 1)
    return f"({a}−{b})² = ?", (a - b) ** 2


def ex_power_law_mul(grade):
    # aᵐ × aⁿ = aᵐ⁺ⁿ - daraja qonuni (9-sinf algebra dasturi)
    base = random.randint(2, 5)
    m = random.randint(1, 4)
    n = random.randint(1, 4)
    return f"{base}^{m} × {base}^{n} ni {base} ning bitta darajasi ko'rinishida yozsangiz, daraja ko'rsatkichi nechaga teng?", m + n


def ex_power_law_div(grade):
    # aᵐ ÷ aⁿ = aᵐ⁻ⁿ (m > n bo'lishi shart)
    base = random.randint(2, 5)
    n = random.randint(1, 4)
    m = random.randint(n + 1, n + 5)
    return f"{base}^{m} ÷ {base}^{n} ni {base} ning bitta darajasi ko'rinishida yozsangiz, daraja ko'rsatkichi nechaga teng?", m - n


def ex_power_law_value(grade):
    # Daraja qonunini qo'llab, YAKUNIY SON qiymatini hisoblash (kuchliroq)
    base = random.choice([2, 3])
    m = random.randint(1, 3)
    n = random.randint(1, 3)
    return f"{base}^{m} × {base}^{n} necha songa teng? (avval daraja qonunini qo'llang)", base ** (m + n)


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
    return f"√{a} = ?", int(math.sqrt(a))


def ex_sqrt_from_square(grade):
    lo, hi = _rng(grade, (5, 12), (13, 22), (18, 30))
    base = random.randint(lo, hi)
    return f"√{base*base} = ?", base


def ex_sqrt_area_to_side(grade):
    lo, hi = _rng(grade, (3, 10), (10, 20), (15, 28))
    side = random.randint(lo, hi)
    area = side * side
    return f"Yuzasi {area} bo'lgan kvadratning tomoni nechaga teng?", side


def ex_sqrt_product(grade):
    lo, hi = _rng(grade, (2, 6), (4, 12), (8, 18))
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    return f"√{a*a} × √{b*b} nechaga teng?", a * b


def ex_sqrt_estimate(grade):
    # To'liq kvadrat bo'lmagan son ikkita ketma-ket butun son orasida qayerda
    # joylashganini aniqlash - 9-sinf uchun mantiqiy baholash ko'nikmasi
    n = random.randint(4, 29)
    low_root = n
    n_squared_area = random.randint(low_root * low_root + 1, (low_root + 1) * (low_root + 1) - 1)
    return f"√{n_squared_area} soni qaysi ikkita ketma-ket butun son orasida joylashgan? Kichigini yozing.", low_root


def ex_sqrt_simplify(grade):
    # √(a²×b) = a√b ko'rinishida soddalashtirish (b - kvadratsiz son) -
    # 10-11 sinf uchun kuchliroq ildiz bilan ishlash ko'nikmasi
    a = random.randint(2, 10)
    b = random.choice([2, 3, 5, 6, 7, 10, 11, 13, 14, 15])
    n = a * a * b
    return f"√{n} sonini a√{b} ko'rinishida soddalashtiring. a nechaga teng?", a


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
    return f"{a}x − {b} = {c}, x = ?", x


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
    return f"x² = {x*x} (x > 0), x = ?", x


def _quad_text(r1, r2):
    b, c = -(r1 + r2), r1 * r2
    sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
    sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
    return f"x² {sign_b} {sign_c} = 0", b, c


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
    # Diskriminantni hisoblash - D = b² - 4ac (9-sinf uchun asosiy ko'nikma)
    a = random.randint(1, 3)
    r1, r2 = random.randint(1, 10), random.randint(1, 10)
    b = -a * (r1 + r2)
    c = a * r1 * r2
    d = b * b - 4 * a * c
    sign_b = f"+ {b}x" if b >= 0 else f"- {abs(b)}x"
    sign_c = f"+ {c}" if c >= 0 else f"- {abs(c)}"
    coef_a = f"{a}x²" if a != 1 else "x²"
    return f"{coef_a} {sign_b} {sign_c} = 0 tenglamaning diskriminanti (D = b² − 4ac) nechaga teng?", d


def ex_quad_sum_of_squares(grade):
    # x1² + x2² = (x1+x2)² - 2*x1*x2 ayniyati (kuchliroq, hard darajaga mos)
    r1, r2 = random.randint(1, 10), random.randint(1, 10)
    eq, b, c = _quad_text(r1, r2)
    return f"{eq} tenglama ildizlari x1 va x2 bo'lsa, x1² + x2² nechaga teng?", r1 * r1 + r2 * r2


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
    # Pifagor teoremasi - to'g'ri burchakli uchburchak (9-sinf geometriya)
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


def ex_circle_radius_from_circumference(grade):
    r = _circle_radius(grade)
    circ = int(2 * 22 * r / 7)
    return f"Aylana uzunligi {circ} bo'lgan doiraning radiusi nechaga teng? (π=22/7 deb oling)", r


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
    # Uch qismli nisbat - 9-sinf uchun kuchliroq
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
    # 9-sinf uchun teskari fikrlash talab qiladi
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
    return f"({a}) × ({b})" if b < 0 else f"({a}) × {b}", a * b


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
    return f"Havo harorati {start}°C edi. Kechqurun {delta}° ga {op}. Hozir harorat necha daraja?", result


def ex_negative_chain(grade):
    # Ko'p qadamli manfiy sonlar zanjiri - 9-sinf uchun kuchliroq
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
    text = " × ".join(f"({f})" if f < 0 else str(f) for f in factors)
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
    # Qarama-qarshi harakat - ikki obyekt bir-biriga tomon yuradi (9-sinf klassik masalasi)
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
    # Uchburchak burchaklari yig'indisi 180° - trigonometriyaga tayyorgarlik
    a1 = random.randint(20, 90)
    a2 = random.randint(20, 150 - a1)  # a1+a2 <= 150 bo'lgani uchun a3 >= 30 (har doim musbat)
    a3 = 180 - a1 - a2
    return f"Uchburchak burchaklaridan ikkitasi {a1}° va {a2}°. Uchinchi burchak nechaga teng?", a3


TRIG_EQUATION_FACTS = [
    ("sin", 0, 0), ("sin", 50, 30), ("sin", 100, 90),
    ("cos", 100, 0), ("cos", 50, 60), ("cos", 0, 90),
    ("tan", 0, 0), ("tan", 100, 45),
]


def ex_trig_equation(grade):
    # Oddiy trigonometrik tenglamani yechish (10-11 sinf) - qiymatlar foiz
    # ko'rinishida berilgani uchun natija ANIQ va butun son bo'ladi
    func, pct, angle = random.choice(TRIG_EQUATION_FACTS)
    return f"{func}(x°) = {pct}% tenglamani yeching (0° ≤ x ≤ 90°, sin(90°)=100% deb hisoblang). x = ?", angle


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
    return f"{base}^x × {base}^{k} = {base**n} tenglamani yeching. x = ?", x


def ex_expo_divide_rule(grade):
    # a^x / a^k = a^n ko'rinishi
    base = random.choice(EXPO_BASES)
    k = random.randint(1, 4)
    n = random.randint(1, 5)
    x = n + k
    return f"{base}^x ÷ {base}^{k} = {base**n} tenglamani yeching. x = ?", x


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
    # Cheksiz kamayuvchi geometrik progressiya yig'indisi: S = a1 ÷ (1 − q), |q| < 1.
    # Natija butun son chiqishi uchun q = 1/k va a1 = m×(k−1) qilib tanlanadi:
    # S = a1 ÷ (1 − 1/k) = a1×k ÷ (k−1) = m×(k−1)×k ÷ (k−1) = m×k
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
    # 5-sinf uchun javobni manfiy chiqarmaymiz.
    a = random.randint(120, 4500)
    b = random.randint(120, 2800)
    if random.choice([True, False]):
        c = random.randint(50, min(1600, a + b - 1))
        return f"{a} + {b} − {c} = ?", a + b - c
    # (a+b)-a-c = b-c, shuning uchun c < b bo'lishi shart.
    c = random.randint(50, b - 1)
    total = a + b
    return f"{total} − {a} − {c} = ?", total - a - c


def f5_add_sub_missing(grade):
    x = random.randint(50, 900)
    known = random.randint(20, x - 1)
    total = x + known
    return f"□ + {known} = {total}. □ o'rniga qaysi son keladi?", x


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
        return f"{a} × {b} = ?", a * b
    total = a * b
    return f"{total} ÷ {b} = ?", a


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
    return f"{a} × {b} + {c} = ? (avval ko'paytirish bajariladi)", result


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
    return f"{a}² = ?", a * a


def f5_power_cube(grade):
    a = random.randint(2, 10)
    return f"{a}³ = ?", a ** 3


def f5_power_missing_square(grade):
    a = random.randint(2, 20)
    return f"Qaysi sonning kvadrati {a*a} ga teng?", a


def f5_power_expression(grade):
    a = random.randint(2, 10)
    b = random.randint(2, 8)
    return f"{a}² + {b}² = ?", a * a + b * b


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
    return f"x − {b} = {x-b}. x = ?", x


def f5_eq_mul(grade):
    x = random.randint(2, 25)
    a = random.randint(2, 10)
    return f"{a} × x = {a*x}. x = ?", x


def f5_eq_div(grade):
    x = random.randint(2, 30)
    a = random.randint(2, 10)
    return f"x ÷ {a} = {x}. x = ?", x * a


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
    return f"To'g'ri to'rtburchak yuzasi {area} sm², bir tomoni {a} sm. Ikkinchi tomoni nechaga teng?", b


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
    "percent": "10% — sonning o'ndan biri, 50% — yarmi, 25% — choragi. Zarur bo'lsa foizni 100 ga bo'lib hisoblang.",
    "power": "Kvadrat — sonni o'ziga bir marta ko'paytirish; kub — sonni o'ziga ikki marta ko'paytirish.",
    "ratio": "Nisbatdagi bir qism qiymatini topib, kerakli qismlar soniga ko'paytiring.",
    "average": "Barcha sonlarni qo'shing va nechta son bo'lsa, shunga bo'ling.",
    "linear_eq": "Noma'lumni yolg'iz qoldirish uchun teskari amalni bajaring.",
    "triangle": "Perimetr — uchala tomon yig'indisi. Yuzasi = asos × balandlik ÷ 2.",
    "rectangle": "Perimetr = 2 × (uzunlik + eni), yuza = uzunlik × eni.",
    "speed": "Masofa = tezlik × vaqt; tezlik = masofa ÷ vaqt; vaqt = masofa ÷ tezlik.",
})

TOPIC_GENERATORS = {
    "add_sub": GEN_ADD_SUB,
    "mul_div": GEN_MUL_DIV,
    "percent": GEN_PERCENT,
    "fraction": GEN_FRACTION,
    "power": GEN_POWER,
        "linear_eq": GEN_LINEAR_EQ,
            "triangle": GEN_TRIANGLE,
    "rectangle": GEN_RECTANGLE,
    "circle": GEN_CIRCLE,
    "ratio": GEN_RATIO,
    "average": GEN_AVERAGE,
    "negative": GEN_NEGATIVE,
    "speed": GEN_SPEED,
            "log": GEN_LOG,
    "expo_eq": GEN_EXPO_EQ,
            "combinatorics": GEN_COMBINATORICS,
}


# ==================== 6-SINF YANGI GENERATORLARI ====================
# 6-sinf uchun 5-sinfdan alohida, mustaqil generatorlar.
# Bu generatorlar "sixth" daraja bilan ishlaydi va boshqa sinf generatorlariga
# bog'lanmaydi. Javoblar Telegramdagi raqamli variantlar bilan mos kelishi
# uchun butun son ko'rinishida beriladi.



# ==================== 6-SINF YANGI GENERATORLARI ====================
# Har bir generator bir nechta turli mazmunli vaziyatni tanlaydi.
# Javoblar formuladan hisoblanadi va imkon qadar butun/aniq natija beriladi.

def f6_add_sub_multistep(grade):
    scenario = random.randrange(4)
    if scenario == 0:
        a = random.randint(250, 900); b = random.randint(50, 250); c = random.randint(20, min(180, a+b))
        return f"Kutubxonada {a} ta kitob bor edi. {b} ta yangi kitob keltirildi, keyin {c} tasi o'quvchilarga berildi. Nechta kitob qoldi?", a+b-c
    if scenario == 1:
        a = random.randint(300, 900); b = random.randint(40, min(220, a)); c = random.randint(30, 180)
        return f"Fermer {a} kg olma terdi. {b} kg sotildi va {c} kg yana terildi. Hozir necha kg olma bor?", a-b+c
    if scenario == 2:
        a = random.randint(200, 800); b = random.randint(30, 180); c = random.randint(20, min(150, a-b))
        return f"Do'konga {a} dona daftar keldi. {b} tasi sotildi, keyin {c} tasi qayta olib kelindi. Do'konda nechta daftar bo'ldi?", a-b+c
    a = random.randint(300, 900); b = random.randint(40, 200); c = random.randint(30, 160)
    return f"Omborda {a} kg un bor edi. {b} kg ishlatildi va {c} kg boshqa omborga olib kelindi. Hozir necha kg un bor?", a-b+c

def f6_add_sub_order(grade):
    a,b,c = random.randint(12,60), random.randint(8,40), random.randint(2,9)
    if random.choice([True,False]):
        return f"({a} + {b}) × {c} = ?", (a+b)*c
    # add_sub mavzusi manfiy sonlar bilan shug'ullanmaydi (buning uchun alohida
    # "negative" mavzusi bor) - shuning uchun natija HAR DOIM manfiy bo'lmasligi
    # kerak. b ni a*c dan oshmaydigan qilib cheklaymiz.
    b = random.randint(1, a * c)
    return f"{a} × {c} − {b} = ?", a*c-b

def f6_mul_div(grade):
    scenario=random.randrange(3)
    if scenario==0:
        groups=random.randint(6,15); each=random.randint(8,25)
        return f"{groups} ta qutining har birida {each} tadan qalam bor. Jami nechta qalam?", groups*each
    if scenario==1:
        total=random.randint(8,20)*random.randint(6,18)
        groups=random.choice([2,3,4,5])
        total=(total//groups)*groups
        return f"{total} ta daftar {groups} ta sinfga teng taqsimlandi. Har bir sinfga nechta daftar tegdi?", total//groups
    boxes=random.randint(5,12); each=random.randint(7,20); taken=random.randint(1,boxes-1)
    return f"{boxes} qutining har birida {each} tadan shar bor. {taken} quti olib ketildi. Nechta shar qoldi?", (boxes-taken)*each

def f6_divisibility(grade):
    d=random.choice([2,3,5,9,10])
    if d==2: n=random.randint(10,500)//2*2
    elif d==5: n=random.randint(2,100)*5
    elif d==10: n=random.randint(2,60)*10
    else: n=random.randint(3,80)*d
    return f"{n} soni {d} ga qoldiqsiz bo'linadimi? Ha bo'lsa 1, yo'q bo'lsa 0.", 1

def f6_divisibility_no(grade):
    d=random.choice([2,3,5,9,10])
    n=random.randint(10,300)
    while n%d==0: n+=1
    return f"{n} soni {d} ga qoldiqsiz bo'linadimi? Ha bo'lsa 1, yo'q bo'lsa 0.", 0

def f6_divisibility_counterexample(grade):
    d=random.choice([2,3,5,9,10])
    n=random.randint(10,500)
    while n%d==0: n+=random.randint(1,3)
    return f"{n} sonini {d} ga bo'lganda qoldiq nechaga teng?", n%d

def f6_prime_check(grade):
    primes=[11,13,17,19,23,29,31,37,41,43,47,53,59,61,67,71,73,79,83,89,97]
    composites=[12,14,15,16,18,20,21,22,24,25,26,27,28,30,32,33,34,35,36,38,39,40]
    n=random.choice(primes+composites)
    return f"{n} soni tubmi? Ha bo'lsa 1, yo'q bo'lsa 0.", 1 if n in primes else 0

def f6_prime_factor(grade):
    p=random.choice([2,3,5,7]); q=random.choice([2,3,5,7,11])
    n=p*q
    return f"{n} sonining eng kichik tub bo'luvchisini toping.", min(p,q)

def f6_gcd(grade):
    import math
    a=random.randint(4,18); b=random.randint(4,18); k=random.randint(2,6)
    a*=k; b*=k
    return f"{a} va {b} sonlarining EKUBini toping.", math.gcd(a,b)

def f6_lcm(grade):
    import math
    a=random.randint(3,12); b=random.randint(3,12)
    return f"{a} va {b} sonlarining EKUKini toping.", abs(a*b)//math.gcd(a,b)

def f6_decimal_add(grade):
    wa=random.randint(10,90); wb=random.randint(10,90); d=random.randint(1,9)
    a=wa+d/10; b=wb+(10-d)/10
    return f"{a:g} + {b:g} = ?", wa+wb+1

def f6_decimal_sub(grade):
    wa=random.randint(30,90); wb=random.randint(5,29); digit=random.randint(1,9)
    a=wa+digit/10; b=wb+digit/10
    return f"{a:g} − {b:g} = ?", wa-wb

def f6_decimal_mul10(grade):
    whole=random.randint(2,99); digit=random.randint(1,9)
    a=whole+digit/10
    return f"{a:g} × 10 = ?", whole*10+digit

def f6_fraction_of_number(grade):
    d=random.choice([2,3,4,5,6,8,10]); n=random.randint(1,d-1); k=random.randint(4,25); total=d*k
    return f"{total} ning {n}/{d} qismini toping.", total*n//d

def f6_fraction_add_same(grade):
    d=random.choice([5,6,8,10,12]); a=random.randint(1,d-2); b=random.randint(1,d-a-1)
    return f"{a}/{d} + {b}/{d} = ? (suratni toping)", a+b

def f6_fraction_compare(grade):
    d=random.choice([5,6,7,8,9,10]); a=random.randint(1,d-2); b=random.randint(a+1,d-1)
    return f"Qaysi kasr katta: {a}/{d} yoki {b}/{d}? Katta kasrning suratini tanlang.", b

def f6_fraction_word(grade):
    d=random.choice([3,4,5,6,8]); n=random.randint(1,d-1); total=d*random.randint(4,20)
    taken=total*n//d
    return f"Bir sinfda {total} ta daftar bor. Ularning {n}/{d} qismi yig'ib olindi. Nechta daftar yig'ildi?", taken

def f6_mixed_to_improper(grade):
    whole=random.randint(1,9); d=random.choice([2,3,4,5,6,8]); n=random.randint(1,d-1)
    return f"{whole} {n}/{d} aralash sonini noto'g'ri kasrga aylantirganda surat nechaga teng?", whole*d+n

def f6_percent_direct(grade):
    base=random.choice([80,120,160,200,240,300,400,500]); pct=random.choice([10,15,20,25,30,40,50])
    # 15% uchun ham aniq natija beradigan bazalar tanlangan
    return f"{base} ning {pct}% ini toping.", base*pct//100

def f6_percent_reverse(grade):
    pct=random.choice([10,20,25,50]); whole=random.choice([80,100,120,160,200,240,400]); part=whole*pct//100
    return f"{part} soni {whole} ning necha foizini tashkil qiladi? Foiz sonini toping.", pct

def f6_ratio_simplify(grade):
    import math
    k=random.randint(2,9); a=random.randint(2,12)*k; b=random.randint(2,12)*k; g=math.gcd(a,b)
    return f"{a}:{b} nisbatini eng sodda ko'rinishga keltirganda birinchi son nechaga teng?", a//g

def f6_ratio_split(grade):
    p,q=random.choice([(2,3),(3,4),(2,5),(3,5)]); unit=random.randint(4,25); total=(p+q)*unit
    return f"{total} so'm {p}:{q} nisbatda ikki kishiga bo'lindi. Birinchi kishi qancha oldi?", p*unit

def f6_proportion(grade):
    a,b,x=random.randint(2,9),random.randint(2,9),random.randint(2,12)
    c=a*x
    if c%b: x=b*random.randint(2,8); c=a*x
    return f"{a}:{b} = {c}:x. x ni toping.", x

def f6_negative_add(grade):
    a=random.randint(-20,-3); b=random.randint(-15,20)
    return f"({a}) + ({b}) = ?", a+b

def f6_negative_sub(grade):
    a=random.randint(-15,20); b=random.randint(-15,20)
    return f"({a}) − ({b}) = ?", a-b

def f6_negative_temperature(grade):
    start=random.randint(-15,5); change=random.randint(2,12)
    if random.choice([True,False]):
        return f"Ertalab harorat {start}°C edi, kunduzi {change}° ga ko'tarildi. Hozir necha °C?", start+change
    return f"Ertalab harorat {start}°C edi, kechqurun {change}° ga pasaydi. Hozir necha °C?", start-change

def f6_power(grade):
    a=random.randint(2,10); n=random.choice([2,3])
    return f"{a}^{n} ni hisoblang.", a**n

def f6_power_product(grade):
    a=random.randint(2,9); n=random.randint(2,5)
    return f"{a}^{n} = ?", a**n

def f6_coordinate_move(grade):
    x=random.randint(-10,10); step=random.randint(2,8); direction=random.choice(["o'ngga","chapga"])
    ans=x+step if direction=="o'ngga" else x-step
    return f"Sonlar o'qida {x} nuqtadan {step} birlik {direction} yurildi. Yangi koordinata?", ans

def f6_coordinate_distance(grade):
    a=random.randint(-12,-1); b=random.randint(1,12)
    return f"{a} va {b} nuqtalar orasidagi masofani toping.", b-a

def f6_expression(grade):
    a=random.randint(2,9); b=random.randint(2,9); x=random.randint(2,10)
    return f"{a}x + {b} ifodasining x={x} dagi qiymatini toping.", a*x+b

def f6_expression_parentheses(grade):
    a=random.randint(2,9); b=random.randint(2,9); c=random.randint(2,9)
    return f"{a}({b} + {c}) − {a} = ?", a*(b+c)-a

def f6_linear_eq(grade):
    a=random.randint(2,9); x=random.randint(2,20); b=random.randint(1,30); c=a*x+b
    return f"{a}x + {b} = {c}. x ni toping.", x

def f6_linear_eq_sub(grade):
    a=random.randint(2,9); x=random.randint(2,20); b=random.randint(1,a*x-1); c=a*x-b
    return f"{a}x − {b} = {c}. x ni toping.", x

def f6_triangle_perimeter(grade):
    # Uchburchak tengsizliklari avtomatik bajariladi.
    a=random.randint(5,18); b=random.randint(5,18); c=random.randint(abs(a-b)+1,a+b-1)
    return f"Tomonlari {a} sm, {b} sm va {c} sm bo'lgan uchburchak perimetri?", a+b+c

def f6_triangle_area(grade):
    base=random.randint(4,20); height=random.randint(2,20)
    if base*height%2: height+=1
    return f"Asosi {base} sm, balandligi {height} sm bo'lgan uchburchak yuzasi?", base*height//2

def f6_rectangle_area(grade):
    a=random.randint(5,30); b=random.randint(4,25)
    if random.choice([True,False]):
        return f"Bog'ning bo'yi {a} m, eni {b} m. Uning yuzasi nechaga teng?", a*b
    return f"Tomonlari {a} sm va {b} sm bo'lgan to'g'ri to'rtburchakning perimetri nechaga teng?", 2*(a+b)

def f6_rectangle_missing(grade):
    a=random.randint(5,30); b=random.randint(4,25); p=2*(a+b)
    return f"To'g'ri to'rtburchak perimetri {p} sm, bir tomoni {a} sm. Ikkinchi tomoni?", b

def f6_circle_area(grade):
    r=random.choice([7,14,21,28])
    # 22/7 tanlovi bilan natija butun son chiqadi.
    return f"Radiusi {r} sm bo'lgan doira yuzini π=22/7 deb olib toping.", 22*r*r//7

def f6_circle_diameter(grade):
    d=random.choice([6,8,10,12,14,16,20])
    return f"Doiraning diametri {d} sm. Radiusi nechaga teng?", d//2

def f6_speed_distance(grade):
    speed=random.randint(20,80); time=random.randint(2,6)
    return f"Avtobus {speed} km/soat tezlikda {time} soat yurdi. Qancha masofa bosib o'tdi?", speed*time

def f6_speed_time(grade):
    speed=random.choice([30,40,50,60,70,80]); time=random.randint(2,6); distance=speed*time
    return f"{distance} km yo'l {speed} km/soat tezlikda bosib o'tildi. Yo'lga qancha vaqt ketdi?", time

def f6_average(grade):
    avg=random.randint(15,70)
    offsets=random.sample([-6,-4,-2,2,4,6],3)
    a,b,c=avg+offsets[0],avg+offsets[1],avg+offsets[2]
    d=4*avg-a-b-c
    return f"{a}, {b}, {c}, {d} sonlarining o'rtacha arifmetik qiymati?", avg

GEN_F6_ADD_SUB=[(f6_add_sub_multistep,{"sixth"}),(f6_add_sub_order,{"sixth"})]
GEN_F6_DIVISIBILITY=[(f6_divisibility,{"sixth"}),(f6_divisibility_no,{"sixth"}),(f6_divisibility_counterexample,{"sixth"})]
GEN_F6_PRIME=[(f6_prime_check,{"sixth"}),(f6_prime_factor,{"sixth"})]
GEN_F6_GCD_LCM=[(f6_gcd,{"sixth"}),(f6_lcm,{"sixth"})]
GEN_F6_DECIMAL=[(f6_decimal_add,{"sixth"}),(f6_decimal_sub,{"sixth"}),(f6_decimal_mul10,{"sixth"})]
GEN_F6_FRACTION=[(f6_fraction_of_number,{"sixth"}),(f6_fraction_add_same,{"sixth"}),(f6_fraction_compare,{"sixth"}),(f6_fraction_word,{"sixth"})]
GEN_F6_MIXED_FRACTION=[(f6_mixed_to_improper,{"sixth"})]
GEN_F6_PERCENT=[(f6_percent_direct,{"sixth"}),(f6_percent_reverse,{"sixth"})]
GEN_F6_RATIO=[(f6_ratio_simplify,{"sixth"}),(f6_ratio_split,{"sixth"})]
GEN_F6_PROPORTION=[(f6_proportion,{"sixth"})]
GEN_F6_NEGATIVE=[(f6_negative_add,{"sixth"}),(f6_negative_sub,{"sixth"}),(f6_negative_temperature,{"sixth"})]
GEN_F6_POWER=[(f6_power,{"sixth"}),(f6_power_product,{"sixth"})]
GEN_F6_COORDINATE=[(f6_coordinate_move,{"sixth"}),(f6_coordinate_distance,{"sixth"})]
GEN_F6_EXPRESSION=[(f6_expression,{"sixth"}),(f6_expression_parentheses,{"sixth"})]
GEN_F6_LINEAR=[(f6_linear_eq,{"sixth"}),(f6_linear_eq_sub,{"sixth"})]
GEN_F6_TRIANGLE=[(f6_triangle_perimeter,{"sixth"}),(f6_triangle_area,{"sixth"})]
GEN_F6_RECTANGLE=[(f6_rectangle_area,{"sixth"}),(f6_rectangle_missing,{"sixth"})]
GEN_F6_CIRCLE=[(f6_circle_area,{"sixth"}),(f6_circle_diameter,{"sixth"})]
GEN_F6_SPEED=[(f6_speed_distance,{"sixth"}),(f6_speed_time,{"sixth"})]
GEN_F6_AVERAGE=[(f6_average,{"sixth"})]

# 6-sinf mavzulari va formulalari.
TOPICS.update({
    "divisibility":"🔢 Bo'linish belgilari","prime_numbers":"🔹 Tub va murakkab sonlar",
    "gcd_lcm":"🔗 EKUB va EKUK","decimal":"🔟 O'nli kasrlar","mixed_fraction":"½ Aralash kasrlar",
    "proportion":"⚖️ Proporsiya","coordinates":"📍 Koordinata o'qi","expression":"🔤 Algebraik ifodalar",
})
GRADE_LABELS["sixth"]="🔵 6-sinf"
GRADE_TOPICS["sixth"]=["add_sub","mul_div","divisibility","prime_numbers","gcd_lcm","decimal","fraction",
"mixed_fraction","percent","ratio","proportion","negative","power","coordinates","expression","linear_eq",
"triangle","rectangle","circle","speed","average"]

HINTS.update({
"divisibility":"2 ga: oxirgi raqam juft; 5 ga: 0 yoki 5; 10 ga: 0; 3 va 9 ga: raqamlar yig'indisini tekshiring.",
"prime_numbers":"Tub sonning aynan 2 ta musbat bo'luvchisi bor: 1 va o'zi. 1 tub ham, murakkab ham emas.",
"gcd_lcm":"EKUB — eng katta umumiy bo'luvchi. EKUK — eng kichik umumiy karrali.",
"decimal":"Vergullarni bir ustunga tekislang; 10 ga ko'paytirishda vergul bir xona o'ngga, bo'lishda chapga siljiydi.",
"mixed_fraction":"a b/c = (a×c+b)/c.",
"proportion":"a:b=c:d bo'lsa, a×d=b×c.",
"coordinates":"O'ngga yurilganda koordinata ortadi, chapga yurilganda kamayadi. Masofa |a-b|.",
"expression":"Avval qavs, keyin ko'paytirish/bo'lish, so'ng qo'shish/ayirish bajariladi."
})
FORMULAS.update({
"divisibility":"🔢 BO'LINISH BELGILARI (formula kitobi asosida)\n\n• 2 ga: oxirgi raqami 0,2,4,6,8 bo'lsa\n• 3 ga: raqamlar yig'indisi 3 ga bo'linsa\n• 4 ga: oxirgi ikkita raqami 0 yoki 4 ga bo'linsa\n• 5 ga: oxirgi raqami 0 yoki 5 bo'lsa\n• 6 ga: son ham 2 ga, ham 3 ga bo'linsa\n• 9 ga: raqamlar yig'indisi 9 ga bo'linsa\n• 10 ga: oxirgi raqami 0 bo'lsa\n• 25 ga: oxirgi ikkita raqami 0 yoki 25 ga bo'linsa",
"prime_numbers":"🔹 TUB VA MURAKKAB SONLAR (formula kitobi asosida)\n\n• Tub son — faqat 1 ga va o'ziga bo'linadigan, 1 dan katta son (2,3,5,7,11,...)\n• Murakkab son — 2 tadan ko'p bo'luvchiga ega son\n• 1 — tub ham, murakkab ham emas\n• O'zaro tub sonlar — 1 dan boshqa umumiy bo'luvchiga ega bo'lmagan sonlar (masalan 16 va 27)",
"gcd_lcm":"🔗 EKUB VA EKUK (formula kitobi asosida)\n\n• EKUB(a,b) — eng katta umumiy bo'luvchi\n• EKUK(a,b) — eng kichik umumiy karrali\n• EKUB(a,b) × EKUK(a,b) = a × b\n• Topish: sonlarni tub ko'paytuvchilarga ajratib, EKUB uchun umumiy ko'paytuvchilarni eng kichik darajada, EKUK uchun eng katta darajada olinadi\n• Kasrlar uchun: EKUB(a/m,b/n)=EKUB(a,b)/EKUK(m,n); EKUK(a/m,b/n)=EKUK(a,b)/EKUB(m,n)",
"decimal":"🔟 O'NLI KASRLAR (formula kitobi asosida)\n\n• Qo'shish/ayirishda vergullarni tekislang\n• ×10ⁿ — vergul n xona o'ngga suriladi\n• ÷10ⁿ — vergul n xona chapga suriladi\n• Ko'paytirishda vergullardan keyingi raqamlar soni qo'shiladi\n• Bo'lishda bo'linuvchi va bo'luvchining vergulini bir xil xonaga suriladi",
"mixed_fraction":"½ ARALASH KASR (formula kitobi asosida)\n\n• a b/c = (a×c+b)/c (aralashdan oddiy kasrga)\n• Aralash sonlarni qo'shish/ayirishda avval butun, keyin kasr qismlar ustida amal bajariladi",
"proportion":"⚖️ PROPORSIYA (formula kitobi asosida)\n\n• a:b=c:d bo'lsa, a×d=b×c (proportsiyaning asosiy xossasi)\n• a:b=c:d bo'lsa, b:a=d:c va a:c=b:d ham to'g'ri",
"coordinates":"📍 KOORDINATA O'QI (formula kitobi asosida)\n\n• O'ngga siljish — koordinata ortadi\n• Chapga siljish — koordinata kamayadi\n• Ikki nuqta orasidagi masofa: AB = |a−b|",
"expression":"🔤 ALGEBRAIK IFODALAR (formula kitobi asosida)\n\n• Harf o'rniga qiymatni qo'ying\n• Amallar tartibi: qavs → daraja/ildiz → ko'paytirish/bo'lish → qo'shish/ayirish"
})

TOPIC_GENERATORS.update({
"add_sub":GEN_ADD_SUB+GEN_F6_ADD_SUB,"mul_div":GEN_MUL_DIV+[(f6_mul_div,{"sixth"})],
"divisibility":GEN_F6_DIVISIBILITY,"prime_numbers":GEN_F6_PRIME,"gcd_lcm":GEN_F6_GCD_LCM,
"decimal":GEN_F6_DECIMAL,"fraction":GEN_FRACTION+GEN_F6_FRACTION,"mixed_fraction":GEN_F6_MIXED_FRACTION,
"percent":GEN_PERCENT+GEN_F6_PERCENT,"ratio":GEN_RATIO+GEN_F6_RATIO,"proportion":GEN_F6_PROPORTION,
"negative":GEN_NEGATIVE+GEN_F6_NEGATIVE,"power":GEN_POWER+GEN_F6_POWER,"coordinates":GEN_F6_COORDINATE,
"expression":GEN_F6_EXPRESSION,"linear_eq":GEN_LINEAR_EQ+GEN_F6_LINEAR,"triangle":GEN_TRIANGLE+GEN_F6_TRIANGLE,
"rectangle":GEN_RECTANGLE+GEN_F6_RECTANGLE,"circle":GEN_CIRCLE+GEN_F6_CIRCLE,"speed":GEN_SPEED+GEN_F6_SPEED,
"average":GEN_AVERAGE+GEN_F6_AVERAGE
})


# ==================== 7-SINF PROFESSIONAL GENERATORLARI ====================

def f7_int(grade):
    t=random.randrange(4); a=random.randint(4,20); b=random.randint(2,12); c=random.randint(2,9)
    if t==0: return f"−{a} + {b} × {c} = ?", -a+b*c
    if t==1: return f"({a} − {b}) × {c} = ?", (a-b)*c
    if t==2: return f"{a} × ({b} − {c}) = ?", a*(b-c)
    return f"−({a} + {b}) + {c} = ?", -(a+b)+c

def f7_int_word(grade):
    t=random.randrange(4)
    if t==0:
        s=random.randint(-8,8); d=random.randint(3,12)
        return f"Harorat {s}°C edi. Kechasi {d}°C ga pasaydi. Yangi harorat?", s-d
    if t==1:
        f=random.randint(-3,4); up=random.randint(4,10); down=random.randint(2,7)
        return f"Liftda {f}-qavatdan {up} qavat yuqoriga, keyin {down} qavat pastga tushildi. Qaysi qavatda to'xtaldi?", f+up-down
    if t==2:
        bal=random.randint(-20,20); inc=random.randint(10,40); exp=random.randint(5,30)
        return f"Hisobdagi balans {bal} ming so'm edi. {inc} ming tushdi va {exp} ming sarflandi. Yangi balans?", bal+inc-exp
    s=random.randint(-10,10); d=random.randint(2,8)
    return f"Sonlar o'qida {s} nuqtadan {d} birlik chapga yurildi. Yangi koordinata?", s-d

def f7_frac(grade):
    # Javoblar butun son bo'lishi uchun kasrli amallarning natijasi oldindan nazorat qilinadi.
    t=random.randrange(4)
    if t==0:
        den=random.choice([2,3,4,5,6,8])
        result=random.randint(2,10)
        a=random.randint(1,den*result-1)
        b=den*result-a
        return f"{a}/{den} + {b}/{den} = ?", result
    if t==1:
        den=random.choice([2,3,4,5,6,8])
        result=random.randint(1,8)
        b=random.randint(1,den*result-1)
        a=den*result+b
        return f"{a}/{den} − {b}/{den} = ?", result
    if t==2:
        n=random.randint(20,100); den=random.choice([2,3,4,5,10])
        n += (-n)%den; num=random.randint(1,den-1)
        return f"{n} sonining {num}/{den} qismi nechaga teng?", n*num//den
    den=random.choice([2,3,4,5,6])
    n=random.randint(2,10)
    return f"{n} : 1/{den} = ?", n*den

def f7_expr(grade):
    t=random.randrange(4); x=random.randint(2,12); b=random.randint(1,15)
    if t==0: return f"x={x} bo'lsa, 3x+{b} qiymatini toping.", 3*x+b
    if t==1: return f"a={x} bo'lsa, 5a−{b} qiymatini toping.", 5*x-b
    if t==2: return f"y={x} bo'lsa, 2(y+{b}) qiymatini toping.", 2*(x+b)
    return f"m={x} bo'lsa, 4m−2(m−{b}) qiymatini toping.", 4*x-2*(x-b)

def f7_like(grade):
    a=random.randint(2,12); b=random.randint(1,10); c=random.randint(1,8); t=random.randrange(3)
    if t==0: return f"{a}x + {b}x − {c}x. x oldidagi koeffitsiyent?", a+b-c
    if t==1: return f"{a}a − {b}a + {c}a. a oldidagi koeffitsiyent?", a-b+c
    return f"{a}y + {b} − {c}y. y oldidagi koeffitsiyent?", a-c

def f7_monomial(grade):
    a=random.randint(2,9); b=random.randint(2,9); t=random.randrange(3)
    if t==0: return f"{a}x × {b}x ning x² oldidagi koeffitsiyenti?", a*b
    if t==1: return f"{a}a² × {b}a ning a³ oldidagi koeffitsiyenti?", a*b
    return f"{a}x × {b}y ko'paytmasining sonli koeffitsiyenti?", a*b

def f7_identity(grade):
    a=random.randint(2,9); b=random.randint(1,8); t=random.randrange(3)
    if t==0: return f"({a}+{b})² − {a}² − {b}² = ?", 2*a*b
    if t==1: return f"{a}² − 2×{a}×{b} + {b}² ifodadagi 2ab qiymati?", 2*a*b
    return f"({a}−{b})² ni ochganda o'rta hadning modul qiymati?", 2*a*b

def f7_eq(grade):
    a=random.randint(2,9); x=random.randint(2,18); t=random.randrange(3)
    if t==0:
        b=random.randint(1,20); return f"{a}x+{b}={a*x+b}. x ni toping.", x
    if t==1:
        b=random.randint(1,a*x-1); return f"{a}x−{b}={a*x-b}. x ni toping.", x
    return f"{a}x={a*x}. x ni toping.", x

def f7_eq_word(grade):
    t=random.randrange(3); x=random.randint(6,25)
    if t==0: return f"Bir sonning 4 baravariga 7 qo'shilsa {4*x+7} chiqadi. Sonni toping.", x
    if t==1: return f"Bir sondan 9 ayirilganda {x-9} hosil bo'ldi. Sonni toping.", x
    return f"Ketma-ket uchta natural sonning o'rtadagi hadi {x}. Ularning yig'indisi?", 3*x

def f7_ineq(grade):
    a=random.randint(2,8); lim=random.randint(a+1,8*a); ans=(lim-1)//a
    return f"{a}x < {lim} tengsizlikni qanoatlantiruvchi eng katta natural x?", ans

def f7_func(grade):
    k=random.choice([2,3,4,5]); x=random.randint(-6,10)
    return f"y={k}x funksiyada x={x} bo'lsa, y?", k*x

def f7_func_reverse(grade):
    k=random.choice([2,3,4,5]); x=random.randint(-5,10); y=k*x
    return f"y={k}x funksiyada y={y} bo'lsa, x?", x

def f7_coord(grade):
    x=random.randint(-8,8); d=random.randint(2,9); right=random.choice([True,False])
    return f"A({x}) nuqtadan {d} birlik {'o‘ngga' if right else 'chapga'} o'tilsa yangi koordinata?", x+(d if right else -d)

def f7_distance(grade):
    a=random.randint(-12,12); b=random.randint(-12,12)
    while a==b: b=random.randint(-12,12)
    return f"A({a}) va B({b}) nuqtalar orasidagi masofa?", abs(a-b)

def f7_angles(grade):
    t=random.randrange(3); a=random.randint(25,155)
    if t==0: return f"Yonma-yon burchaklardan biri {a}°. Ikkinchisi?", 180-a
    if t==1: return f"Qarama-qarshi burchaklardan biri {a}°. Ikkinchisi?", a
    return f"Parallel chiziqlarda mos burchaklardan biri {a}°. Ikkinchisi?", a

def f7_tri_angles(grade):
    a=random.randint(35,80); b=random.randint(35,min(80,144-a))
    return f"Uchburchakning ikki burchagi {a}° va {b}°. Uchinchi burchak?", 180-a-b

def f7_isosceles(grade):
    base=random.randint(20,85)
    return f"Teng yonli uchburchakning asosidagi burchaklar {base}° dan. Tepa burchagi?", 180-2*base

def f7_tri_side(grade):
    a=random.randint(6,15); b=random.randint(6,15)
    return f"Tomonlari {a} sm va {b} sm bo'lgan uchburchakning uchinchi tomoni butun son. Eng kichik qiymati?", abs(a-b)+1

def f7_tri_perim(grade):
    a=random.randint(5,18); b=random.randint(5,18); c=random.randint(abs(a-b)+1,a+b-1)
    return f"Uchburchak tomonlari {a}, {b}, {c} sm. Perimetri?", a+b+c

def f7_rect(grade):
    t=random.randrange(3)
    a=random.randint(5,30); b=random.randint(4,25)
    if t==0:
        return f"To'g'ri to'rtburchakning tomonlari {a} sm va {b} sm. Perimetri?", 2*(a+b)
    if t==1:
        return f"To'g'ri to'rtburchakning tomonlari {a} sm va {b} sm. Yuzasi?", a*b
    p=2*(a+b)
    return f"Perimetri {p} sm bo'lgan to'g'ri to'rtburchakning bir tomoni {a} sm. Ikkinchi tomoni?", b

def f7_para(grade):
    a=random.randint(6,20); b=random.randint(4,15)
    return f"Parallelogramm tomonlari {a} sm va {b} sm. Perimetri?", 2*(a+b)

def f7_area(grade):
    base=random.randint(6,24); h=random.randint(2,20)
    if base*h%2: h+=1
    return f"Uchburchak asosi {base} sm, balandligi {h} sm. Yuzasi?", base*h//2

def f7_ratio(grade):
    unit=random.randint(3,12); p=random.randint(2,5); q=random.randint(3,7)
    total=unit*(p+q)
    return f"{total} ta kitob ikki javonga {p}:{q} nisbatda joylandi. Birinchi javonda nechta?", unit*p

def f7_percent(grade):
    base=random.choice([50,80,100,120,150,200,250,300]); pct=random.choice([10,20,25,30,40]); ch=base*pct//100
    if random.choice([True,False]): return f"{base} soni {pct}% ga oshirildi. Yangi qiymat?", base+ch
    return f"{base} soni {pct}% ga kamaytirildi. Yangi qiymat?", base-ch

def f7_sequence(grade):
    a=random.randint(2,20); d=random.randint(2,8); n=random.randint(5,12)
    return f"Arifmetik ketma-ketlik {a}, {a+d}, {a+2*d}, ... . {n}-hadi?", a+(n-1)*d

def f7_sequence_missing(grade):
    a=random.randint(2,15); d=random.randint(2,7); n=random.randint(6,9)
    vals=[a+i*d for i in range(n)]; idx=random.randint(1,n-2); shown=vals[:]; shown[idx]="?"
    return f"Ketma-ketlik: {', '.join(map(str,shown))}. ? o'rniga qaysi son keladi?", vals[idx]

def f7_combinatorics(grade):
    a=random.randint(2,10); b=random.randint(2,10); t=random.randrange(4)
    if t==0: return f"{a} xil ko'ylak va {b} xil shimdan bittadan tanlashning nechta usuli bor?", a*b
    if t==1: return f"{a} xil daftar muqovasi va {b} xil rangdagi ruchkadan bittadan tanlashning nechta usuli bor?", a*b
    if t==2: return f"{a} xil taom va {b} xil ichimlikdan bittadan tanlashning nechta usuli bor?", a*b
    return f"{a} xil kitob va {b} xil xatcho'pdan bittadan tanlashning nechta usuli bor?", a*b

def f7_logic(grade):
    t=random.randrange(4)
    if t==0:
        x=random.randint(4,20); return f"Ketma-ket uchta natural sonning o'rtadagi hadi {x}. Ularning yig'indisi?", 3*x
    if t==1:
        x=random.randint(5,25); return f"Ikki sonning yig'indisi {2*x+7}, ayirmasi 7. Katta son?", x+7
    if t==2:
        s=random.randint(30,80); first=random.randint(8,25); second=s-first
        return f"Ikki qutida jami {s} ta qalam bor. Birinchi qutida {first} ta. Ikkinchisida nechta?", second
    side=random.randint(5,18); p=4*side
    return f"Kvadrat perimetri {p} sm. Har bir tomon 2 sm ga oshirilsa, yangi perimetr?", p+8

def f7_probability(grade):
    # Bir nechta nisbat va masshtablar ishlatiladi; savollar tarixda uzoq takrorlanmaydi.
    ratios=[(1,2),(1,4),(1,5),(1,10),(2,5),(3,5),(3,10),(4,5),(1,20),(2,10),(5,10)]
    good,total=random.choice(ratios)
    scale=random.randint(1,12)
    good*=scale; total*=scale
    return f"Qutida {good} ta qizil va {total-good} ta ko'k shar bor. Bitta shar olinsa, qizil chiqish ehtimoli necha foiz?", 100*good//total

GEN_F7_INTEGER=[(f7_int,{"seventh"}),(f7_int_word,{"seventh"})]
GEN_F7_FRACTION=[(f7_frac,{"seventh"})]
GEN_F7_EXPR=[(f7_expr,{"seventh"}),(f7_like,{"seventh"})]
GEN_F7_MONOMIAL=[(f7_monomial,{"seventh"})]
GEN_F7_IDENTITY=[(f7_identity,{"seventh"})]
GEN_F7_LINEAR=[(f7_eq,{"seventh"}),(f7_eq_word,{"seventh"})]
GEN_F7_INEQ=[(f7_ineq,{"seventh"})]
GEN_F7_FUNCTION=[(f7_func,{"seventh"}),(f7_func_reverse,{"seventh"})]
GEN_F7_COORD=[(f7_coord,{"seventh"}),(f7_distance,{"seventh"})]
GEN_F7_ANGLES=[(f7_angles,{"seventh"})]
GEN_F7_TRIANGLE=[(f7_tri_angles,{"seventh"}),(f7_isosceles,{"seventh"}),(f7_tri_side,{"seventh"}),(f7_tri_perim,{"seventh"})]
GEN_F7_GEOMETRY=[(f7_rect,{"seventh"}),(f7_para,{"seventh"}),(f7_area,{"seventh"})]
GEN_F7_RATIO=[(f7_ratio,{"seventh"}),(f7_percent,{"seventh"})]
GEN_F7_SEQUENCE=[(f7_sequence,{"seventh"}),(f7_sequence_missing,{"seventh"})]
GEN_F7_COMBINATORICS=[(f7_combinatorics,{"seventh"})]
GEN_F7_LOGIC=[(f7_logic,{"seventh"})]
GEN_F7_PROBABILITY=[(f7_probability,{"seventh"})]

TOPICS.update({
"integer7":"➖ Butun sonlar va amallar","fraction7":"½ Ratsional sonlar",
"algebra_value7":"🔤 Algebraik ifodalar","monomial7":"✖️ Birhadlar",
"identity7":"🧩 Ayniyatlar","linear7":"📐 Chiziqli tenglamalar",
"inequality7":"⚖️ Chiziqli tengsizliklar","function7":"📈 Chiziqli funksiya",
"coordinate7":"📍 Koordinata o'qi","angles7":"📐 Burchaklar va parallel chiziqlar",
"triangle7":"🔺 Uchburchaklar","geometry7":"▱ To'rtburchaklar va yuzalar",
"ratio_percent7":"⚖️ Nisbat va foiz","sequence7":"🔢 Arifmetik ketma-ketliklar",
"combinatorics7":"🎲 Sodda kombinatorika","logic7":"🧠 Mantiqiy masalalar",
"probability7":"🎯 Sodda ehtimollik"
})
GRADE_LABELS["seventh"]="🟠 7-sinf"
GRADE_TOPICS["seventh"]=[
"integer7","fraction7","algebra_value7","monomial7","identity7","linear7",
"inequality7","function7","coordinate7","angles7","triangle7","geometry7",
"ratio_percent7","sequence7","combinatorics7","logic7","probability7"
]
HINTS.update({
"integer7":"Amallar tartibiga rioya qiling. Manfiy sonlarda sonlar o'qidan foydalaning.",
"fraction7":"Bir xil maxrajli kasrlarda suratlar ustida amal bajariladi. Sonning a/b qismini topish: son÷b×a.",
"algebra_value7":"Harf o'rniga berilgan qiymatni qo'yib, amallar tartibida hisoblang.",
"monomial7":"Birhadlarni ko'paytirishda koeffitsiyentlar ko'payadi, bir xil harflarning darajalari qo'shiladi.",
"identity7":"(a+b)²=a²+2ab+b² va (a−b)²=a²−2ab+b² formulalaridan foydalaning.",
"linear7":"Noma'lumni yolg'iz qoldirish uchun teskari amallarni ketma-ket bajaring.",
"inequality7":"Tengsizlikni yeching va natural sonlar ichidan eng katta mos qiymatni tanlang.",
"function7":"y=kx bo'lsa, berilgan x yoki y ni formulaga qo'ying.",
"coordinate7":"O'ngga — koordinata ortadi, chapga — kamayadi. Masofa |x₂−x₁|.",
"angles7":"Yonma-yon burchaklar yig'indisi 180°, qarama-qarshi va mos burchaklar teng.",
"triangle7":"Uchburchak burchaklari yig'indisi 180°. Uchburchak tengsizligi: |a−b|<c<a+b.",
"geometry7":"P va S formulalaridan foydalaning; noma'lum tomonni perimetr shartidan toping.",
"ratio_percent7":"Nisbatda bir ulushni toping. Foiz o'zgarishida o'zgarish miqdorini eski qiymatga qo'shing yoki ayiring.",
"sequence7":"Arifmetik ketma-ketlikda qo'shni hadlar ayirmasi doimiy: aₙ=a₁+(n−1)d.",
"combinatorics7":"Mustaqil tanlovlar sonini ko'paytirish qoidasidan foydalaning.",
"logic7":"Shartlarni ajrating, noma'lumni belgilang, hisoblang va javobni qayta tekshiring.",
"probability7":"Ehtimollik = qulay holatlar soni / barcha teng imkoniyatli holatlar soni."
})
FORMULAS.update({
"integer7":"➖ BUTUN SONLAR\n\n• Amallar tartibiga rioya qiling: qavs → ko'paytirish/bo'lish → qo'shish/ayirish\n• (−a)+(−b)=−(a+b);  (−a)×(−b)=a×b;  (−a)×b=−(a×b)\n• Sonlar o'qida o'ngga — katta, chapga — kichik",
"fraction7":"½ RATSIONAL SONLAR\n\n• Bir xil maxrajli kasrlarda suratlar ustida amal bajariladi: a/c ± b/c=(a±b)/c\n• Sonning a/b qismi = son÷b×a\n• n : (1/k) = n×k",
"algebra_value7":"🔤 ALGEBRAIK IFODALAR (formula kitobi asosida)\n\n• Harf o'rniga qiymatni qo'ying\n• Amallar tartibi: qavs → daraja/ildiz → ko'paytirish/bo'lish → qo'shish/ayirish",
"monomial7":"✖️ BIRHADLAR\n\n• (a·xᵐ)(b·xⁿ)=ab·xᵐ⁺ⁿ (koeffitsiyentlar ko'paytiriladi, darajalar qo'shiladi)\n• xᵐ÷xⁿ=xᵐ⁻ⁿ (m>n bo'lganda)",
"identity7":"🧩 AYNIYATLAR\n\n• (a+b)²=a²+2ab+b²\n• (a−b)²=a²−2ab+b²\n• a²−b²=(a−b)(a+b)",
"linear7":"📐 CHIZIQLI TENGLAMA\n\n• ax+b=cx+d shaklida x li hadlarni bir tomonga, ozod hadlarni ikkinchi tomonga o'tkazing: (a−c)x=d−b ⇒ x=(d−b)/(a−c)",
"inequality7":"⚖️ CHIZIQLI TENGSIZLIK\n\n• Ikkala tomonga bir xil son qo'shish/ayirish belgini o'zgartirmaydi\n• Manfiy songa ko'paytirish yoki bo'lishda tengsizlik belgisi TESKARIGA almashadi",
"function7":"📈 CHIZIQLI FUNKSIYA\n\n• y=kx — to'g'ri proportsionallik, grafigi koordinata boshidan o'tuvchi to'g'ri chiziq\n• y=kx+b — umumiy chiziqli funksiya, k — burchak koeffitsiyenti, b — OY o'qi bilan kesishish ordinatasi",
"coordinate7":"📍 KOORDINATA O'QI (formula kitobi asosida)\n\n• O'ngga siljish — koordinata ortadi\n• Chapga siljish — koordinata kamayadi\n• Ikki nuqta orasidagi masofa: AB = |a−b|",
"angles7":"📐 BURCHAKLAR VA PARALLEL CHIZIQLAR\n\n• Yonma-yon burchaklar yig'indisi: 180°\n• Vertikal (qarama-qarshi) burchaklar teng\n• Parallel to'g'ri chiziqlarni kesuvchi bilan kesganda: mos burchaklar teng, ichki almashinuvchi burchaklar teng, ichki bir tomonli burchaklar yig'indisi 180°",
"triangle7":"🔺 UCHBURCHAKLAR\n\n• Uchburchak tengsizligi: |a−b|<c<a+b\n• Burchaklar yig'indisi: 180°\n• Tashqi burchak ikkita qo'shni bo'lmagan ichki burchaklar yig'indisiga teng\n• Perimetr P=a+b+c",
"geometry7":"▱ TO'RTBURCHAKLAR VA YUZALAR\n\n• To'g'ri to'rtburchak: P=2(a+b), S=ab\n• Parallelogramm: P=2(a+b), S=a·h\n• Uchburchak: S=(asos×balandlik)/2\n• Trapetsiya: S=((a+b)/2)·h",
"ratio_percent7":"⚖️ NISBAT VA FOIZ\n\n• a:b=c:d ⇒ a·d=b·c\n• P% i a ga teng son: (P/100)·a\n• Yangi qiymat = eski qiymat ± o'zgarish miqdori",
"sequence7":"🔢 ARIFMETIK KETMA-KETLIKLAR\n\n• Ayirma d=a₂−a₁ (qo'shni hadlar farqi doimiy)\n• n-had: aₙ=a₁+(n−1)d\n• Yig'indi: Sₙ=((a₁+aₙ)/2)·n",
"combinatorics7":"🎲 SODDA KOMBINATORIKA\n\n• Ko'paytirish qoidasi: mustaqil m ta va n ta tanlov birga m×n xil usulda bajariladi",
"logic7":"🧠 MANTIQIY MASALALAR\n\n• Shartlarni ajrating, noma'lumni belgilang (x), tenglama yoki tengsizlik tuzing\n• Topilgan javobni masala shartiga qayta qo'yib tekshiring",
"probability7":"🎯 SODDA EHTIMOLLIK\n\n• P(A) = qulay holatlar soni / barcha teng imkoniyatli holatlar soni\n• 0 ≤ P(A) ≤ 1; P(A) foizda: P(A)×100%"
})
TOPIC_GENERATORS.update({
"integer7":GEN_F7_INTEGER,"fraction7":GEN_F7_FRACTION,"algebra_value7":GEN_F7_EXPR,
"monomial7":GEN_F7_MONOMIAL,"identity7":GEN_F7_IDENTITY,"linear7":GEN_F7_LINEAR,
"inequality7":GEN_F7_INEQ,"function7":GEN_F7_FUNCTION,"coordinate7":GEN_F7_COORD,
"angles7":GEN_F7_ANGLES,"triangle7":GEN_F7_TRIANGLE,"geometry7":GEN_F7_GEOMETRY,
"ratio_percent7":GEN_F7_RATIO,"sequence7":GEN_F7_SEQUENCE,
"combinatorics7":GEN_F7_COMBINATORICS,"logic7":GEN_F7_LOGIC,"probability7":GEN_F7_PROBABILITY
})



# ==================== 8-SINF PROFESSIONAL GENERATORLARI ====================
# 8-sinf uchun alohida daraja. Savollar bir xil qolipga yopishib qolmasligi
# uchun har bir mavzuda turli matematik vaziyatlar qo'llanadi. Javoblar botning
# mavjud 4 ta variantli tugmalar tizimiga mos ravishda butun son qilib tuziladi.

def f8_rational_ops(grade):
    from fractions import Fraction
    t=random.randrange(5)
    if t==0:
        den=random.choice([2,3,4,5,6,8,10])
        whole=random.randint(1,8)
        n1=random.randint(1,den-1)
        n2=den*whole-n1
        return f"{n1}/{den} + {n2}/{den} = ?", whole
    if t==1:
        den=random.choice([2,3,4,5,6,8,10])
        whole=random.randint(1,8)
        n2=random.randint(1,den-1)
        n1=den*whole+n2
        return f"{n1}/{den} − {n2}/{den} = ?", whole
    if t==2:
        a=random.randint(2,12); den=random.choice([2,3,4,5,6,8,10])
        return f"{a} ÷ 1/{den} = ?", a*den
    if t==3:
        a=random.randint(2,10); b=random.randint(2,10)
        return f"{a}/{b} × {b} = ?", a
    den=random.choice([2,3,4,5,6,8]); whole=random.randint(2,9)
    n=random.randint(1,den-1)
    return f"{whole} {n}/{den} − {n}/{den} = ?", whole


def f8_square_root(grade):
    t=random.randrange(4)
    if t==0:
        n=random.randint(2,20); return f"√{n*n} = ?", n
    if t==1:
        a=random.randint(2,15); b=random.randint(2,15)
        return f"√({a*a}) + √({b*b}) = ?", a+b
    if t==2:
        n=random.randint(3,20)
        return f"√{n*n} − {n-1} = ?", 1
    n=random.randint(2,15); return f"(√{n*n})² = ?", n*n


def f8_monomial_ops(grade):
    t=random.randrange(5)
    a=random.randint(2,9); b=random.randint(2,9); m=random.randint(1,4); n=random.randint(1,4)
    if t==0: return f"{a}x^{m} × {b}x^{n} ning x^{m+n} oldidagi koeffitsiyenti?", a*b
    if t==1: return f"{a}a^{m} × {b}a^{n} ning darajasi nechaga teng?", m+n
    if t==2: return f"({a}x^{m})^2 ning x^{2*m} oldidagi koeffitsiyenti?", a*a
    if t==3: return f"{a}x^{m} ni {b} ga ko'paytirganda koeffitsiyent?", a*b
    return f"{a*a}x^{m+n} ni {a}x^{m} ga bo'lganda x darajasi?", n


def f8_polynomial(grade):
    t=random.randrange(5)
    a=random.randint(2,9); b=random.randint(1,9); c=random.randint(1,9)
    if t==0: return f"({a}x + {b}) + ({c}x − {b}) da x oldidagi koeffitsiyent?", a+c
    if t==1: return f"({a}x − {b}) − ({c}x − {b}) da x oldidagi koeffitsiyent?", a-c
    if t==2: return f"{a}x({b} + {c}) ni soddalashtirganda x oldidagi koeffitsiyent?", a*(b+c)
    if t==3: return f"{a}(x + {b}) ni ochganda doimiy had?", a*b
    return f"({a}x + {b}) + {c} ni x=0 da hisoblang.", b+c


def f8_linear_eq(grade):
    t=random.randrange(6); x=random.randint(2,25); a=random.randint(2,9)
    if t==0:
        b=random.randint(1,20); return f"{a}x + {b} = {a*x+b}. x = ?", x
    if t==1:
        b=random.randint(1,20); return f"{a}x − {b} = {a*x-b}. x = ?", x
    if t==2:
        b=random.randint(1,12); return f"{a}(x + {b}) = {a*(x+b)}. x = ?", x
    if t==3:
        c=random.randint(1,7)
        while c==a:
            c=random.randint(1,7)
        b=random.randint(1,20); rhs=(a-c)*x+b
        return f"{a}x = {c}x + {rhs}. x = ?", x
    if t==4:
        b=random.randint(2,12); rhs=a*x+b
        return f"({a}x + {b}) − {b} = {rhs-b}. x = ?", x
    b=random.randint(1,10); return f"{a}(x − {b}) = {a*(x-b)}. x = ?", x

def f8_system(grade):
    x=random.randint(2,15); y=random.randint(2,15); t=random.randrange(4)
    if t==0: return f"x+y={x+y}, x−y={x-y}. x ni toping.", x
    if t==1: return f"x+y={x+y}, x−y={x-y}. y ni toping.", y
    if t==2: return f"2x+y={2*x+y}, x+y={x+y}. x ni toping.", x
    return f"x+2y={x+2*y}, x+y={x+y}. y ni toping.", y


def f8_function(grade):
    t=random.randrange(5); k=random.randint(2,7); b=random.randint(-8,8); x=random.randint(-5,8)
    if t==0: return f"y={k}x+({b}) funksiyada x={x}. y=?", k*x+b
    if t==1:
        y=k*x+b; return f"y={k}x+({b}) funksiyada y={y}. x=?", x
    if t==2:
        x1=random.randint(-5,5); x2=x1+random.randint(2,6); return f"y={k}x+{b} funksiyada x {x1} dan {x2} gacha oshganda y qancha birlikka o'zgaradi?", k*(x2-x1)
    if t==3: return f"y={k}x+{b} funksiyaning y o'qi bilan kesishish nuqtasining ordinatasi?", b
    return f"y={k}x+{b} funksiyada x=0 bo'lgandagi y qiymat?", b


def f8_slope(grade):
    x1=random.randint(-5,3); x2=x1+random.randint(2,6); k=random.randint(1,6)
    y1=random.randint(-8,8); y2=y1+k*(x2-x1)
    return f"A({x1};{y1}) va B({x2};{y2}) nuqtalardan o'tuvchi y=kx+b chiziqda k nechaga teng?", k


def f8_factoring(grade):
    t=random.randrange(5); a=random.randint(2,9); b=random.randint(2,12)
    if t==0: return f"{a}x + {a*b} ni a(x+b) ko'rinishida yozganda b=?", b
    if t==1: return f"x² + {2*b}x + {b*b} = (x+{b})². Qavs ichidagi son?", b
    if t==2: return f"x² − {2*b}x + {b*b} = (x−{b})². Qavs ichidagi son?", b
    if t==3: return f"x² − {b*b} = (x−{b})(x+{b}). b=?", b
    return f"{a}x² + {a*b}x ni umumiy ko'paytuvchiga ajratganda x oldidagi koeffitsiyent?", a


def f8_quad(grade):
    r1=random.randint(2,12); r2=random.randint(2,12); t=random.randrange(5)
    b=-(r1+r2); c=r1*r2
    if t==0: return f"x² + ({b})x + {c}=0 tenglamaning kichik ildizi?", min(r1,r2)
    if t==1: return f"x² + ({b})x + {c}=0 tenglamaning ildizlari yig'indisi?", r1+r2
    if t==2: return f"x² + ({b})x + {c}=0 tenglamaning ildizlari ko'paytmasi?", c
    if t==3: return f"x² + ({b})x + {c}=0 tenglamaning diskriminanti?", (r1-r2)**2
    return f"x² + ({b})x + {c}=0 tenglamaning katta ildizi?", max(r1,r2)


def f8_inequality(grade):
    t=random.randrange(5); a=random.randint(2,8); x=random.randint(2,20); b=random.randint(1,20)
    if t==0:
        rhs=a*x+b-1
        return f"{a}x+{b} > {rhs}. Eng kichik natural x?", x
    if t==1:
        # a*x-b < a*x+1 emas, chegaralangan tengsizlik beramiz.
        rhs=a*x+b
        return f"{a}x < {rhs}. Eng katta natural x?", max(1,(rhs-1)//a)
    if t==2:
        rhs=a*x+1
        return f"{a}x < {rhs}. Eng katta natural x?", x
    if t==3:
        rhs=a*x
        return f"{a}x ≥ {rhs}. Eng kichik natural x?", x
    # x ning koeffitsiyenti manfiy bo'lganda yo'nalish o'zgarishini tekshiradi.
    rhs=-a*x+b
    return f"−{a}x + {b} ≤ {rhs}. Eng kichik natural x?", x

def f8_geometry_similarity(grade):
    t=random.randrange(4)
    if t==0:
        a=random.randint(3,10); k=random.randint(2,5); return f"O'xshash uchburchaklarda mos tomonlar nisbati 1:{k}. Kichik tomoni {a} sm bo'lsa, katta mos tomon?", a*k
    if t==1:
        a=random.randint(4,12); k=random.randint(2,4); return f"Masshtab 1:{k}. Chizmada {a} sm bo'lgan kesma aslida necha sm?", a*k
    if t==2:
        a=random.randint(3,10); k=random.randint(2,5); return f"O'xshash shakllarning uzunlik koeffitsiyenti {k}. Kichik perimetr {a*4} sm. Katta perimetr?", a*4*k
    a=random.randint(2,8); k=random.randint(2,5); return f"O'xshash uchburchaklarda balandliklar nisbati 1:{k}. Kichik balandlik {a} sm. Katta balandlik?", a*k


def f8_pythagoras(grade):
    triples=[(3,4,5),(5,12,13),(6,8,10),(8,15,17),(7,24,25)]
    a,b,c=random.choice(triples); t=random.randrange(3)
    if t==0: return f"Katetlari {a} sm va {b} sm bo'lgan to'g'ri burchakli uchburchak gipotenuzasi?", c
    if t==1: return f"Gipotenuzasi {c} sm, bir kateti {a} sm. Ikkinchi katet?", b
    return f"Gipotenuzasi {c} sm, bir kateti {b} sm. Ikkinchi katet?", a


def f8_area(grade):
    t=random.randrange(5)
    if t==0:
        a=random.randint(5,20); h=random.randint(3,14)
        return f"Parallelogramm asosi {a} sm, balandligi {h} sm. Yuzasi?", a*h
    if t==1:
        a=random.randint(5,18); b=a+random.randint(2,8); h=random.choice([2,4,6,8,10,12])
        return f"Trapetsiyaning asoslari {a} va {b} sm, balandligi {h} sm. Yuzasi?", (a+b)*h//2
    if t==2:
        a=random.choice([4,6,8,10,12,14,16,18,20]); b=random.choice([2,4,6,8,10,12,14])
        return f"Uchburchak asosi {a} sm, balandligi {b} sm. Yuzasi?", a*b//2
    if t==3:
        a=random.randint(5,20); b=random.randint(4,18)
        return f"To'g'ri to'rtburchak tomonlari {a} sm va {b} sm. Yuzasi?", a*b
    # Yuzasi berilib, balandlikni tiklash.
    a=random.randint(4,12); h=random.randint(3,12); area=a*h
    return f"Parallelogrammning asosi {a} sm, yuzi {area} sm². Balandligi nechaga teng?", h

def f8_statistics(grade):
    t=random.randrange(4); base=random.randint(10,40); vals=[base+random.randint(-5,5) for _ in range(5)]
    if t==0:
        vals=[base-2,base,base+4,base+6,base-3]; return f"{', '.join(map(str,vals))} sonlarining o'rtacha arifmetigi?", sum(vals)//len(vals)
    if t==1:
        vals=[base,base+3,base,base-2,base+5]; return f"{', '.join(map(str,vals))} qatorining modasi?", base
    vals=sorted([base-5,base-2,base,base+4,base+7]); return f"{', '.join(map(str,vals))} qatorining medianasi?", vals[2]


def f8_word_ratio(grade):
    unit=random.randint(4,15); a=random.randint(2,5); b=random.randint(3,7); c=random.randint(2,5)
    total=unit*(a+b); return f"Kitoblar soni ikki javonda {a}:{b} nisbatda. Jami {total} ta bo'lsa, birinchi javonga {c} ta qo'shilsa u yerda nechta bo'ladi?", unit*a+c


def f8_logic(grade):
    t=random.randrange(5)
    if t==0:
        n=random.randint(4,20); return f"Ketma-ket to'rtta natural son yig'indisi {4*n+6} ga teng. Eng kichik son?", n
    if t==1:
        a=random.randint(3,15); return f"Ikki sonning yig'indisi {2*a+9}, katta son kichik sondan 9 taga katta. Kichik son?", a
    if t==2:
        n=random.randint(5,20); return f"Bir sonning 3 baravaridan 4 ayirilsa {3*n-4} chiqadi. Son?", n
    if t==3:
        n=random.randint(5,18); return f"To'g'ri to'rtburchakning bo'yi enidan 3 sm uzun. Perimetri {4*n+6} sm. Eni?", n
    n=random.randint(4,15); return f"Ketma-ket uchta juft sonning o'rtadagi hadi {2*n}. Ularning yig'indisi?", 6*n


def f8_word_speed(grade):
    t=random.randrange(4)
    if t==0:
        v=random.randint(40,90); h=random.randint(2,5)
        return f"Avtomobil {v} km/soat tezlikda {h} soat yurdi. Necha km yo'l bosdi?", v*h
    if t==1:
        v=random.choice([40,50,60,80]); h=random.randint(2,8); d=v*h
        return f"Avtomobil {d} km yo'lni {v} km/soat tezlikda bosib o'tdi. Vaqt necha soat?", h
    v=random.choice([30,40,50,60,80]); h=random.randint(2,8); d=v*h
    return f"Velosipedchi {d} km yo'lni {h} soatda bosib o'tdi. O'rtacha tezligi?", v



def f8_power_rules(grade):
    t=random.randrange(5)
    if t==0:
        a=random.randint(2,8); m=random.randint(2,4); n=random.randint(1,4)
        return f"{a}^{m} × {a}^{n} = {a}^? darajaning ko'rsatkichi nechaga teng?", m+n
    if t==1:
        a=random.randint(2,8); m=random.randint(3,6); n=random.randint(1,m-1)
        return f"{a}^{m} ÷ {a}^{n} = {a}^? darajaning ko'rsatkichi nechaga teng?", m-n
    if t==2:
        a=random.randint(2,6); m=random.randint(2,4); n=random.randint(2,3)
        return f"({a}^{m})^{n} = {a}^? darajaning ko'rsatkichi?", m*n
    if t==3:
        a=random.randint(2,10); n=random.randint(2,5)
        return f"{a}^{n} qiymatini toping.", a**n
    a=random.randint(2,9); n=random.randint(2,4)
    return f"{a}^{n} sonining oxirgi raqami nechaga teng?", (a**n)%10


def f8_circle(grade):
    t=random.randrange(4)
    r=random.choice([7,14,21,28])
    if t==0:
        return f"Radiusi {r} sm bo'lgan aylananing diametri nechaga teng?", 2*r
    if t==1:
        return f"Radiusi {r} sm bo'lgan aylana uzunligini π=22/7 deb olib toping.", 2*22*(r//7)
    if t==2:
        return f"Radiusi {r} sm bo'lgan doira yuzini π=22/7 deb olib toping.", 22*(r//7)*(r)
    return f"Diametri {2*r} sm bo'lgan doiraning radiusi?", r


def f8_probability(grade):
    t=random.randrange(4)
    if t==0:
        choices=[(1,4),(1,2),(3,4),(1,5),(2,5),(3,5),(4,5)]
        p,total_units=random.choice(choices); mult=random.randint(1,5)
        good=p*mult; total=total_units*mult
        return f"Qutida {good} ta qizil va {total-good} ta ko'k shar bor. Qizil shar chiqish ehtimoli necha foiz?", 100*good//total
    if t==1:
        # Kubik: 1 dan 6 gacha teng imkoniyatli natijalar. Juft chiqish — 3/6=50%.
        return "Oddiy kubik bir marta tashlandi. Juft son chiqish ehtimoli necha foiz?", 50
    if t==2:
        # 1..10 oralig'ida 5 ga karrali sonlar: 5 va 10 -> 20%.
        return "1 dan 10 gacha son yozilgan kartalardan biri tasodifiy tanlandi. 5 ga karrali son chiqish ehtimoli necha foiz?", 20
    # 4 ta teng rangli bo'lakdan 1 tasi tanlanadi.
    return "G'ildirak 4 ta teng sektorga bo'lingan: 1 tasi yashil, 3 tasi sariq. Yashil tushish ehtimoli necha foiz?", 25


GEN_F8_RATIONAL=[(f8_rational_ops,{"eighth"})]
GEN_F8_POWER=[(f8_power_rules,{"eighth"})]
GEN_F8_ROOT=[(f8_square_root,{"eighth"})]
GEN_F8_MONOMIAL=[(f8_monomial_ops,{"eighth"})]
GEN_F8_POLYNOMIAL=[(f8_polynomial,{"eighth"})]
GEN_F8_LINEAR=[(f8_linear_eq,{"eighth"})]
GEN_F8_SYSTEM=[(f8_system,{"eighth"})]
GEN_F8_FUNCTION=[(f8_function,{"eighth"}),(f8_slope,{"eighth"})]
GEN_F8_FACTOR=[(f8_factoring,{"eighth"})]
GEN_F8_QUAD=[(f8_quad,{"eighth"})]
GEN_F8_INEQ=[(f8_inequality,{"eighth"})]
GEN_F8_SIMILAR=[(f8_geometry_similarity,{"eighth"})]
GEN_F8_PYTH=[(f8_pythagoras,{"eighth"})]
GEN_F8_AREA=[(f8_area,{"eighth"})]
GEN_F8_CIRCLE=[(f8_circle,{"eighth"})]
GEN_F8_PROB=[(f8_probability,{"eighth"})]
GEN_F8_STATS=[(f8_statistics,{"eighth"})]
GEN_F8_RATIO=[(f8_word_ratio,{"eighth"})]
GEN_F8_LOGIC=[(f8_logic,{"eighth"})]
GEN_F8_SPEED=[(f8_word_speed,{"eighth"})]

TOPICS.update({
"rational8":"½ Ratsional sonlar va amallar","power8":"xⁿ Darajalar va daraja xossalari",
"root8":"√ Kvadrat ildizlar","monomial8":"✖️ Birhadlar va ularning amallari",
"polynomial8":"🔤 Ko'phadlar","linear8":"📐 Chiziqli tenglamalar",
"system8":"🔗 Tenglamalar sistemasi","function8":"📈 Chiziqli funksiya",
"factor8":"🧩 Ko'paytuvchilarga ajratish","quad8":"🔷 Kvadrat tenglamaga kirish",
"ineq8":"⚖️ Tengsizliklar","similar8":"🔺 O'xshashlik va masshtab",
"pyth8":"📐 Pifagor teoremasi","area8":"▱ Geometrik yuzalar",
"circle8":"⭕ Aylana va doira","prob8":"🎯 Ehtimollik",
"stats8":"📊 Statistika","ratio8":"⚖️ Nisbatli amaliy masalalar",
"logic8":"🧠 Mantiqiy masalalar","speed8":"🚗 Harakat masalalari"
})
GRADE_LABELS["eighth"]="🟣 8-sinf"
GRADE_TOPICS["eighth"]= [
"rational8","power8","root8","monomial8","polynomial8","linear8","system8",
"function8","factor8","quad8","ineq8","similar8","pyth8","area8","circle8",
"prob8","stats8","ratio8","logic8","speed8"
]
HINTS.update({
"rational8":"Kasrlarni umumiy maxrajga keltiring; amalni bajarib, natijani qisqartiring.",
"power8":"a^m×a^n=a^(m+n), a^m÷a^n=a^(m−n), (a^m)^n=a^(mn).",
"root8":"√a — kvadrati a ga teng bo'lgan nomanfiy son.",
"monomial8":"Koeffitsiyentlarni ko'paytiring, bir xil harflar darajalarini qo'shing.",
"polynomial8":"O'xshash hadlarni birlashtiring va qavslarni to'g'ri oching.",
"linear8":"Qavslarni ochib, x li hadlarni bir tomonga, sonlarni ikkinchi tomonga o'tkazing.",
"system8":"Ikki tenglamadan birini ifodalab, ikkinchisiga qo'ying yoki qo'shish usulidan foydalaning.",
"function8":"y=kx+b da k — x o'zgarganda y ning o'zgarish tezligi, b — x=0 dagi qiymat.",
"factor8":"Umumiy ko'paytuvchini ajrating va qisqa ko'paytirish formulalarini tekshiring.",
"quad8":"Vieta: x1+x2=−b, x1x2=c (x²+bx+c=0). Diskriminant D=b²−4ac.",
"ineq8":"Noma'lumni ajratishda manfiy songa bo'lsangiz, tengsizlik belgisi almashadi.",
"similar8":"O'xshash shakllarda mos tomonlar bir xil koeffitsiyentga ega.",
"pyth8":"To'g'ri burchakli uchburchakda c²=a²+b².",
"area8":"Parallelogramm S=ah; uchburchak S=ah/2; trapetsiya S=(a+b)h/2.",
"circle8":"Diametr d=2r. Doira yuzi S=πr², aylana uzunligi L=2πr.",
"prob8":"P(A)=qulay holatlar soni / barcha teng imkoniyatli holatlar soni.",
"stats8":"O'rtacha — yig'indi/n; moda — eng ko'p uchragan qiymat; mediana — tartiblangan qatorning o'rtasi.",
"ratio8":"Nisbatdagi bir ulushni toping, so'ng kerakli ulushlar soniga ko'paytiring.",
"logic8":"Shartni algebraik modelga aylantiring, yechimni toping va shartga qayta qo'ying.",
"speed8":"s=vt, v=s/t, t=s/v. Birliklarni bir xil qiling."
})
FORMULAS.update({
"rational8":"½ RATSIONAL SONLAR\n\n• Kasrlarni umumiy maxrajga keltirib qo'shing/ayiring: a/b±c/d=(ad±bc)/(bd)\n• Bo'lishda ikkinchi kasr teskarilanadi: (a/b)÷(c/d)=(a/b)×(d/c)",
"power8":"xⁿ DARAJALAR\n\n• aᵐ×aⁿ=aᵐ⁺ⁿ\n• aᵐ÷aⁿ=aᵐ⁻ⁿ (a≠0)\n• (aᵐ)ⁿ=aᵐⁿ\n• a⁰=1 (a≠0);  a⁻ⁿ=1/aⁿ",
"root8":"√ KVADRAT ILDIZ\n\n• √a — a ning nomanfiy kvadrat ildizi (a≥0)\n• √(a·b)=√a·√b (a,b≥0);  √(a/b)=√a/√b (a≥0,b>0)\n• (√a)²=a (a≥0);  √(a²)=|a|",
"monomial8":"✖️ BIRHADLAR\n\n• (a·xᵐ)(b·xⁿ)=ab·xᵐ⁺ⁿ\n• Bir xil harfli ko'phadlarni qo'shishda faqat koeffitsiyentlar qo'shiladi",
"polynomial8":"🔤 KO'PHADLAR\n\n• O'xshash hadlar qo'shiladi yoki ayriladi\n• Qavs ochishda distributivlik qonunidan foydalaniladi: a(b+c)=ab+ac\n• Ko'phadni ko'phadga ko'paytirish: har bir hadni har bir had bilan ko'paytirib qo'shiladi",
"linear8":"📐 CHIZIQLI TENGLAMA\n\n• ax+b=cx+d shaklida x li hadlarni bir tomonga, ozod hadlarni ikkinchi tomonga o'tkazing: (a−c)x=d−b ⇒ x=(d−b)/(a−c)",
"system8":"🔗 IKKI NOMA'LUMLI CHIZIQLI TENGLAMALAR SISTEMASI (formula kitobi asosida)\n\n• Umumiy ko'rinish: a₁x + b₁y = c₁,  a₂x + b₂y = c₂\n1) Yechimga ega emas: a₁/a₂ = b₁/b₂ ≠ c₁/c₂\n2) Yagona yechim: a₁/a₂ ≠ b₁/b₂\n3) Cheksiz ko'p yechim: a₁/a₂ = b₁/b₂ = c₁/c₂\n\n• O'RNIGA QO'YISH USULI: bitta tenglamadan bitta o'zgaruvchini ifodalab (masalan y=...), ikkinchi tenglamaga qo'yiladi\n• QO'SHISH (ALGEBRAIK) USULI: tenglamalarni bir xil koeffitsientlar hosil bo'ladigan songa ko'paytirib, qo'shish yoki ayirish orqali bitta o'zgaruvchi yo'qotiladi",
"function8":"📈 CHIZIQLI FUNKSIYA\n\n• y=kx — to'g'ri proportsionallik, grafigi koordinata boshidan o'tuvchi to'g'ri chiziq\n• y=kx+b — umumiy chiziqli funksiya, k — burchak koeffitsiyenti, b — OY o'qi bilan kesishish ordinatasi",
"factor8":"🧩 KO'PAYTUVCHILARGA AJRATISH\n\n• Umumiy ko'paytuvchini qavsdan tashqariga chiqarish\n• (a+b)²=a²+2ab+b²,  (a−b)²=a²−2ab+b²\n• a²−b²=(a−b)(a+b)\n• Guruhlash usuli: ax+ay+bx+by=a(x+y)+b(x+y)=(a+b)(x+y)",
"quad8":"🔷 KVADRAT TENGLAMALAR (formula kitobi asosida)\n\n• ax²+bx+c=0 (a≠0);  D=b²−4ac\n• x₁,₂=(−b±√D)/(2a)\n• Viyet: x₁+x₂=−b/a, x₁·x₂=c/a\n• D>0 — 2 ildiz, D=0 — 1 ildiz, D<0 — ildiz yo'q",
"ineq8":"⚖️ TENGSIZLIK\n\n• Manfiy songa ko'paytirish yoki bo'lishda tengsizlik belgisi almashadi\n• Kvadrat tengsizlikda: parabolaning OX o'qi bilan kesishish nuqtalari va shoxobchalar yo'nalishini (a ishorasini) tahlil qiling",
"similar8":"🔺 O'XSHASHLIK\n\n• Ikkita uchburchakning ikkita burchagi mos teng bo'lsa, ular o'xshash\n• O'xshash shakllarda mos tomonlar nisbati bir xil (o'xshashlik koeffitsiyenti k)\n• Yuzalar nisbati=o'xshashlik koeffitsiyenti kvadratiga teng: S₁/S₂=k²",
"pyth8":"📐 PIFAGOR TEOREMASI\n\n• To'g'ri burchakli uchburchakda: a²+b²=c² (c — gipotenuza)\n• Balandlik xossasi: h²=a_c·b_c (gipotenuzaga tushirilgan balandlik)\n• a²=a_c·c,  b²=b_c·c",
"area8":"▱ YUZALAR\n\n• Parallelogramm: S=a·h=ab·sinα\n• Uchburchak: S=(a·h)/2\n• Trapetsiya: S=((a+b)/2)·h\n• Romb: S=(d₁·d₂)/2=a·h",
"circle8":"⭕ AYLANA VA DOIRA\n\n• Diametr d=2r\n• Doira yuzi S=πr²=πd²/4\n• Aylana uzunligi L=2πr=πd",
"prob8":"🎯 11-SINF — EHTIMOLLIK\n\n• P(A)=m/n — teng imkoniyatli natijalarda (m — qulay, n — barcha holatlar)\n• P(A')=1−P(A)\n• Mustaqil hodisalar: P(A∩B)=P(A)·P(B)\n• Umumiy qoida: P(A∪B)=P(A)+P(B)−P(A∩B)\n• Shartli ehtimollik: P(A|B)=P(A∩B)/P(B), P(B)>0.",
"stats8":"📊 STATISTIKA\n\n• O'rtacha (o'rta arifmetik) = yig'indi/n\n• Moda — eng ko'p uchragan qiymat\n• Mediana — tartiblangan qatorning o'rtasidagi qiymat (juft sonda o'rtadagi ikkitasining o'rta arifmetigi)",
"ratio8":"⚖️ NISBAT\n\n• a:b=c:d ⇒ a·d=b·c\n• Nisbatdagi bir ulushni toping, so'ng kerakli ulushlar soniga ko'paytiring",
"logic8":"🧠 MANTIQIY MASALALAR\n\n• Shartlarni ajrating, noma'lumni belgilang (x), tenglama yoki tengsizlik tuzing\n• Topilgan javobni masala shartiga qayta qo'yib tekshiring",
"speed8":"🚗 HARAKAT MASALALARI\n\n• s=vt,  v=s/t,  t=s/v\n• Qarama-qarshi harakatda tezliklar qo'shiladi: v=v₁+v₂\n• Quvib o'tishda tezliklar farqi olinadi: v=v₁−v₂"
})
TOPIC_GENERATORS.update({
"rational8":GEN_F8_RATIONAL,"power8":GEN_F8_POWER,"root8":GEN_F8_ROOT,"monomial8":GEN_F8_MONOMIAL,
"polynomial8":GEN_F8_POLYNOMIAL,"linear8":GEN_F8_LINEAR,"system8":GEN_F8_SYSTEM,"function8":GEN_F8_FUNCTION,
"factor8":GEN_F8_FACTOR,"quad8":GEN_F8_QUAD,"ineq8":GEN_F8_INEQ,"similar8":GEN_F8_SIMILAR,
"pyth8":GEN_F8_PYTH,"area8":GEN_F8_AREA,"circle8":GEN_F8_CIRCLE,"prob8":GEN_F8_PROB,
"stats8":GEN_F8_STATS,"ratio8":GEN_F8_RATIO,"logic8":GEN_F8_LOGIC,"speed8":GEN_F8_SPEED
})


# ==================== 9-SINF PROFESSIONAL GENERATORLARI ====================
# 9-sinf uchun alohida generatorlar. Har bir mavzuda savol turi va mazmuni
# bir-biridan farq qiladi. Javoblar Telegramdagi raqamli variantlar tizimiga
# mos ravishda butun son qilib tuziladi.

def f9_rational(grade):
    t=random.randrange(6)
    if t==0:
        den=random.choice([3,4,5,6,8,10,12]); a=random.randint(1,den-1); b=den-a
        return f"{a}/{den} + {b}/{den} = ?", 1
    if t==1:
        den=random.choice([3,4,5,6,8,10]); k=random.randint(2,8)
        return f"{k*den}/{den} × 1 = ?", k
    if t==2:
        den=random.choice([2,3,4,5,6]); k=random.randint(2,8)
        return f"{k*den}/{den} ÷ 1 = ?", k
    if t==3:
        den=random.choice([2,3,4,5,6,8]); whole=random.randint(2,8); num=whole*den+random.randint(1,den-1)
        return f"{num}/{den} aralash sonining butun qismi nechaga teng?", whole
    if t==4:
        den=random.choice([3,4,5,6,8]); a=random.randint(1,den-1); b=random.randint(1,den-1)
        return f"{a}/{den} va {b}/{den} kasrlardan kattasining surati nechaga teng?", max(a,b)
    den=random.choice([2,3,4,5,6]); k=random.randint(2,8)
    return f"{k} sonining {den}/{den} qismi nechaga teng?", k

def f9_power(grade):
    base=random.randint(2,5); t=random.randrange(6)
    if t==0:
        m,n=random.randint(2,5),random.randint(1,4)
        return f"{base}^{m} × {base}^{n} = {base}^k. k = ?", m+n
    if t==1:
        n=random.randint(1,3); m=random.randint(n+1,6)
        return f"{base}^{m} ÷ {base}^{n} = {base}^k. k = ?", m-n
    if t==2:
        m,n=random.randint(1,4),random.randint(2,4)
        return f"({base}^{m})^{n} = {base}^k. k = ?", m*n
    if t==3:
        n=random.randint(2,4)
        return f"{base}^{n} qiymati nechaga teng?", base**n
    if t==4:
        n=random.randint(2,5)
        return f"{base}^0 + {base}^{n} = ?", 1+base**n
    n=random.randint(2,4)
    return f"{base}^{n} · {base}^0 = ?", base**n


def f9_root(grade):
    t=random.randrange(6)
    if t==0:
        a=random.randint(3,25)
        return f"√{a*a} = ?", a
    if t==1:
        a=random.randint(3,18); b=random.randint(2,10)
        return f"√({a*a}·{b*b}) = ?", a*b
    if t==2:
        a=random.randint(4,20)
        return f"Yuzasi {a*a} sm² bo'lgan kvadratning tomoni nechaga teng?", a
    if t==3:
        a=random.randint(2,12)
        return f"√({a*a}) + √({(a+1)*(a+1)}) = ?", 2*a+1
    if t==4:
        a=random.randint(3,15)
        return f"√({a*a}·4) = ?", 2*a
    a=random.randint(2,12)
    return f"√({a*a}) − {a-1} = ?", 1


def f9_algebra(grade):
    t=random.randrange(7)
    a,b,c=random.randint(2,9),random.randint(2,12),random.randint(2,10)
    x=random.randint(2,8)
    if t==0:
        return f"{a}x + {b}x − {c}x ifodasida x ning koeffitsiyenti nechaga teng?", a+b-c
    if t==1:
        return f"{a}(x + {b}) ni ochganda x ning koeffitsiyenti nechaga teng?", a
    if t==2:
        return f"{a}x + {b} ifodaning x={x} dagi qiymati?", a*x+b
    if t==3:
        return f"{a}(x+{b}) − {a}x = ?", a*b
    if t==4:
        return f"{a}x − {b}x + {c}x ifodasining x koeffitsiyenti?", a-b+c
    if t==5:
        return f"{a}(2x+{b}) da x oldidagi koeffitsiyent nechaga teng?", 2*a
    return f"x={x} bo'lganda {a}(x+{b})−{c} ifodaning qiymati?", a*(x+b)-c


def f9_factor(grade):
    a=random.randint(2,9); b=random.randint(2,12); t=random.randrange(6)
    if t==0:
        return f"{a}x + {a*b} ni {a}(x+b) ko'rinishida yozganda b = ?", b
    if t==1:
        return f"x² + {2*b}x + {b*b} = (x+{b})². Qavs ichidagi son nechaga teng?", b
    if t==2:
        return f"x² − {b*b} = (x−b)(x+b). b = ?", b
    if t==3:
        return f"{a}x² + {a*b}x = {a}x(x + b). b ning qiymati?", b
    if t==4:
        return f"{a}x² − {a*b}x = {a}x(x − b). b ning qiymati?", b
    return f"x² + {2*b}x + {b*b} ifodaning x={-b} dagi qiymati?", 0

def f9_linear(grade):
    t=random.randrange(6); x=random.randint(2,20)
    if t==0:
        a=random.randint(2,9); b=random.randint(1,20); c=a*x+b
        return f"{a}x + {b} = {c}. x = ?", x
    if t==1:
        a=random.randint(3,9); b=random.randint(1,20); c=a*x-b
        return f"{a}x − {b} = {c}. x = ?", x
    if t==2:
        a=random.randint(2,7); b=random.randint(1,10); c=random.randint(1,6); d=a*x+b-c*x
        return f"{a}x + {b} = {c}x + {d}. x = ?", x
    if t==3:
        a=random.randint(2,8); b=random.randint(1,10); c=a*(x+b)
        return f"{a}(x+{b}) = {c}. x = ?", x
    if t==4:
        a=random.randint(2,7); b=random.randint(1,12); total=a*x+b
        return f"{a} ta bir xil daftar to'plami va {b} ta alohida daftar jami {total} ta. Har bir to'plamda x ta daftar bo'lsa, x=?", x
    a=random.randint(2,7); b=random.randint(1,10); c=random.randint(1,6); rhs=a*x+a*b-c*x
    sign='+' if rhs>=0 else '−'
    return f"{a}(x+{b}) = {c}x {sign} {abs(rhs)}. x = ?", x


def f9_quad(grade):
    t=random.randrange(6); r1=random.randint(1,12); r2=random.randint(1,12)
    b=-(r1+r2); c=r1*r2
    sb=f"+ {b}x" if b>=0 else f"− {abs(b)}x"
    sc=f"+ {c}" if c>=0 else f"− {abs(c)}"
    eq=f"x² {sb} {sc} = 0"
    if t==0: return f"{eq} tenglama ildizlarining yig'indisi?", r1+r2
    if t==1: return f"{eq} tenglama ildizlarining ko'paytmasi?", r1*r2
    if t==2: return f"{eq} tenglamaning katta ildizi?", max(r1,r2)
    if t==3:
        d=b*b-4*c
        return f"{eq} tenglamaning diskriminanti D nechaga teng?", d
    if t==4: return f"{eq} ildizlari x₁ va x₂ bo'lsa, x₁²+x₂² nechaga teng?", r1*r1+r2*r2
    return f"{eq} tenglama ildizlarining ayirmasi |x₁−x₂| nechaga teng?", abs(r1-r2)


def f9_system(grade):
    x0=random.randint(1,12); y0=random.randint(1,12)
    while True:
        a,b=random.randint(1,6),random.randint(1,6); c,d=random.randint(1,6),random.randint(1,6)
        if a*d-b*c!=0: break
    e=a*x0+b*y0; f=c*x0+d*y0
    l1=f"{a}x + {b}y = {e}"; l2=f"{c}x + {d}y = {f}"
    t=random.randrange(5)
    if t==0: return f"{l1}\n{l2}\nSistemadan x ni toping.", x0
    if t==1: return f"{l1}\n{l2}\nSistemadan y ni toping.", y0
    if t==2: return f"{l1}\n{l2}\nSistemada x+y nechaga teng?", x0+y0
    if t==3: return f"{l1}\n{l2}\nSistemada x−y nechaga teng?", x0-y0
    total=x0+y0
    return f"Ikki sonning yig'indisi {total}. Ularning mos ravishda {a} va {b} marta olingan yig'indisi {a*x0+b*y0}. Birinchi son nechaga teng?", x0


def f9_function(grade):
    k=random.randint(2,8); b=random.randint(-8,8); x=random.randint(-5,8); t=random.randrange(6)
    y=k*x+b
    if t==0: return f"y={k}x{b:+d} funksiyada x={x}. y=?", y
    if t==1: return f"y={k}x{b:+d} funksiyada y={y}. x=?", x
    if t==2:
        x1=random.randint(-5,2); x2=x1+random.randint(2,6)
        return f"y={k}x{b:+d}: x {x1} dan {x2} gacha oshsa, y nechaga o'zgaradi?", k*(x2-x1)
    if t==3: return f"y={k}x{b:+d} funksiyaning y o'qi bilan kesishish ordinatasi?", b
    if t==4: return f"y={k}x{b:+d} funksiyada x=0 bo'lgandagi y?", b
    return f"y={k}x{b:+d} funksiyaning qiyalik koeffitsiyenti k nechaga teng?", k


def f9_inequality(grade):
    t=random.randrange(5)
    if t==0:
        a=random.randint(2,9); x0=random.randint(-5,12); b=random.randint(-10,10); c=a*x0+b
        bs=f"+ {b}" if b>=0 else f"− {abs(b)}"
        return f"{a}x {bs} > {c-1}. x ning eng kichik butun qiymati?", x0
    if t==1:
        a=random.randint(2,9); x0=random.randint(-5,12); b=random.randint(-10,10); c=a*x0+b
        bs=f"+ {b}" if b>=0 else f"− {abs(b)}"
        return f"{a}x {bs} ≤ {c}. x ning eng katta butun qiymati?", x0
    if t==2:
        a=random.randint(2,9); x0=random.randint(-5,12); b=random.randint(-10,10); c=-a*x0+b
        bs=f"+ {b}" if b>=0 else f"− {abs(b)}"
        return f"−{a}x {bs} ≥ {c}. x ning eng katta butun qiymati?", -x0
    if t==3:
        a=random.randint(2,8); lo=random.randint(-5,5); hi=lo+random.randint(3,8); rhs=a*(hi-lo)
        return f"{a}(x − ({lo})) < {rhs}. x ning eng katta butun qiymati?", hi-1
    lo=random.randint(-4,5); hi=lo+random.randint(3,7)
    return f"x > {lo} va x ≤ {hi}. Shu shartlarni qanoatlantiruvchi eng katta butun x?", hi

def f9_statistics(grade):
    t=random.randrange(5)
    if t==0:
        avg=random.randint(10,40); vals=[avg-3,avg-1,avg+1,avg+3]
        return f"{vals} sonlarining o'rtacha arifmetik qiymati?", avg
    if t==1:
        mode=random.randint(2,9); a=random.randint(1,9); b=random.randint(1,9); vals=[a,mode,b,mode,mode]
        return f"{vals} qatorining modasi nechaga teng?", mode
    if t==2:
        vals=sorted(random.sample(range(2,30),5)); return f"{vals} qatorining medianasi?", vals[2]
    if t==3:
        vals=sorted(random.sample(range(2,30),6)); return f"{vals} qatorining diapazoni nechaga teng?", vals[-1]-vals[0]
    vals=[random.randint(5,20) for _ in range(4)]; return f"{vals} sonlari yig'indisi nechaga teng?", sum(vals)


def f9_probability(grade):
    t=random.randrange(8)
    if t==0:
        total=random.choice([20,40,50,100]); pct=random.choice([10,20,25,30,40,50,60,75]); good=total*pct//100
        return f"Qutida {good} ta qizil va {total-good} ta ko'k shar bor. Qizil shar chiqish ehtimoli necha foiz?", pct
    if t==1:
        total=random.choice([20,40,50,100]); pct=random.choice([10,20,30,40,50]); good=total*pct//100
        return f"{total} ta lotereya chiptasidan {good} tasi yutuqli. Tasodifiy bitta chipta yutuqli chiqish ehtimoli necha foiz?", pct
    if t==2:
        total=random.choice([20,40,100]); pct=random.choice([20,25,30,40,50]); good=total*pct//100
        return f"Testda {total} ta savol bor. O'quvchi {good} tasiga to'g'ri javob berdi. Tasodifiy tanlangan savol to'g'ri ishlangan bo'lish ehtimoli necha foiz?", pct
    if t==3:
        total=random.choice([20,40,50]); pct=random.choice([20,25,40,50,60]); good=total*pct//100
        return f"Qutida jami {total} ta shar bor, ulardan {good} tasi yashil. Bitta shar tanlanganda yashil chiqish ehtimoli necha foiz?", pct
    if t==4:
        return "Oddiy kubik bir marta tashlandi. Juft son tushish ehtimoli necha foiz?", 50
    if t==5:
        return "Oddiy kubik bir marta tashlandi. 3 dan katta son tushish ehtimoli necha foiz?", 50
    if t==6:
        return "1 dan 10 gacha bo'lgan sonlardan bittasi tanlandi. 3 ga karrali son chiqish ehtimoli necha foiz?", 30
    return "1 dan 20 gacha bo'lgan sonlardan bittasi tanlandi. 4 ga karrali son chiqish ehtimoli necha foiz?", 25

def f9_logic(grade):
    t=random.randrange(6)
    if t==0:
        m=random.randint(3,12); return f"Ketma-ket uchta juft son yig'indisi {6*m} ga teng. O'rtadagi son?", 2*m
    if t==1:
        a=random.randint(3,12); b=random.randint(2,9); total=3*a-b
        return f"Bir sonning 3 baravaridan {b} ayirilsa {total} chiqadi. Sonni toping.", a
    if t==2:
        small=random.randint(4,15); large=small+random.randint(5,15)
        return f"Ikki sonning yig'indisi {small+large}, ayirmasi {large-small}. Katta son nechaga teng?", large
    if t==3:
        n=random.randint(5,15); return f"1, 4, 9, 16, ... ketma-ketligining {n}-hadi nechaga teng?", n*n
    if t==4:
        a=random.randint(2,8); return f"Bir sonning yarmi bilan 7 ning yig'indisi {a+7} ga teng. Sonni toping.", 2*a
    a=random.randint(4,10); return f"To'rtburchakning perimetri {4*a} sm. Agar u kvadrat bo'lsa, bir tomoni nechaga teng?", a


def f9_geometry(grade):
    t=random.randrange(6)
    if t==0:
        a=random.randint(5,15); b=random.randint(5,15)
        return f"To'g'ri to'rtburchak tomonlari {a} sm va {b} sm. Diagonalining kvadrati nechaga teng?", a*a+b*b
    if t==1:
        a,b=random.choice([(3,4),(5,12),(6,8),(8,15),(9,12)])
        return f"To'g'ri burchakli uchburchak katetlari {a} sm va {b} sm. Gipotenuza nechaga teng?", int(math.sqrt(a*a+b*b))
    if t==2:
        r=random.randint(3,12); return f"Radiusi {r} sm bo'lgan doira yuzi π=3 deb olinsa, yuza nechaga teng?", 3*r*r
    if t==3:
        a=random.randint(30,80); b=random.randint(30,80)
        while a+b>=170: b=random.randint(30,80)
        return f"Uchburchakning ikki burchagi {a}° va {b}°. Uchinchi burchak?", 180-a-b
    if t==4:
        a=random.randint(4,15); h=random.randint(4,15); return f"Asosi {a} sm, balandligi {h} sm bo'lgan parallelogramm yuzi?", a*h
    a=random.randint(4,16); h=random.choice([4,6,8,10,12,14,16]); return f"Asosi {a} sm va balandligi {h} sm bo'lgan uchburchak yuzi?", a*h//2

def f9_motion(grade):
    t=random.randrange(5)
    if t==0:
        v=random.randint(40,80); time=random.randint(2,5)
        return f"Avtomobil {v} km/soat tezlikda {time} soat yurdi. Masofa?", v*time
    if t==1:
        v=random.randint(40,80); time=random.randint(2,5); d=v*time
        return f"{d} km yo'l {time} soatda bosib o'tildi. Tezlik?", v
    if t==2:
        v=random.randint(40,80); time=random.randint(2,5); d=v*time
        return f"{d} km masofani {v} km/soat tezlikda bosib o'tish uchun vaqt?", time
    if t==3:
        v1=random.randint(40,70); v2=random.randint(30,60); time=random.randint(2,5); d=(v1+v2)*time
        return f"Ikki avtomobil bir-biriga qarab {v1} va {v2} km/soat tezliklarda yurdi. Ular orasidagi masofa {d} km. Uchrashish vaqti?", time
    v_slow=random.randint(30,55); diff=random.randint(10,30); time=random.randint(2,5); gap=diff*time
    return f"Quvib yetish masofasi {gap} km, vaqt {time} soat. Tezroq avtomobilning tezligi sekinroqnikidan nechaga katta?", diff

def f9_arith(grade):
    t=random.randrange(6); a1=random.randint(2,12); d=random.randint(2,9); n=random.randint(4,10); an=a1+(n-1)*d
    if t==0: return f"a₁={a1}, d={d}. a₍{n}₎ = ?", an
    if t==1: return f"a₁={a1}, d={d}. Dastlabki {n} ta had yig'indisi Sₙ = ?", n*(a1+an)//2
    if t==2: return f"Arifmetik progressiyada a₁={a1}, a₂={a1+d}. Ayirma d=?", d
    if t==3: return f"a₁={a1}, d={d}, aₙ={an}. n=?", n
    if t==4: return f"a₁={a1}, d={d}. a₃ = ?", a1+2*d
    return f"a₁={a1}, d={d}. a₂+a₃ = ?", (a1+d)+(a1+2*d)


def f9_geom(grade):
    t=random.randrange(5); a1=random.randint(1,5); q=random.choice([2,3]); n=random.randint(3,6); an=a1*q**(n-1)
    if t==0: return f"Geometrik progressiyada a₁={a1}, q={q}. a₍{n}₎ = ?", an
    if t==1: return f"a₁={a1}, q={q}. a₂ = ?", a1*q
    if t==2:
        a2=a1*q; return f"a₁={a1}, a₂={a2}. q=?", q
    if t==3: return f"a₁={a1}, q={q}. Dastlabki {n} ta had yig'indisi Sₙ=?", a1*(q**n-1)//(q-1)
    return f"a₁={a1}, q={q}. a₃ = ?", a1*q*q


def f9_bank(grade):
    deposit=random.choice([100000,200000,300000,400000,500000,600000]); pct=random.choice([5,10,15,20]); t=random.randrange(5)
    interest=deposit*pct//100
    if t==0: return f"{deposit} so'mning {pct}% i qancha?", interest
    if t==1: return f"{deposit} so'mga {pct}% qo'shilsa yangi summa qancha?", deposit+interest
    if t==2: return f"{deposit} so'mga {pct}% oddiy foiz 2 yil qo'shilsa umumiy summa qancha?", deposit+2*interest
    if t==3: return f"Narx {deposit} so'm edi va {pct}% ga kamaydi. Yangi narx qancha?", deposit-interest
    final=deposit+interest
    return f"Mahsulot narxi {pct}% oshgach {final} so'm bo'ldi. Dastlabki narx qancha edi?", deposit

def f9_trig(grade):
    t=random.randrange(10)
    triples=[(3,4,5),(5,12,13),(8,15,17),(7,24,25),(9,12,15)]
    if t==0: return "sin 30° + cos 60° = ?", 1
    if t==1: return "sin 90° + cos 0° = ?", 2
    if t==2: return "tan 45° = ?", 1
    if t==3:
        p,q,h=random.choice(triples); k=random.randint(1,5); return f"sin α = {p}/{h}. Gipotenuza {h*k} sm bo'lsa, α ga qarshi katet?", p*k
    if t==4:
        p,q,h=random.choice(triples); k=random.randint(1,5); return f"cos α = {q}/{h}. Gipotenuza {h*k} sm bo'lsa, α ga yopishgan katet?", q*k
    if t==5:
        p,q,h=random.choice(triples); k=random.randint(1,5); return f"tan α = {p}/{q}. α ga yopishgan katet {q*k} sm bo'lsa, qarshi katet?", p*k
    if t==6:
        p,q,h=random.choice(triples); return f"sin α = {p}/{h}. {h}²cos²α nechaga teng?", q*q
    if t==7:
        p,q,h=random.choice(triples); return f"cos α = {q}/{h}. {h}²sin²α nechaga teng?", p*p
    if t==8:
        k=random.randint(2,12); return f"sin α = 1/2 va gipotenuza {2*k} sm. α ga qarshi katet?", k
    k=random.randint(2,12); return f"cos α = 1/2 va gipotenuza {2*k} sm. α ga yopishgan katet?", k

def f9_word(grade):
    t=random.randrange(6)
    if t==0:
        price=random.choice([4000,5000,6000,8000]); count=random.randint(3,12)
        return f"Bir dona daftar {price} so'm. {count} ta daftar uchun jami qancha to'lanadi?", price*count
    if t==1:
        x=random.randint(5,20); total=4*x+6
        return f"Bir sonning 4 baravariga 6 qo'shilsa {total} chiqadi. Sonni toping.", x
    if t==2:
        base=random.choice([20000,30000,40000,50000]); p=random.choice([10,20,25]); return f"{base} so'mlik mahsulot {p}% ga arzonlashdi. Yangi narx qancha?", base-base*p//100
    if t==3:
        a=random.randint(5,12); d=random.randint(2,6); return f"Ketma-ketlik {a}, {a+d}, {a+2*d}, ... ko'rinishida. 5-had nechaga teng?", a+4*d
    if t==4:
        n=random.randint(4,12); return f"{n} ta o'quvchidan 1 nafar sardor tanlashning nechta usuli bor?", n
    a=random.randint(6,12); b=random.randint(3,8); total=a+b
    return f"Bir savatda {a} kg, ikkinchisida {b} kg meva bor. Jami mevaning necha kilogrammi bor?", total


GEN_F9_RATIONAL=[(f9_rational,{"medium"})]
GEN_F9_POWER=[(f9_power,{"medium"})]
GEN_F9_ROOT=[(f9_root,{"medium"})]
GEN_F9_ALGEBRA=[(f9_algebra,{"medium"})]
GEN_F9_FACTOR=[(f9_factor,{"medium"})]
GEN_F9_LINEAR=[(f9_linear,{"medium"})]
GEN_F9_QUAD=[(f9_quad,{"medium"})]
GEN_F9_SYSTEM=[(f9_system,{"medium"})]
GEN_F9_FUNCTION=[(f9_function,{"medium"})]
GEN_F9_INEQ=[(f9_inequality,{"medium"})]
GEN_F9_STAT=[(f9_statistics,{"medium"})]
GEN_F9_PROB=[(f9_probability,{"medium"})]
GEN_F9_LOGIC=[(f9_logic,{"medium"})]
GEN_F9_GEOM=[(f9_geometry,{"medium"})]
GEN_F9_MOTION=[(f9_motion,{"medium"})]
GEN_F9_ARITH=[(f9_arith,{"medium"})]
GEN_F9_GEOMPROG=[(f9_geom,{"medium"})]
GEN_F9_BANK=[(f9_bank,{"medium"})]
GEN_F9_TRIG=[(f9_trig,{"medium"})]
GEN_F9_WORD=[(f9_word,{"medium"})]

TOPICS.update({
    "rational9":"½ Ratsional sonlar va amallar",
    "power9":"xⁿ Darajalar va daraja xossalari",
    "root9":"√ Kvadrat ildizlar",
    "algebra9":"🔤 Algebraik ifodalar",
    "factor9":"🧩 Ko'paytuvchilarga ajratish",
    "linear9":"📐 Chiziqli tenglamalar",
    "quad9":"🔷 Kvadrat tenglamalar",
    "system9":"🔗 Tenglamalar sistemasi",
    "function9":"📈 Funksiya va grafik",
    "ineq9":"⚖️ Tengsizliklar",
    "stat9":"📊 Statistika",
    "prob9":"🎯 Ehtimollik",
    "logic9":"🧠 Mantiqiy masalalar",
    "geometry9":"📐 Geometriya",
    "motion9":"🚗 Harakat masalalari",
    "arith9":"🔢 Arifmetik progressiya",
    "geomprog9":"🔢 Geometrik progressiya",
    "bank9":"🏦 Foiz va amaliy hisoblar",
    "trig9":"📐 Trigonometriya",
    "word9":"🧩 Aralash mantiqiy masalalar",
})

GRADE_TOPICS["medium"]=[
    "rational9","power9","root9","algebra9","factor9","linear9","quad9",
    "system9","function9","ineq9","stat9","prob9","logic9","geometry9",
    "motion9","arith9","geomprog9","bank9","trig9","word9"
]

HINTS.update({
    "rational9":"Kasrlarni umumiy maxrajga keltiring; ko'paytirishda surat va maxrajni mos ravishda ko'paytiring.",
    "power9":"Bir xil asosli darajalarni ko'paytirishda ko'rsatkichlar qo'shiladi, bo'lishda ayriladi.",
    "root9":"√a — kvadrati a ga teng bo'lgan nomanfiy son.",
    "algebra9":"O'xshash hadlarni birlashtiring va qavslarni distributiv qonun bilan oching.",
    "factor9":"Avval umumiy ko'paytuvchini ajrating; zarur bo'lsa qisqa ko'paytirish formulalaridan foydalaning.",
    "linear9":"x li hadlarni bir tomonga, ozod hadlarni ikkinchi tomonga o'tkazing.",
    "quad9":"D=b²−4ac yoki Vieta formulalaridan masalaga mos ravishda foydalaning.",
    "system9":"Ikki tenglamani qo'shish-ayirish yoki o'rniga qo'yish usuli bilan yeching.",
    "function9":"y=kx+b da k — qiyalik, b — y o'qi bilan kesishish ordinatasi.",
    "ineq9":"Manfiy songa ko'paytirish yoki bo'lishda tengsizlik belgisi almashadi.",
    "stat9":"O'rtacha = yig'indi/sonlar soni; medianani tartiblangan qatorning o'rtasidan toping.",
    "prob9":"Ehtimollik = qulay holatlar soni / barcha teng imkoniyatli holatlar soni.",
    "logic9":"Shartlarni tenglama yoki jadvalga aylantirib, topilgan javobni qayta tekshiring.",
    "geometry9":"Kerakli formulani tanlang va o'lchov birliklarini bir xil qiling.",
    "motion9":"s=vt, v=s/t, t=s/v. Qarama-qarshi harakatda tezliklar qo'shiladi.",
    "arith9":"aₙ=a₁+(n−1)d; Sₙ=n(a₁+aₙ)/2.",
    "geomprog9":"aₙ=a₁qⁿ⁻¹; Sₙ=a₁(qⁿ−1)/(q−1), q≠1.",
    "bank9":"Foiz = asosiy summa × foiz / 100. Oddiy foizda har yilgi foiz bir xil asosdan olinadi.",
    "trig9":"Asosiy qiymatlar: sin30°=1/2, cos60°=1/2, sin90°=1, cos0°=1.",
    "word9":"Masala shartidan noma'lumni belgilang, tenglama tuzing va javobni shartga qo'yib tekshiring.",
})

FORMULAS.update({
"rational9":"½ RATSIONAL SONLAR\n\n• Kasrlarni umumiy maxrajga keltirib qo'shing/ayiring: a/b±c/d=(ad±bc)/(bd)\n• Bo'lishda ikkinchi kasr teskarilanadi: (a/b)÷(c/d)=(a/b)×(d/c)",
"power9":'xⁿ DARAJALAR\n\n• aᵐ×aⁿ=aᵐ⁺ⁿ\n• aᵐ÷aⁿ=aᵐ⁻ⁿ (a≠0)\n• (aᵐ)ⁿ=aᵐⁿ\n• a⁰=1 (a≠0);  a⁻ⁿ=1/aⁿ',
"root9":'√ KVADRAT ILDIZ\n\n• √a — a ning nomanfiy kvadrat ildizi (a≥0)\n• √(a·b)=√a·√b (a,b≥0);  √(a/b)=√a/√b (a≥0,b>0)\n• (√a)²=a (a≥0);  √(a²)=|a|',
"algebra9":"🔤 ALGEBRAIK IFODALAR (formula kitobi asosida)\n\n• Harf o'rniga qiymatni qo'ying\n• Amallar tartibi: qavs → daraja/ildiz → ko'paytirish/bo'lish → qo'shish/ayirish",
"factor9":"🧩 KO'PAYTUVCHILARGA AJRATISH\n\n• Umumiy ko'paytuvchini qavsdan tashqariga chiqarish\n• (a+b)²=a²+2ab+b²,  (a−b)²=a²−2ab+b²\n• a²−b²=(a−b)(a+b)\n• Guruhlash usuli: ax+ay+bx+by=a(x+y)+b(x+y)=(a+b)(x+y)",
"linear9":"📐 CHIZIQLI TENGLAMA\n\n• ax+b=cx+d shaklida x li hadlarni bir tomonga, ozod hadlarni ikkinchi tomonga o'tkazing: (a−c)x=d−b ⇒ x=(d−b)/(a−c)",
"quad9":"🔷 KVADRAT TENGLAMALAR (formula kitobi asosida)\n\n• ax²+bx+c=0 (a≠0);  D=b²−4ac\n• x₁,₂=(−b±√D)/(2a)\n• Viyet: x₁+x₂=−b/a, x₁·x₂=c/a\n• D>0 — 2 ildiz, D=0 — 1 ildiz, D<0 — ildiz yo'q",
"system9":"🔗 IKKI NOMA'LUMLI CHIZIQLI TENGLAMALAR SISTEMASI (formula kitobi asosida)\n\n• Umumiy ko'rinish: a₁x + b₁y = c₁,  a₂x + b₂y = c₂\n1) Yechimga ega emas: a₁/a₂ = b₁/b₂ ≠ c₁/c₂\n2) Yagona yechim: a₁/a₂ ≠ b₁/b₂\n3) Cheksiz ko'p yechim: a₁/a₂ = b₁/b₂ = c₁/c₂\n\n• O'RNIGA QO'YISH USULI: bitta tenglamadan bitta o'zgaruvchini ifodalab (masalan y=...), ikkinchi tenglamaga qo'yiladi\n• QO'SHISH (ALGEBRAIK) USULI: tenglamalarni bir xil koeffitsientlar hosil bo'ladigan songa ko'paytirib, qo'shish yoki ayirish orqali bitta o'zgaruvchi yo'qotiladi",
"function9":"📈 FUNKSIYA\n\n• y=kx+b da k — qiyalik (burchak koeffitsiyenti), b — y o'qi bilan kesishish ordinatasi\n• y=ax²+bx+c — parabola, uchi x₀=−b/(2a), y₀=(4ac−b²)/(4a)",
"ineq9":"⚖️ TENGSIZLIK\n\n• Manfiy songa ko'paytirish yoki bo'lishda tengsizlik belgisi almashadi\n• Kvadrat tengsizlikda: parabolaning OX o'qi bilan kesishish nuqtalari va shoxobchalar yo'nalishini (a ishorasini) tahlil qiling",
"stat9":"📊 STATISTIKA\n\n• O'rtacha (o'rta arifmetik) = yig'indi/n\n• Moda — eng ko'p uchragan qiymat\n• Mediana — tartiblangan qatorning o'rtasidagi qiymat (juft sonda o'rtadagi ikkitasining o'rta arifmetigi)",
"prob9":"🎯 11-SINF — EHTIMOLLIK\n\n• P(A)=m/n — teng imkoniyatli natijalarda (m — qulay, n — barcha holatlar)\n• P(A')=1−P(A)\n• Mustaqil hodisalar: P(A∩B)=P(A)·P(B)\n• Umumiy qoida: P(A∪B)=P(A)+P(B)−P(A∩B)\n• Shartli ehtimollik: P(A|B)=P(A∩B)/P(B), P(B)>0.",
"geometry9":"📐 GEOMETRIYA\n\n• Kerakli formulani tanlang va o'lchov birliklarini bir xil qiling\n• Uchburchak: S=Geron formulasi yoki S=(1/2)ab·sinγ\n• Sinuslar/kosinuslar teoremasidan foydalaning",
"motion9":"🚗 HARAKAT MASALALARI\n\n• s=vt,  v=s/t,  t=s/v\n• Qarama-qarshi harakatda tezliklar qo'shiladi: v=v₁+v₂\n• Quvib o'tishda tezliklar farqi olinadi: v=v₁−v₂",
"arith9":"🔢 ARIFMETIK PROGRESSIYA (formula kitobi asosida)\n\n• Ayirma: d = a_(n+1) − a_n = a₂ − a₁\n• n-had: aₙ = a₁ + (n−1)·d\n• d = (aₙ−a₁)/(n−1),  n = (aₙ−a₁)/d + 1\n• O'rta had: a_o'rt = (a₁+aₙ)/2\n• Yig'indi: Sₙ = ((a₁+aₙ)/2)·n = ((2a₁+(n−1)d)/2)·n\n• a,b,c ketma-ket had bo'lishi sharti: 2b = a+c\n• aₘ+aₙ = 2aₖ, agar m+n=2k bo'lsa",
"geomprog9":"🔢 GEOMETRIK PROGRESSIYA (formula kitobi asosida)\n\n• Maxraj: q = b_(n+1) ÷ b_n\n• n-had: bₙ = b₁·qⁿ⁻¹\n• O'rta had: b_o'rt = √(b₁·bₙ)\n• Yig'indi (q≠1): Sₙ = b₁(1−qⁿ)/(1−q) = b₁(qⁿ−1)/(q−1)\n• Agar q=1: Sₙ = n·b₁\n• Cheksiz kamayuvchi progressiya (|q|<1): S = b₁/(1−q)\n• a,b,c ketma-ket had bo'lishi sharti: b² = a·c",
"bank9":"🏦 FOIZ MASALALARI\n\n• Foiz = asosiy summa × foiz / 100\n• Oddiy foizda har yilgi foiz bir xil asosdan olinadi: Summa=A+A·P·n/100\n• Murakkab foizda: Summa=A·(1+P/100)ⁿ",
"trig9":"📐 TRIGONOMETRIK FUNKSIYALAR (formula kitobi asosida)\n\nAsosiy burchaklar jadvali:\n• 0°: sin=0, cos=1, tg=0, ctg — mavjud emas\n• 30°: sin=1/2, cos=√3/2, tg=√3/3, ctg=√3\n• 45°: sin=√2/2, cos=√2/2, tg=1, ctg=1\n• 60°: sin=√3/2, cos=1/2, tg=√3, ctg=√3/3\n• 90°: sin=1, cos=0, tg — mavjud emas, ctg=0\n\nAsosiy ayniyatlar:\n• sin²x + cos²x = 1\n• tgx = sinx/cosx,  ctgx = cosx/sinx,  tgx·ctgx = 1\n• 1 + tg²x = 1/cos²x,   1 + ctg²x = 1/sin²x\n\nYig'indi/ayirma formulalari:\n• sin(x±y) = sinx·cosy ± cosx·siny\n• cos(x±y) = cosx·cosy ∓ sinx·siny\n• tg(x±y) = (tgx ± tgy)/(1 ∓ tgx·tgy)\n\nKarrali burchak:\n• sin2x = 2sinx·cosx\n• cos2x = cos²x − sin²x = 2cos²x − 1 = 1 − 2sin²x\n• tg2x = 2tgx/(1−tg²x)",
"word9":"📝 MATNLI MASALALAR\n\n• Masala shartidan noma'lumni belgilang (x)\n• Tenglama yoki tengsizlik tuzing\n• Yechimni topib, javobni masala shartiga qo'yib tekshiring",
"logic9":"🧠 MANTIQIY MASALALAR\n\n• Shartlarni ajrating, noma'lumni belgilang (x), tenglama yoki tengsizlik tuzing\n• Topilgan javobni masala shartiga qayta qo'yib tekshiring"
})

TOPIC_GENERATORS.update({
    "rational9":GEN_F9_RATIONAL,"power9":GEN_F9_POWER,"root9":GEN_F9_ROOT,
    "algebra9":GEN_F9_ALGEBRA,"factor9":GEN_F9_FACTOR,"linear9":GEN_F9_LINEAR,
    "quad9":GEN_F9_QUAD,"system9":GEN_F9_SYSTEM,"function9":GEN_F9_FUNCTION,
    "ineq9":GEN_F9_INEQ,"stat9":GEN_F9_STAT,"prob9":GEN_F9_PROB,"logic9":GEN_F9_LOGIC,
    "geometry9":GEN_F9_GEOM,"motion9":GEN_F9_MOTION,"arith9":GEN_F9_ARITH,
    "geomprog9":GEN_F9_GEOMPROG,"bank9":GEN_F9_BANK,"trig9":GEN_F9_TRIG,"word9":GEN_F9_WORD,
})


# ============================================================
# ---------- 10-sinf professional generatorlari ----------
# Har bir generator faqat "hard" darajada ishlaydi.
# Javoblar Telegramdagi 4 ta butun-sonli variant tizimiga mos ravishda integer.
# ============================================================

def _f10_quad_roots():
    r1, r2 = random.sample(range(-9, 10), 2)
    if r1 == 0 or r2 == 0 or r1 == r2:
        return _f10_quad_roots()
    b = -(r1 + r2); c = r1 * r2
    return r1, r2, b, c


def f10_rational(grade):
    a, b, c, d = random.sample(range(2, 13), 4)
    # (a/b) / (c/d), faqat butun natija chiqadigan holat
    num = a * d; den = b * c
    if num % den != 0:
        return f10_rational(grade)
    return f"({a}/{b}) ÷ ({c}/{d}) = ?", num // den


def f10_power(grade):
    base = random.choice([2, 3, 4, 5])
    k = random.randint(1, 3)
    n = random.randint(k + 1, 5)
    # a^n / a^k = a^(n-k), javob har doim butun son.
    return f"{base}^{n} ÷ {base}^{k} = ?", base ** (n-k)


def f10_radical(grade):
    n = random.choice([2, 3, 5, 6, 7, 10, 11, 13, 14, 15, 17, 19, 21, 22, 23, 26, 29, 30, 33, 35])
    a = random.randint(2, 8)
    value = (a*a) * n
    return f"√({value}) ni a√b ko‘rinishida yozing. a koeffitsientini toping.", a


def f10_log(grade):
    base = random.choice([2, 3, 5, 10])
    p = random.randint(2, 5)
    return f"log_{base}({base**p}) = ?", p


def f10_expo(grade):
    base = random.choice([2, 3, 5])
    x = random.randint(2, 6)
    extra = random.randint(1, 8)
    rhs = base ** x * (base ** extra)
    return f"{base}^(x+{extra}) = {rhs} bo‘lsa, x ni toping.", x


def f10_trig(grade):
    # 10-sinf uchun ko‘plab turli Pifagor uchliklari: savol mazmuni ham, sonlari ham almashadi.
    triples = [
        (3,4,5),(5,12,13),(6,8,10),(7,24,25),(8,15,17),(9,12,15),
        (9,40,41),(10,24,26),(12,16,20),(12,35,37),(14,48,50),
        (15,20,25),(16,30,34),(18,24,30),(20,21,29),(20,48,52),
        (21,28,35),(24,32,40),(28,45,53),(30,40,50)
    ]
    a,b,c = random.choice(triples)
    mode = random.choice([0,1,2])
    if mode == 0:
        return f"To‘g‘ri burchakli uchburchakda α burchakka qarshi katet {a}, gipotenuza {c}. sin α = p/{c} bo‘lsa, p ni toping.", a
    if mode == 1:
        return f"To‘g‘ri burchakli uchburchakda α burchakka yopishgan katet {a}, gipotenuza {c}. cos α = p/{c} bo‘lsa, p ni toping.", a
    return f"To‘g‘ri burchakli uchburchakda α burchakka qarshi katet {a}, yopishgan katet {b}. tan α = p/{b} bo‘lsa, p ni toping.", a


def f10_trig_eq(grade):
    # Bir xil tenglamani qaytarmaslik uchun koeffitsientli ko‘rinishlar va turli burchaklar.
    facts = [("sin", 30, 1, 2), ("cos", 60, 1, 2), ("sin", 90, 1, 1),
             ("cos", 0, 1, 1), ("tan", 45, 1, 1)]
    fn, angle, num, den = random.choice(facts)
    k = random.randint(2, 12)
    if num == den:
        return f"0° ≤ x ≤ 90° da {k}{fn}(x) = {k} tenglamani yeching. x = ?", angle
    rhs_num = k * num
    return f"0° ≤ x ≤ 90° da {k}{fn}(x) = {rhs_num}/{den} tenglamani yeching. x = ?", angle


def f10_system(grade):
    x = random.randint(2, 9); y = random.randint(2, 9)
    while True:
        a = random.randint(2, 6); b = random.randint(2, 6)
        d = random.randint(2, 6); e = random.randint(2, 6)
        if a*e - b*d != 0:
            break
    c = a*x + b*y
    f = d*x + e*y
    return f"{a}x + {b}y = {c};  {d}x + {e}y = {f}. x ni toping.", x


def f10_quad(grade):
    r1, r2, b, c = _f10_quad_roots()
    return f"x² {b:+d}x {c:+d} = 0 tenglamaning katta ildizini toping.", max(r1, r2)


def f10_quad_ineq(grade):
    # (x-r1)(x-r2) > 0; so‘ralgan eng kichik butun yechim, chegaralardan tashqarida
    r1, r2 = sorted(random.sample(range(-6, 7), 2))
    ans = r2 + 1
    return f"(x-{r1})(x-{r2}) > 0 tengsizlikning eng kichik butun yechimini toping.", ans


def f10_function(grade):
    a = random.randint(2, 7); b = random.randint(-9, 9); x = random.randint(-5, 7)
    return f"f(x)={a}x{b:+d}. f({x}) ni toping.", a*x+b


def f10_sequence(grade):
    a1 = random.randint(2, 12); d = random.randint(2, 9); n = random.randint(5, 12)
    return f"Arifmetik ketma-ketlikda a₁={a1}, d={d}. a_{n} ni toping.", a1+(n-1)*d


def f10_arith(grade):
    a1 = random.randint(1, 10); d = random.randint(2, 8); n = random.randint(5, 10)
    an = a1+(n-1)*d
    return f"a₁={a1}, d={d}. Dastlabki {n} ta had yig‘indisini toping.", n*(a1+an)//2


def f10_geom(grade):
    a1 = random.choice([1,2,3,4]); q = random.choice([2,3]); n = random.randint(3, 6)
    return f"Geometrik progressiyada a₁={a1}, q={q}. a_{n} ni toping.", a1*q**(n-1)


def f10_combinatorics(grade):
    n = random.randint(5, 9); k = random.randint(2, 3)
    # C(n,k)
    from math import comb
    return f"{n} ta o‘quvchidan {k} kishilik guruhni nechta usulda tanlash mumkin?", comb(n,k)


def f10_probability(grade):
    pairs = [(1,1),(1,4),(1,9),(2,3),(2,8),(3,7),(4,6),(5,5),(5,15)]
    red, blue = random.choice(pairs)
    total = red + blue
    if 100 % total == 0:
        return f"Qutida {red} ta qizil va {blue} ta ko‘k shar bor. Tasodifiy bitta shar olinganda qizil chiqish ehtimolini foizda toping.", red*100//total
    return f"Qutida {red} ta qizil va {blue} ta ko‘k shar bor. Qizil shar chiqish ehtimolining qisqartirilgan kasr maxrajini toping.", total // math.gcd(red,total)


def f10_statistics(grade):
    vals = random.sample(range(5, 40), 5)
    # average is integer
    rem = sum(vals) % 5
    vals[-1] += (5-rem) % 5
    return f"{vals} sonlarining o‘rtacha arifmetik qiymatini toping.", sum(vals)//5


def f10_analytic(grade):
    # koordinata tekisligida masofa: 3-4-5 yoki 5-12-13 uchliklari
    triples = [(3,4,5),(5,12,13),(6,8,10),(8,15,17)]
    dx, dy, dist = random.choice(triples)
    x1, y1 = random.randint(-5,5), random.randint(-5,5)
    return f"A({x1},{y1}) va B({x1+dx},{y1+dy}) nuqtalar orasidagi masofani toping.", dist


def f10_word(grade):
    # Ish unumdorligi: birinchi ishchi a soat, ikkinchisi b soat; birgalikda necha soat?
    a, b = random.choice([(2,6),(3,6),(4,12),(5,10)])
    # 1/T = 1/a + 1/b => T=ab/(a+b), faqat integer holatlar uchun
    num=a*b; den=a+b
    if num % den != 0:
        return f10_word(grade)
    t=num//den
    return f"Bir ishchi vazifani {a} soatda, ikkinchisi {b} soatda bajaradi. Ular birga ishlasa, vazifa necha soatda tugaydi?", t


def f10_logic(grade):
    # Turli mantiqiy tur: uch xonali son raqamlari yig‘indisi va o‘nlik raqami
    a = random.randint(2,8); b = random.randint(1,8); c = random.randint(1,8)
    if len({a,b,c}) < 3:
        return f10_logic(grade)
    number = 100*a + 10*b + c
    # Savol: sonni 9 ga bo‘lgandagi qoldiq = raqamlar yig‘indisi mod 9; javob qoldiq
    ans = (a+b+c) % 9
    return f"{number} sonini 9 ga bo‘lgandagi qoldiqni toping.", ans


GEN_F10 = {
    "rational10": [(f10_rational, {"hard"})],
    "power10": [(f10_power, {"hard"})],
    "radical10": [(f10_radical, {"hard"})],
    "log10": [(f10_log, {"hard"})],
    "expo10": [(f10_expo, {"hard"})],
    "trig10": [(f10_trig, {"hard"})],
    "trig_eq10": [(f10_trig_eq, {"hard"})],
    "system10": [(f10_system, {"hard"})],
    "quad10": [(f10_quad, {"hard"})],
    "quad_ineq10": [(f10_quad_ineq, {"hard"})],
    "function10": [(f10_function, {"hard"})],
    "sequence10": [(f10_sequence, {"hard"})],
    "arith10": [(f10_arith, {"hard"})],
    "geom10": [(f10_geom, {"hard"})],
    "combinatorics10": [(f10_combinatorics, {"hard"})],
    "probability10": [(f10_probability, {"hard"})],
    "statistics10": [(f10_statistics, {"hard"})],
    "analytic10": [(f10_analytic, {"hard"})],
    "word10": [(f10_word, {"hard"})],
    "logic10": [(f10_logic, {"hard"})],
}

TOPIC_GENERATORS.update(GEN_F10)

for _k, _label in {
    "rational10":"➗ Ratsional ifodalar",
    "power10":"xⁿ Daraja qonunlari",
    "radical10":"√ Ildizli ifodalar",
    "log10":"📈 Logarifmlar",
    "expo10":"📶 Ko‘rsatkichli tenglamalar",
    "trig10":"📐 Trigonometriya",
    "trig_eq10":"📐 Trigonometrik tenglamalar",
    "system10":"🔗 Tenglamalar sistemasi",
    "quad10":"🔷 Kvadrat tenglamalar",
    "quad_ineq10":"⚖️ Kvadrat tengsizliklar",
    "function10":"📈 Funksiya va qiymatlar",
    "sequence10":"🔢 Ketma-ketliklar",
    "arith10":"🔢 Arifmetik progressiya",
    "geom10":"🔢 Geometrik progressiya",
    "combinatorics10":"🎲 Kombinatorika",
    "probability10":"🎯 Ehtimollik",
    "statistics10":"📊 Statistika",
    "analytic10":"📍 Analitik geometriya",
    "word10":"🧩 Murakkab amaliy masalalar",
    "logic10":"🧠 Mantiqiy fikrlash",
}.items():
    TOPICS[_k] = _label

FORMULAS.update({
    "rational10": (
        "➗ RATSIONAL IFODALAR (formula kitobi asosida)\n\n"
        "• Kasrlarni umumiy maxrajga keltirib qo'shish/ayirish: a/b±c/d=(ad±bc)/(bd)\n"
        "• Ko'paytirish: (a/b)(c/d)=(ac)/(bd);  Bo'lish: (a/b)÷(c/d)=(a/b)(d/c)\n"
        "• Qisqartirish uchun surat va maxrajni ko'paytuvchilarga ajrating\n"
        "• Aniqlanish sohasi: maxraj ≠ 0"
    ),
    "power10": (
        "xⁿ DARAJA QONUNLARI (formula kitobi asosida)\n\n"
        "1) a⁰=1 (a≠0)         6) aᵖ·aᵠ=aᵖ⁺ᵠ\n"
        "2) a⁻ⁿ=1/aⁿ            7) aᵖ÷aᵠ=aᵖ⁻ᵠ\n"
        "3) aˡ/ᵗ=ᵗ√(aˡ)         8) (aᵖ)ᵠ=aᵖᵠ\n"
        "4) a⁻ˡ/ᵗ=1/ᵗ√(aˡ)      9) (ab)ᵖ=aᵖbᵖ\n"
        "5) (a/b)ᵖ=aᵖ/bᵖ"
    ),
    "radical10": (
        "√ ILDIZLI IFODALAR (formula kitobi asosida)\n\n"
        "• ᵏ√(ab)=ᵏ√a·ᵏ√b;  ᵏ√(a/b)=ᵏ√a÷ᵏ√b (b≠0)\n"
        "• ᵏ√(ᵗ√a)=ᵏᵗ√a\n"
        "• ²ⁿ√(a²ⁿ)=|a|;  ²ⁿ⁺¹√(a²ⁿ⁺¹)=a\n"
        "• Ratsionallashtirish: 1/√a = √a/a;  1/(√a±√b) = (√a∓√b)/(a−b)"
    ),
    "log10": (
        "📈 LOGARIFMLAR (formula kitobi asosida)\n\n"
        "log_a b (a>0, a≠1, b>0)\n"
        "• log_a a=1,  log_a 1=0,  a^(log_a b)=b\n"
        "• log_a(bc)=log_a b+log_a c;  log_a(b/c)=log_a b−log_a c\n"
        "• log_a(bᵖ)=p·log_a b\n"
        "• Asos almashtirish: log_a b = log_c b / log_c a"
    ),
    "expo10": (
        "📶 11-SINF — KO'RSATKICHLI TENGLAMALAR\n\n• aˣ=aʸ ⇔ x=y (a>0, a≠1)\n• aˣ·aʸ=aˣ⁺ʸ;  aˣ/aʸ=aˣ⁻ʸ;  (aˣ)ʸ=aˣʸ\n• Asos bir xil bo'lmasa, ikkala tomonni bir xil asosga keltiring yoki t=aˣ almashtirishdan foydalaning\n• Ko'rsatkichli tengsizlikda: a>1 bo'lsa funksiya o'suvchi, 0<a<1 bo'lsa kamayuvchi (belgi shunga qarab saqlanadi/almashadi)."
    ),
    "trig10": (
        "📐 TRIGONOMETRIK FUNKSIYALAR (formula kitobi asosida)\n\nAsosiy burchaklar jadvali:\n• 0°: sin=0, cos=1, tg=0, ctg — mavjud emas\n• 30°: sin=1/2, cos=√3/2, tg=√3/3, ctg=√3\n• 45°: sin=√2/2, cos=√2/2, tg=1, ctg=1\n• 60°: sin=√3/2, cos=1/2, tg=√3, ctg=√3/3\n• 90°: sin=1, cos=0, tg — mavjud emas, ctg=0\n\nAsosiy ayniyatlar:\n• sin²x + cos²x = 1\n• tgx = sinx/cosx,  ctgx = cosx/sinx,  tgx·ctgx = 1\n• 1 + tg²x = 1/cos²x,   1 + ctg²x = 1/sin²x\n\nYig'indi/ayirma formulalari:\n• sin(x±y) = sinx·cosy ± cosx·siny\n• cos(x±y) = cosx·cosy ∓ sinx·siny\n• tg(x±y) = (tgx ± tgy)/(1 ∓ tgx·tgy)\n\nKarrali burchak:\n• sin2x = 2sinx·cosx\n• cos2x = cos²x − sin²x = 2cos²x − 1 = 1 − 2sin²x\n• tg2x = 2tgx/(1−tg²x)"
    ),
    "trig_eq10": (
        "🔺 11-SINF — TRIGONOMETRIK TENGLAMALAR\n\n• sinx=a (−1≤a≤1): x=(−1)ⁿ arcsin a + πn, n∈Z\n• cosx=a (−1≤a≤1): x=±arccos a + 2πn, n∈Z\n• tgx=a: x=arctg a + πn, n∈Z\n• ctgx=a: x=arcctg a + πn, n∈Z\n• Xususiy holatlar: sinx=0 ⇒ x=πn; sinx=1 ⇒ x=π/2+2πn; cosx=0 ⇒ x=π/2+πn\n• Berilgan intervalda yechim qidirishda umumiy yechimga har xil n qiymatlarini qo'yib tekshiring."
    ),
    "system10": (
        "🔗 IKKI NOMA'LUMLI CHIZIQLI TENGLAMALAR SISTEMASI (formula kitobi asosida)\n\n• Umumiy ko'rinish: a₁x + b₁y = c₁,  a₂x + b₂y = c₂\n1) Yechimga ega emas: a₁/a₂ = b₁/b₂ ≠ c₁/c₂\n2) Yagona yechim: a₁/a₂ ≠ b₁/b₂\n3) Cheksiz ko'p yechim: a₁/a₂ = b₁/b₂ = c₁/c₂\n\n• O'RNIGA QO'YISH USULI: bitta tenglamadan bitta o'zgaruvchini ifodalab (masalan y=...), ikkinchi tenglamaga qo'yiladi\n• QO'SHISH (ALGEBRAIK) USULI: tenglamalarni bir xil koeffitsientlar hosil bo'ladigan songa ko'paytirib, qo'shish yoki ayirish orqali bitta o'zgaruvchi yo'qotiladi"
    ),
    "quad10": (
        "🔷 KVADRAT TENGLAMALAR (formula kitobi asosida)\n\n"
        "• ax²+bx+c=0 (a≠0);  D=b²−4ac\n"
        "• x₁,₂=(−b±√D)/(2a)\n"
        "• Viyet: x₁+x₂=−b/a, x₁·x₂=c/a\n"
        "• D>0 — 2 ildiz, D=0 — 1 ildiz, D<0 — ildiz yo'q"
    ),
    "quad_ineq10": (
        "⚖️ KVADRAT TENGSIZLIKLAR (formula kitobi asosida)\n\n"
        "• ax²+bx+c=0 ning ildizlarini toping (x₁<x₂)\n"
        "• a>0, D>0: ax²+bx+c>0 ⟺ x∈(−∞;x₁)∪(x₂;+∞);  <0 ⟺ x∈(x₁;x₂)\n"
        "• a<0 bo'lsa, natijalar teskarisiga almashadi\n"
        "• Parabolaning shoxobchalari yo'nalishi (a ishorasi) va ildizlar orasidagi/tashqarisidagi oraliqni tekshiring"
    ),
    "function10": (
        "📈 FUNKSIYA VA QIYMATLAR (formula kitobi asosida)\n\n"
        "• y=kx+b — chiziqli, k — burchak koeffitsiyenti\n"
        "• y=ax²+bx+c — parabola, uchi x₀=−b/(2a), y₀=(4ac−b²)/(4a)\n"
        "• Aniqlanish sohasi: maxraj≠0, juft ildiz osti≥0, logarifm argumenti>0"
    ),
    "sequence10": (
        '🔢 11-SINF — KETMA-KETLIKLAR\n\n• Arifmetik: aₙ=a₁+(n−1)d;  Sₙ=n(a₁+aₙ)/2\n• Geometrik: bₙ=b₁qⁿ⁻¹;  Sₙ=b₁(qⁿ−1)/(q−1), q≠1\n• Cheksiz kamayuvchi geometrik progressiya (|q|<1): S=b₁/(1−q)\n• Rekurrent ketma-ketlikda berilgan oldingi hadlardan keyingi hadni topish mumkin.'
    ),
    "arith10": (
        "🔢 ARIFMETIK PROGRESSIYA (formula kitobi asosida)\n\n• Ayirma: d = a_(n+1) − a_n = a₂ − a₁\n• n-had: aₙ = a₁ + (n−1)·d\n• d = (aₙ−a₁)/(n−1),  n = (aₙ−a₁)/d + 1\n• O'rta had: a_o'rt = (a₁+aₙ)/2\n• Yig'indi: Sₙ = ((a₁+aₙ)/2)·n = ((2a₁+(n−1)d)/2)·n\n• a,b,c ketma-ket had bo'lishi sharti: 2b = a+c\n• aₘ+aₙ = 2aₖ, agar m+n=2k bo'lsa"
    ),
    "geom10": (
        "🔢 GEOMETRIK PROGRESSIYA (formula kitobi asosida)\n\n• Maxraj: q = b_(n+1) ÷ b_n\n• n-had: bₙ = b₁·qⁿ⁻¹\n• O'rta had: b_o'rt = √(b₁·bₙ)\n• Yig'indi (q≠1): Sₙ = b₁(1−qⁿ)/(1−q) = b₁(qⁿ−1)/(q−1)\n• Agar q=1: Sₙ = n·b₁\n• Cheksiz kamayuvchi progressiya (|q|<1): S = b₁/(1−q)\n• a,b,c ketma-ket had bo'lishi sharti: b² = a·c"
    ),
    "combinatorics10": (
        "🎲 11-SINF — KOMBINATORIKA\n\n• n!=1·2·...·n,  0!=1\n• Pₙ=n! — o'rin almashtirish\n• Aₙᵏ=n!/(n−k)! — joylashtirish (tartib MUHIM)\n• Cₙᵏ=n!/[k!(n−k)!] — kombinatsiya (tartib MUHIM EMAS)\n• Cₙᵏ=Cₙⁿ⁻ᵏ;  Cₙ⁰+Cₙ¹+...+Cₙⁿ=2ⁿ\n• Murakkab tanlashlarda holatlarni ajratib, qo'shish yoki ko'paytirish qoidasidan foydalaning."
    ),
    "probability10": (
        "🎯 11-SINF — EHTIMOLLIK\n\n• P(A)=m/n — teng imkoniyatli natijalarda (m — qulay, n — barcha holatlar)\n• P(A')=1−P(A)\n• Mustaqil hodisalar: P(A∩B)=P(A)·P(B)\n• Umumiy qoida: P(A∪B)=P(A)+P(B)−P(A∩B)\n• Shartli ehtimollik: P(A|B)=P(A∩B)/P(B), P(B)>0."
    ),
    "statistics10": (
        "📊 STATISTIKA\n\n• O'rtacha (o'rta arifmetik) = yig'indi/n\n• Moda — eng ko'p uchragan qiymat\n• Mediana — tartiblangan qatorning o'rtasidagi qiymat (juft sonda o'rtadagi ikkitasining o'rta arifmetigi)"
    ),
    "analytic10": (
        "📍 11-SINF — ANALITIK GEOMETRIYA\n\n• Ikki nuqta orasidagi masofa: d=√((x₂−x₁)²+(y₂−y₁)²)\n• Kesma o'rtasining koordinatasi: M((x₁+x₂)/2, (y₁+y₂)/2)\n• To'g'ri chiziq tenglamasi: y−y₀=k(x−x₀), k=tgα\n• Ikki nuqtadan o'tuvchi to'g'ri chiziq: (y−y₁)/(y₂−y₁)=(x−x₁)/(x₂−x₁)\n• Parallellik sharti: k₁=k₂;  Perpendikulyarlik sharti: k₁·k₂=−1\n• Markazi C(a,b), radiusi R bo'lgan aylana: (x−a)²+(y−b)²=R²\n• Nuqtadan to'g'ri chiziqqacha masofa: d=|Ax₀+By₀+C|/√(A²+B²)"
    ),
    "word10": (
        "🧩 MURAKKAB AMALIY MASALALAR (formula kitobi asosida)\n\n"
        "• Noma'lumni belgilang, shartlarni tenglama/tengsizlikka aylantiring\n"
        "• s=vt, ishga oid t=A/x, foizga oid formulalardan mos holini tanlang\n"
        "• Yechimni topib, masala shartiga qayta qo'yib tekshiring"
    ),
    "logic10": (
        "🧠 MANTIQIY FIKRLASH (formula kitobi asosida)\n\n"
        "• EKUB(a,b)×EKUK(a,b)=a×b\n"
        "• Shartlarni jadval yoki holatlar bo'yicha ajrating\n"
        "• Barcha shart bir vaqtda bajariladigan holatni qidiring"
    ),
})


# 10-sinf mavzularida ham bir xil qolip ketma-ket kelmasligi uchun mavjud
# generatorlar orasiga mustaqil ikkinchi yo'nalishlar qo'shiladi.
def f10_logic_v2(grade):
    a=random.randint(2,7); b=random.randint(2,7)
    return f"Ikki sonning EKUBi {math.gcd(a,b)} va EKUKi {math.lcm(a,b)}. Ularning ko'paytmasi nechaga teng?", a*b

def f10_function_v2(grade):
    a=random.randint(2,6); b=random.randint(-5,5); x=random.randint(-3,4)
    return f"f(x)={a}x{b:+d}. f(x)={a*x+b} bo'lsa, x ni toping.", x

def f10_probability_v2(grade):
    return "Ikki oddiy kubik tashlandi. Yig'indi 7 bo'lishining nechta tartibli holati bor?", 6

TOPIC_GENERATORS["logic10"].append((f10_logic_v2,{"hard"}))
TOPIC_GENERATORS["function10"].append((f10_function_v2,{"hard"}))
TOPIC_GENERATORS["probability10"].append((f10_probability_v2,{"hard"}))


# ==================== 11-SINF PROFESSIONAL BAZA ====================
# 11-sinf savollari kichik sonlar bilan ham mantiqiy fikrlashni talab qiladi.
# Har bir mavzuda mazmuni va yechish g'oyasi bir-biridan farq qiladi.

GRADE_LABELS["eleventh"] = "⚫ 11-sinf"

GRADE_TOPICS["eleventh"] = [
    "logic11", "function11", "derivative11", "derivative_app11",
    "integral11", "integral_area11", "trig11", "trig_eq11",
    "log11", "expo11", "sequence11", "probability11",
    "combinatorics11", "vector11", "solid11", "analytic11",
    "complex11", "optimization11", "parameter11", "proof11",
]

TOPICS.update({
    "logic11": "🧠 Mantiqiy tahlil",
    "function11": "📈 Funksiya xossalari",
    "derivative11": "📐 Hosila",
    "derivative_app11": "🎯 Hosilaning tatbiqlari",
    "integral11": "∫ Integral",
    "integral_area11": "📏 Integral va yuza",
    "trig11": "📐 Trigonometrik ayniyatlar",
    "trig_eq11": "🔺 Trigonometrik tenglamalar",
    "log11": "📊 Logarifmik tenglamalar",
    "expo11": "📶 Ko'rsatkichli tenglamalar",
    "sequence11": "🔢 Ketma-ketliklar",
    "probability11": "🎯 Ehtimollik",
    "combinatorics11": "🎲 Kombinatorika",
    "vector11": "➡️ Vektorlar",
    "solid11": "🧊 Fazoviy geometriya",
    "analytic11": "📍 Analitik geometriya",
    "complex11": "🔷 Kompleks sonlar",
    "optimization11": "⚙️ Optimallashtirish",
    "parameter11": "🧩 Parametrli masalalar",
    "proof11": "📝 Isbot va mulohaza",
})

FORMULAS.update({
    "logic11": (
        "🧠 11-SINF — MANTIQIY TAHLIL\n\n"
        "• Qarama-qarshi hodisalar: P(A') = 1 − P(A)\n"
        "• Agar A va B bir vaqtda sodir bo'la olmasa: P(A∪B)=P(A)+P(B)\n"
        "• Umumiy holda: P(A∪B)=P(A)+P(B)−P(A∩B)\n"
        "• Zarur va yetarli shartlarni alohida tekshiring.\n"
        "• Qarama-qarshilik usuli: faraz → zidlik → xulosa.\n"
        "• Inkor qoidasi: ¬(A∧B)=¬A∨¬B, ¬(A∨B)=¬A∧¬B."
    ),
    "function11": (
        "📈 11-SINF — FUNKSIYA XOSSALARI\n\n"
        "• Aniqlanish sohasi: maxraj ≠ 0, juft darajali ildiz osti ≥ 0, logarifm argumenti > 0.\n"
        "• Juft funksiya: f(−x)=f(x), grafigi OY o'qiga nisbatan simmetrik.\n"
        "• Toq funksiya: f(−x)=−f(x), grafigi koordinata boshiga nisbatan simmetrik.\n"
        "• Teskari funksiya mavjud bo'lishi uchun mos sohada bir qiymatlilik (monotonlik) kerak.\n"
        "• Kompozitsiya: (f∘g)(x)=f(g(x))."
    ),
    "derivative11": (
        "📐 11-SINF — HOSILA (formula kitobi asosida)\n\n"
        "• y'=f'(x)=lim(Δx→0) [f(x+Δx)−f(x)]/Δx\n"
        "• (C)'=0,  (x)'=1,  (xⁿ)'=n·xⁿ⁻¹\n"
        "• (C·f)'=C·f',  (f±g)'=f'±g'\n"
        "• (f·g)'=f'g+fg'\n"
        "• (f/g)'=(f'g−fg')/g² (g≠0)\n"
        "• (√x)'=1/(2√x)\n"
        "• (sinx)'=cosx,  (cosx)'=−sinx\n"
        "• (tgx)'=1/cos²x,  (ctgx)'=−1/sin²x\n"
        "• (aˣ)'=aˣ·lna,  (eˣ)'=eˣ\n"
        "• (log_a x)'=1/(x·lna),  (lnx)'=1/x\n"
        "• (arcsinx)'=1/√(1−x²),  (arccosx)'=−1/√(1−x²)\n"
        "• (arctgx)'=1/(1+x²),  (arcctgx)'=−1/(1+x²)\n"
        "• Murakkab funksiya: (f(g(x)))'=f'(g(x))·g'(x)"
    ),
    "derivative_app11": (
        "🎯 11-SINF — HOSILANING TATBIQLARI\n\n"
        "• f'(x)>0 → funksiya o'suvchi; f'(x)<0 → kamayuvchi.\n"
        "• Statsionar nuqtalar: f'(x)=0 tenglamaning ildizlari.\n"
        "• x<x₁ da f'(x)>0, x>x₁ da f'(x)<0 bo'lsa — x₁ maksimum nuqtasi.\n"
        "• x<x₁ da f'(x)<0, x>x₁ da f'(x)>0 bo'lsa — x₁ minimum nuqtasi.\n"
        "• Urinma tenglamasi: y=f(x₀)+f'(x₀)(x−x₀); k=f'(x₀)=tgα\n"
        "• Parallellik sharti: f'(x)=k;  perpendikulyarlik sharti: f'(x₀)·g'(x₀)=−1\n"
        "• Eng katta/eng kichik qiymatni kesmada kritik nuqtalar va kesma uchlarida solishtirib toping."
    ),
    "integral11": (
        "∫ 11-SINF — ANIQMAS INTEGRAL (formula kitobi asosida)\n\n"
        "• ∫xᵖdx = xᵖ⁺¹/(p+1)+C (p≠−1)\n"
        "• ∫(1/x)dx = ln|x|+C\n"
        "• ∫sinx dx = −cosx+C,  ∫cosx dx = sinx+C\n"
        "• ∫(1/cos²x)dx = tgx+C,  ∫(1/sin²x)dx = −ctgx+C\n"
        "• ∫aˣdx = aˣ/lna+C,  ∫eˣdx = eˣ+C\n"
        "• ∫(1/√(1−x²))dx = arcsinx+C\n"
        "• ∫(1/(1+x²))dx = arctgx+C\n"
        "• ∫(f±g)dx = ∫f dx ± ∫g dx\n"
        "• Agar F(x) — f(kx+b) ning boshlang'ich funksiyasi bo'lsa, F(kx+b)/k shu ko'rinishdagilar uchun umumlashadi."
    ),
    "integral_area11": (
        "📏 11-SINF — ANIQ INTEGRAL VA YUZA (formula kitobi asosida)\n\n"
        "• Nyuton–Leybnits formulasi: ∫ₐᵇf(x)dx = F(b)−F(a)\n"
        "• Agar f(x)≥0 bo'lsa, OX o'qi va grafik orasidagi yuza: S=∫ₐᵇf(x)dx\n"
        "• Ikki grafik orasidagi yuza: S=∫ₓ₁ˣ²(f(x)−g(x))dx, avval f(x)=g(x) dan kesishish nuqtalari topiladi\n"
        "• Bo'laklab integrallash: ∫f·g'dx = f·g − ∫g·f'dx\n"
        "• Aylanishdan hosil bo'lgan jism hajmi (OX atrofida): V=π∫ₐᵇ[f(x)]²dx"
    ),
    "trig11": (
        "📐 11-SINF — TRIGONOMETRIK AYNIYATLAR\n\n"
        "• sin²x+cos²x=1\n"
        "• 1+tg²x=1/cos²x (cosx≠0),  1+ctg²x=1/sin²x (sinx≠0)\n"
        "• sin(α±β)=sinα cosβ ± cosα sinβ\n"
        "• cos(α±β)=cosα cosβ ∓ sinα sinβ\n"
        "• tg(α±β)=(tgα±tgβ)/(1∓tgα·tgβ)\n"
        "• sin2x=2sinx cosx;  cos2x=cos²x−sin²x=2cos²x−1=1−2sin²x\n"
        "• Yig'indini ko'paytmaga: sinx+siny=2 sin((x+y)/2)cos((x−y)/2)"
    ),
    "trig_eq11": (
        "🔺 11-SINF — TRIGONOMETRIK TENGLAMALAR\n\n"
        "• sinx=a (−1≤a≤1): x=(−1)ⁿ arcsin a + πn, n∈Z\n"
        "• cosx=a (−1≤a≤1): x=±arccos a + 2πn, n∈Z\n"
        "• tgx=a: x=arctg a + πn, n∈Z\n"
        "• ctgx=a: x=arcctg a + πn, n∈Z\n"
        "• Xususiy holatlar: sinx=0 ⇒ x=πn; sinx=1 ⇒ x=π/2+2πn; cosx=0 ⇒ x=π/2+πn\n"
        "• Berilgan intervalda yechim qidirishda umumiy yechimga har xil n qiymatlarini qo'yib tekshiring."
    ),
    "log11": (
        "📊 11-SINF — LOGARIFMIK TENGLAMALAR\n\n"
        "• logₐb=c ⇔ aᶜ=b (a>0, a≠1, b>0)\n"
        "• logₐ(xy)=logₐx+logₐy;  logₐ(x/y)=logₐx−logₐy\n"
        "• logₐ(xᵖ)=p·logₐx;  log_(aᵍ) x=(1/q)·logₐx\n"
        "• Asos almashtirish: logₐb=log_c b / log_c a\n"
        "• Tenglamani yechishdan oldin barcha logarifm argumentlarining musbatligini (OFT) tekshiring."
    ),
    "expo11": (
        "📶 11-SINF — KO'RSATKICHLI TENGLAMALAR\n\n"
        "• aˣ=aʸ ⇔ x=y (a>0, a≠1)\n"
        "• aˣ·aʸ=aˣ⁺ʸ;  aˣ/aʸ=aˣ⁻ʸ;  (aˣ)ʸ=aˣʸ\n"
        "• Asos bir xil bo'lmasa, ikkala tomonni bir xil asosga keltiring yoki t=aˣ almashtirishdan foydalaning\n"
        "• Ko'rsatkichli tengsizlikda: a>1 bo'lsa funksiya o'suvchi, 0<a<1 bo'lsa kamayuvchi (belgi shunga qarab saqlanadi/almashadi)."
    ),
    "sequence11": (
        "🔢 11-SINF — KETMA-KETLIKLAR\n\n"
        "• Arifmetik: aₙ=a₁+(n−1)d;  Sₙ=n(a₁+aₙ)/2\n"
        "• Geometrik: bₙ=b₁qⁿ⁻¹;  Sₙ=b₁(qⁿ−1)/(q−1), q≠1\n"
        "• Cheksiz kamayuvchi geometrik progressiya (|q|<1): S=b₁/(1−q)\n"
        "• Rekurrent ketma-ketlikda berilgan oldingi hadlardan keyingi hadni topish mumkin."
    ),
    "probability11": (
        "🎯 11-SINF — EHTIMOLLIK\n\n"
        "• P(A)=m/n — teng imkoniyatli natijalarda (m — qulay, n — barcha holatlar)\n"
        "• P(A')=1−P(A)\n"
        "• Mustaqil hodisalar: P(A∩B)=P(A)·P(B)\n"
        "• Umumiy qoida: P(A∪B)=P(A)+P(B)−P(A∩B)\n"
        "• Shartli ehtimollik: P(A|B)=P(A∩B)/P(B), P(B)>0."
    ),
    "combinatorics11": (
        "🎲 11-SINF — KOMBINATORIKA\n\n"
        "• n!=1·2·...·n,  0!=1\n"
        "• Pₙ=n! — o'rin almashtirish\n"
        "• Aₙᵏ=n!/(n−k)! — joylashtirish (tartib MUHIM)\n"
        "• Cₙᵏ=n!/[k!(n−k)!] — kombinatsiya (tartib MUHIM EMAS)\n"
        "• Cₙᵏ=Cₙⁿ⁻ᵏ;  Cₙ⁰+Cₙ¹+...+Cₙⁿ=2ⁿ\n"
        "• Murakkab tanlashlarda holatlarni ajratib, qo'shish yoki ko'paytirish qoidasidan foydalaning."
    ),
    "vector11": (
        "➡️ 11-SINF — VEKTORLAR\n\n"
        "• |a|=√(aₓ²+aᵧ²+a_z²)\n"
        "• A(x₁,y₁,z₁), B(x₂,y₂,z₂) uchun: AB=(x₂−x₁; y₂−y₁; z₂−z₁)\n"
        "• a·b=aₓbₓ+aᵧbᵧ+a_zb_z = |a||b|cosα\n"
        "• a·b=0 → nol bo'lmagan vektorlar o'zaro perpendikulyar\n"
        "• Kollinearlik sharti: aₓ/bₓ=aᵧ/bᵧ=a_z/b_z=λ\n"
        "• cosα=(a·b)/(|a|·|b|)"
    ),
    "solid11": (
        "🧊 11-SINF — FAZOVIY GEOMETRIYA (formula kitobi asosida)\n\n"
        "• Kub: V=a³, S_to'la=6a², diagonal d=a√3\n"
        "• To'g'ri prizma: V=S_asos·H, S_yon=P_asos·H\n"
        "• Piramida: V=(1/3)S_asos·H\n"
        "• Muntazam piramida: S_yon=(1/2)P_asos·apofema\n"
        "• Silindr: V=πR²H, S_yon=2πRH, S_to'la=2πR(R+H)\n"
        "• Konus: V=(1/3)πR²H, S_yon=πRl (l — yasovchi), S_to'la=πR(R+l)\n"
        "• Kesik konus: V=(1/3)πH(R²+Rr+r²)\n"
        "• Shar: V=(4/3)πR³, S=4πR²\n"
        "• Shar sektori: V=(2π/3)R²H"
    ),
    "analytic11": (
        "📍 11-SINF — ANALITIK GEOMETRIYA\n\n"
        "• Ikki nuqta orasidagi masofa: d=√((x₂−x₁)²+(y₂−y₁)²)\n"
        "• Kesma o'rtasining koordinatasi: M((x₁+x₂)/2, (y₁+y₂)/2)\n"
        "• To'g'ri chiziq tenglamasi: y−y₀=k(x−x₀), k=tgα\n"
        "• Ikki nuqtadan o'tuvchi to'g'ri chiziq: (y−y₁)/(y₂−y₁)=(x−x₁)/(x₂−x₁)\n"
        "• Parallellik sharti: k₁=k₂;  Perpendikulyarlik sharti: k₁·k₂=−1\n"
        "• Markazi C(a,b), radiusi R bo'lgan aylana: (x−a)²+(y−b)²=R²\n"
        "• Nuqtadan to'g'ri chiziqqacha masofa: d=|Ax₀+By₀+C|/√(A²+B²)"
    ),
    "complex11": (
        "🔷 11-SINF — KOMPLEKS SONLAR\n\n"
        "• i²=−1\n"
        "• z=a+bi (a — haqiqiy, b — mavhum qism)\n"
        "• (a+bi)+(c+di)=(a+c)+(b+d)i\n"
        "• (a+bi)(c+di)=(ac−bd)+(ad+bc)i\n"
        "• |z|=√(a²+b²)\n"
        "• Qo'shma son: z̄=a−bi;  z·z̄=a²+b²=|z|²"
    ),
    "optimization11": (
        "⚙️ 11-SINF — OPTIMALLASHTIRISH\n\n"
        "• Avval cheklovdan foydalanib maqsad funksiyasini bitta o'zgaruvchiga keltiring.\n"
        "• Kritik nuqtalarni f'(x)=0 tenglamasidan toping.\n"
        "• Kritik nuqtalar va ruxsat etilgan chegara nuqtalaridagi qiymatlarni solishtiring — eng katta/kichigi javob."
    ),
    "parameter11": (
        "🧩 11-SINF — PARAMETRLI MASALALAR\n\n"
        "• Parametrning qaysi qiymatlarida tenglama/tengsizlik ma'noga ega ekanini aniqlang.\n"
        "• Kvadrat tenglamada D ning ishorasi ildizlar sonini belgilaydi (D>0, D=0, D<0).\n"
        "• Barcha shartlar kesishmasini oling — har bir topilgan qiymat hamma shartni qanoatlantirishi kerak.\n"
        "• Oxirida topilgan parametr qiymatini asl tenglamada tekshiring."
    ),
    "proof11": (
        "📝 11-SINF — ISBOT VA MULOHAZA\n\n"
        "• To'g'ridan-to'g'ri isbot: shartdan boshlab xulosaga boring.\n"
        "• Qarama-qarshilik usuli: xulosaning inkorini faraz qilib, zidlikka keling.\n"
        "• Matematik induksiya: n=1 uchun tekshirib, n=k dan n=k+1 ga o'tishni isbotlang.\n"
        "• Bitta misol umumiy hukmni isbotlamaydi; bitta qarshi misol esa hukmni rad etadi."
    ),
})

HINTS.update({
    "logic11": "Shartlarni jadval yoki holatlar bo'yicha ajrating; barcha shart bir vaqtda bajariladigan holatni qidiring.",
    "function11": "Avval aniqlanish sohasi, keyin juft/toqlik va monotonlik kabi xossalarni tekshiring.",
    "derivative11": "Avval ifodani sodda ko'rinishga keltiring, so'ng mos hosila qoidasini tanlang.",
    "derivative_app11": "Kritik nuqtalarni topib, ishora jadvali yoki qiymatlar orqali ularni tasniflang.",
    "integral11": "Boshlang'ich funksiyani toping va oxirida hosila olib tekshiring.",
    "integral_area11": "Avval grafiklarning kesishish nuqtalarini toping, keyin yuqori-pastki funksiyani aniqlang.",
    "trig11": "Kerakli ayniyatni tanlab, chap va o'ng tomonni bir xil ko'rinishga keltiring.",
    "trig_eq11": "Asosiy burchakni toping, umumiy yechimni yozing va berilgan intervalni alohida tekshiring.",
    "log11": "Eng birinchi qadam — logarifm argumentlarining musbatligini yozish.",
    "expo11": "Iloji bo'lsa asoslarni bir xil qiling; bo'lmasa monotonlik yoki almashtirishni o'ylang.",
    "sequence11": "Ketma-ketlikning turini aniqlang: ayirma doimiymi yoki nisbat doimiymi.",
    "probability11": "Avval barcha ehtimolli holatlarni aniqlang; qarama-qarshi hodisa ba'zan ancha qisqa yo'l beradi.",
    "combinatorics11": "Tartib muhim yoki muhim emasligini aniqlamasdan formula tanlamang.",
    "vector11": "Perpendikulyarlik uchun skalyar ko'paytmani nolga tenglashtirish qulay.",
    "solid11": "Hajm formulasi uchun asos yuzasi va balandlikning qaysi biri berilganini ajrating.",
    "analytic11": "Koordinatalarni formulaga qo'yishdan oldin qaysi geometrik xossa so'ralganini aniqlang.",
    "complex11": "i²=−1 ni har bir ko'paytirishda qo'llang va oxirida haqiqiy/mavhum qismlarni ajrating.",
    "optimization11": "Cheklovdan foydalanib maqsad funksiyasini bitta o'zgaruvchiga keltiring.",
    "parameter11": "Parametrni oddiy noma'lumdek emas, shartlarni o'zgartiruvchi son sifatida tahlil qiling.",
    "proof11": "Hukmning aynan nimani talab qilayotganini yozing; bitta misol umumiy isbot emas.",
})

# ---------- 11-sinf generatorlari ----------
# Har bir mavzuda kamida ikki xil yechish g'oyasi bor. Bu bir xil
# sonlarni almashtirib qo'yish emas, balki savolning matematik tuzilishini
# ham o'zgartiradi.

def f11_logic(grade):
    kind = random.randrange(3)
    if kind == 0:
        a = random.randint(2, 6)
        b = random.randint(1, 5)
        # x+y va x-y orqali noma'lumni aniqlash
        return f"Ikki sonning yig'indisi {a+b}, ayirmasi {a-b} ga teng. Katta sonni toping.", a
    if kind == 1:
        n = random.randint(3, 8)
        # n ta nuqtadan nechta kesma: C(n,2)
        return f"Bir to'g'ri chiziqda {n} ta turli nuqta belgilangan. Shu nuqtalardan uchlarini oladigan nechta turli kesma hosil bo'ladi?", math.comb(n, 2)
    # Qarama-qarshi fikrlash
    n = random.randint(2, 7)
    return f"“{n}²+n soni toq” degan da'vo uchun {n} sonida hosil bo'lgan qiymatning juft/toqligini aniqlang: 0=juft, 1=toq.", (n*n+n) % 2

def f11_logic_v2(grade):
    a = random.randint(2, 7)
    b = random.randint(2, 5)
    product = a*a*b
    return f"Katta son kichik sonning {b} marta kattasi. Ularning ko'paytmasi {product} ga teng. Kichik sonni toping.", a

def f11_function(grade):
    a = random.randint(1, 4)
    b = random.randint(1, 5)
    x = random.randint(-2, 3)
    inner = a*x + b
    ans = inner*inner - 1
    return f"f(x)=x²−1, g(x)={a}x+{b}. (f∘g)({x}) ni toping.", ans

def f11_function_v2(grade):
    a = random.randint(2, 5)
    b = random.randint(-4, 2)
    target = random.randint(3, 12)
    # f(x)=ax+b, f(x)=target -> x butun bo'lishini kafolatlaymiz
    target = a*random.randint(1, 6)+b
    x = (target-b)//a
    return f"f(x)={a}x{b:+d}. f(x)={target} bo'lsa, x ni toping.", x

def f11_derivative(grade):
    a = random.randint(2, 5)
    b = random.randint(1, 6)
    x = random.randint(1, 4)
    return f"f(x)={a}x²+{b}x. f'({x}) qiymatini toping.", 2*a*x+b

def f11_derivative_v2(grade):
    a = random.randint(1, 4)
    b = random.randint(1, 5)
    x = random.randint(1, 4)
    return f"f(x)=(x+{a})(x+{b}). f'({x}) ni toping.", 2*x+a+b

def f11_derivative_app(grade):
    a,b = random.choice([(1,-2),(1,-4),(2,-4),(2,-6),(3,-6),(3,-4),(4,-4)])
    x0 = -b//(2*a)
    return f"f(x)={a}x²{b:+d}x+1. Funksiyaning ekstremum nuqtasining x koordinatasi nechaga teng?", x0

def f11_derivative_app_v2(grade):
    a = random.randint(1,4)
    x0 = random.randint(1,4)
    b = -2*a*x0
    c = random.randint(0,4)
    return f"f(x)={a}x²{b:+d}x+{c}. f'(x)=0 tenglamaning yechimini toping.", x0

def f11_integral(grade):
    a = random.randint(2,5); b = random.randint(1,5)
    return f"2·∫₀¹({a}x+{b})dx ni hisoblang.", a+2*b

def f11_integral_v2(grade):
    a = random.randint(1,4); b = random.randint(1,4)
    # integral_0^2 (ax+b) dx = 2a+2b
    return f"∫₀²({a}x+{b})dx ni hisoblang.", 2*a+2*b

def f11_integral_area(grade):
    k = random.randint(2,6)
    return f"y={k}−x grafigi va x o'qi bilan x=0 dan x={k} gacha chegaralangan yuzaning 2 baravarini toping.", k*k

def f11_integral_area_v2(grade):
    k = random.randint(2,5)
    # 0..k oralig'ida y=k va y=x orasidagi yuza = k^2/2; 2S=k^2
    return f"0≤x≤{k} da y={k} va y=x grafiklari orasidagi yuzaning 2 baravarini toping.", k*k

def f11_trig(grade):
    c = random.choice([0, 1/2, math.sqrt(2)/2, math.sqrt(3)/2])
    if c == 0: return "Agar cos x=0 bo'lsa, sin²x ning qiymati nechaga teng?", 1
    if c == 1/2: return "Agar cos x=1/2 bo'lsa, 4·sin²x ning qiymatini toping.", 3
    if abs(c-math.sqrt(2)/2)<1e-9: return "Agar cos x=√2/2 bo'lsa, 2·sin²x ning qiymatini toping.", 1
    return "Agar cos x=√3/2 bo'lsa, 4·sin²x ning qiymatini toping.", 1

def f11_trig_v2(grade):
    # cos(2x)=1-2sin²x dan foydalanish
    s = random.choice([0, 1/2, math.sqrt(2)/2])
    if s == 0: return "sin x=0 bo'lsa, cos(2x) ni toping.", 1
    if s == 1/2: return "sin x=1/2 bo'lsa, 2cos(2x) ning qiymatini toping.", 1
    return "sin x=√2/2 bo'lsa, cos(2x) ning qiymatini toping. Javobni 0, 1 yoki −1 ko'rinishida yozing.", -1

def f11_trig_eq(grade):
    return "0≤x<2π da 2sin x·cos x=1 tenglamaning nechta yechimi bor?", 2

def f11_trig_eq_v2(grade):
    return "0≤x<2π da sin x=0 tenglamaning nechta yechimi bor?", 2

def f11_log(grade):
    a = random.choice([2,3,5]); k = random.randint(1,4); n=a**k
    return f"log_{a}({n}) + log_{a}({a}) ning qiymatini toping.", k+1

def f11_log_v2(grade):
    a = random.choice([2,3,5]); p = random.randint(2,5); q = random.randint(1,p-1)
    return f"log_{a}({a**p}) − log_{a}({a**q}) ni hisoblang.", p-q

def f11_expo(grade):
    a=random.choice([2,3]); k=random.randint(1,4)
    return f"{a}^(x+1)={a**k}. x ni toping.", k-1

def f11_expo_v2(grade):
    a=random.choice([2,3,5]); x=random.randint(1,4); b=random.randint(1,3)
    rhs=a**x
    return f"{a}^(2x−{b})={a**(2*x-b)} bo'lsa, x ni toping.", x

def f11_sequence(grade):
    a1=random.randint(1,5); d=random.randint(2,5); n=random.randint(4,7); an=a1+(n-1)*d
    return f"Arifmetik progressiyada a₁={a1}, d={d}. a_{n} ning qiymatini toping.", an

def f11_sequence_v2(grade):
    a1=random.randint(1,5); q=random.choice([2,3]); n=random.randint(3,6)
    return f"Geometrik progressiyada b₁={a1}, q={q}. b_{n} ni toping.", a1*q**(n-1)

def f11_probability(grade):
    red=random.randint(2,4); blue=random.randint(1,3)
    return f"Qutida {red} ta qizil va {blue} ta ko'k shar bor. Ketma-ket ikki shar olinganda biri qizil, biri ko'k bo'lishining nechta tartibli qulay holati bor?", 2*red*blue

def f11_probability_v2(grade):
    # Ikki kubik yig'indisi 7 bo'lishining tartibli qulay juftliklari soni
    return "Ikki oddiy kubik tashlandi. Ularning yig'indisi 7 bo'lishini ta'minlaydigan nechta tartibli natija mavjud?", 6

def f11_combinatorics(grade):
    n=random.randint(5,8); k=random.choice([2,3])
    return f"{n} ta turli kitobdan {k} tasini tartibsiz tanlashning nechta usuli bor?", math.comb(n,k)

def f11_combinatorics_v2(grade):
    n=random.randint(5,8)
    return f"{n} kishilik guruhdan rais va kotibni (lavozimlar turli) nechta usulda tanlash mumkin?", n*(n-1)

def f11_vector(grade):
    ax,ay=random.randint(1,4),random.randint(1,4); bx,by=-ax,random.randint(-4,4)
    return f"a=({ax},{ay}), b=({bx},{by}). a·b ni toping.", ax*bx+ay*by

def f11_vector_v2(grade):
    x=random.randint(2,5); y=random.randint(2,5)
    return f"a=({x},{y}) vektorining uzunligi kvadratini toping.", x*x+y*y

def f11_solid(grade):
    a=random.randint(2,5)
    return f"Qirrasi {a} bo'lgan kubning fazoviy diagonalining kvadrati nechaga teng?", 3*a*a

def f11_solid_v2(grade):
    r=random.randint(2,5); h=random.randint(2,5)
    return f"Radiusi {r}, balandligi {h} bo'lgan silindr hajmining π ga bo'lingan qiymatini toping.", r*r*h

def f11_analytic(grade):
    x1,y1=random.randint(-2,2),random.randint(-2,2); dx,dy=random.choice([(3,4),(4,3),(1,2),(2,1)])
    x2,y2=x1+dx,y1+dy
    return f"A({x1},{y1}) va B({x2},{y2}) nuqtalar berilgan. AB kesma uzunligining kvadratini toping.", dx*dx+dy*dy

def f11_analytic_v2(grade):
    x1=random.randint(-4,2); y1=random.randint(-4,2); dx=random.choice([2,4]); dy=random.randint(2,5)
    x2,y2=x1+dx,y1+dy
    return f"A({x1},{y1}) va B({x2},{y2}) kesmasining o'rta nuqtasi x koordinatasini toping.", (x1+x2)//2

def f11_complex(grade):
    a,b=random.randint(1,4),random.randint(1,4); c,d=random.randint(1,4),random.randint(1,4)
    return f"z={a}+{b}i va w={c}+{d}i. zw ko'paytmaning haqiqiy qismini toping.", a*c-b*d

def f11_complex_v2(grade):
    a,b=random.randint(1,4),random.randint(1,4)
    return f"z={a}+{b}i. |z|² ni toping.", a*a+b*b

def f11_optimization(grade):
    p=2*random.randint(3,6)
    return f"To'g'ri to'rtburchakning yarim perimetri {p} ga teng. Eng katta mumkin bo'lgan yuza nechaga teng?", (p//2)**2

def f11_optimization_v2(grade):
    p=random.randint(3,7)
    # x(2p-x) maksimum x=p, yuza p²
    return f"x va y musbat sonlar bo'lib, x+y={2*p}. xy ko'paytmaning eng katta qiymatini toping.", p*p

def f11_parameter(grade):
    return "x²−2ax+1=0 tenglama aynan bitta haqiqiy ildizga ega bo'lishi uchun nechta haqiqiy a qiymati mavjud?", 2

def f11_parameter_v2(grade):
    # D=0 sharti: x²+ax+1=0 -> a²-4=0 -> 2 ta qiymat
    return "x²+ax+1=0 tenglama bitta haqiqiy ildizga ega bo'lishi uchun nechta haqiqiy a qiymati mavjud?", 2

def f11_proof(grade):
    return "“n²+n har qanday natural n uchun toq” da'voni rad etuvchi eng kichik natural n ni toping.", 1

def f11_proof_v2(grade):
    # Qarshi misol emas, identiklikni tekshirish
    n=random.randint(2,6)
    return f"n={n} uchun n(n+1) juft ekanini tasdiqlang: n(n+1)/2 ning qiymatini toping.", n*(n+1)//2

GEN_F11 = {
    "logic11": [(f11_logic,{"eleventh"}),(f11_logic_v2,{"eleventh"})],
    "function11": [(f11_function,{"eleventh"}),(f11_function_v2,{"eleventh"})],
    "derivative11": [(f11_derivative,{"eleventh"}),(f11_derivative_v2,{"eleventh"})],
    "derivative_app11": [(f11_derivative_app,{"eleventh"}),(f11_derivative_app_v2,{"eleventh"})],
    "integral11": [(f11_integral,{"eleventh"}),(f11_integral_v2,{"eleventh"})],
    "integral_area11": [(f11_integral_area,{"eleventh"}),(f11_integral_area_v2,{"eleventh"})],
    "trig11": [(f11_trig,{"eleventh"}),(f11_trig_v2,{"eleventh"})],
    "trig_eq11": [(f11_trig_eq,{"eleventh"}),(f11_trig_eq_v2,{"eleventh"})],
    "log11": [(f11_log,{"eleventh"}),(f11_log_v2,{"eleventh"})],
    "expo11": [(f11_expo,{"eleventh"}),(f11_expo_v2,{"eleventh"})],
    "sequence11": [(f11_sequence,{"eleventh"}),(f11_sequence_v2,{"eleventh"})],
    "probability11": [(f11_probability,{"eleventh"}),(f11_probability_v2,{"eleventh"})],
    "combinatorics11": [(f11_combinatorics,{"eleventh"}),(f11_combinatorics_v2,{"eleventh"})],
    "vector11": [(f11_vector,{"eleventh"}),(f11_vector_v2,{"eleventh"})],
    "solid11": [(f11_solid,{"eleventh"}),(f11_solid_v2,{"eleventh"})],
    "analytic11": [(f11_analytic,{"eleventh"}),(f11_analytic_v2,{"eleventh"})],
    "complex11": [(f11_complex,{"eleventh"}),(f11_complex_v2,{"eleventh"})],
    "optimization11": [(f11_optimization,{"eleventh"}),(f11_optimization_v2,{"eleventh"})],
    "parameter11": [(f11_parameter,{"eleventh"}),(f11_parameter_v2,{"eleventh"})],
    "proof11": [(f11_proof,{"eleventh"}),(f11_proof_v2,{"eleventh"})],
}
TOPIC_GENERATORS.update(GEN_F11)

# 11-sinf savollari uchun manfiy javoblar ham mumkin.
NEGATIVE_ALLOWED_TOPICS.update({
    "function11", "derivative11", "derivative_app11", "trig_eq11",
    "log11", "expo11", "sequence11", "vector11", "analytic11",
    "complex11", "parameter11",
})

# Quyidagi mavzularda ham to'g'ri javob tabiiy ravishda manfiy son bo'lishi
# mumkin (koordinata, harorat, funksiya qiymati, tenglama ildizi va h.k.).
# Bularni ro'yxatga qo'shmasak, variant javoblar orasida FAQAT to'g'ri javob
# manfiy bo'lib qolib, o'quvchi hisoblamasdan ham "manfiysini tanla" deb
# to'g'ri javobni topib olishi mumkin edi - bu xato shu yerda tuzatiladi.
NEGATIVE_ALLOWED_TOPICS.update({
    "algebra_value7", "coordinate7", "coordinates", "function10",
    "function7", "function8", "function9", "ineq9", "integer7",
    "polynomial8", "quad10", "quad_ineq10", "system9", "trig11",
    "algebra9",
})


# Eski 11-sinfga xos mavzular menyudan chiqariladi; yangi 10-sinf mavzulari yuqorida alohida qo‘shilgan.
for _obsolete_topic in ("log", "expo_eq", "combinatorics"):
    TOPICS.pop(_obsolete_topic, None)
    FORMULAS.pop(_obsolete_topic, None)
    HINTS.pop(_obsolete_topic, None)
    TOPIC_GENERATORS.pop(_obsolete_topic, None)


# Keep the original formula topics available; 10-sinf has separate log10/expo10/combinatorics10 topics.

# Generatorlar xotirasi: bir mavzuda bir xil yechish qolipi ketma-ket takrorlanmasin.
_RECENT_GENERATORS = {}
_RECENT_SIGNATURES = {}


def _question_signature(text):
    """Savolning sonlari emas, matematik qolipi takrorlanganini aniqlashga yordam beradi."""
    import re
    s = text.lower()
    s = re.sub(r"-?\d+(?:[.,]\d+)?", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def generate_example(user_id, topic, grade="medium"):
    """Daraja va mavzuga mos, yaqinda takrorlanmagan va qolipi almashib turadigan misol yaratadi."""
    all_generators = TOPIC_GENERATORS[topic]
    generators = [func for func, tiers in all_generators if grade in tiers]
    if not generators:
        generators = [func for func, _ in all_generators]

    history = get_topic_history(user_id, topic)
    recent_funcs = _RECENT_GENERATORS.setdefault((user_id, topic), [])
    recent_sigs = _RECENT_SIGNATURES.setdefault((user_id, topic), [])

    candidates = [g for g in generators if g.__name__ not in recent_funcs[-2:]] or generators
    for _ in range(80):
        gen_func = random.choice(candidates)
        candidate_text, candidate_answer = gen_func(grade)
        sig = _question_signature(candidate_text)
        if candidate_text in history:
            continue
        # Agar mavzuda turli qoliplar mavjud bo'lsa, aynan bir qolipni ketma-ket bermaymiz.
        if sig in recent_sigs[-2:] and len(set(_question_signature(x) for x in history)) > 2:
            continue
        if not isinstance(candidate_answer, (int, float)):
            continue
        if isinstance(candidate_answer, float) and not math.isfinite(candidate_answer):
            continue
        recent_funcs.append(gen_func.__name__)
        recent_sigs.append(sig)
        del recent_funcs[:-8]
        del recent_sigs[:-8]
        save_to_history(user_id, topic, candidate_text)
        return candidate_text, candidate_answer

    # Juda tor mavzularda ham bot to'xtab qolmasligi uchun tarixdan tashqari
    # eng yangi tasodifiy savolni qaytaramiz.
    gen_func = random.choice(generators)
    candidate_text, candidate_answer = gen_func(grade)
    recent_funcs.append(gen_func.__name__)
    recent_sigs.append(_question_signature(candidate_text))
    del recent_funcs[:-8]
    del recent_sigs[:-8]
    save_to_history(user_id, topic, candidate_text)
    return candidate_text, candidate_answer

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


def get_unique_formula_topics():
    """
    Bir xil mavzuning formulasi bir necha xil sinf-kaliti ostida (masalan
    quad8/quad9/quad10) saqlanishi mumkin - amaliyot uchun bu kerak (har
    darajada alohida savol generatsiya qilinadi), lekin FORMULA MENYUSIDA
    bir xil mavzu bir necha marta takrorlanib ko'rinishi kerak emas.
    Bu funksiya FORMULAS matni bo'yicha guruhlab, har bir NOYOB formula
    matni uchun faqat BITTA vakil mavzu (imkon qadar raqamli sinf-suffiksisiz,
    "kanonik" nomli kalit) qaytaradi.
    """
    groups = {}
    for key in TOPICS:
        text = FORMULAS.get(key, "")
        groups.setdefault(text, []).append(key)

    def has_trailing_digit(k):
        return bool(re.search(r'\d+$', k))

    unique_topics = []
    for text, keys in groups.items():
        no_digit = [k for k in keys if not has_trailing_digit(k)]
        pool = no_digit if no_digit else keys
        rep = sorted(pool)[0]
        unique_topics.append(rep)

    # Asl TOPICS tartibini saqlab qolish uchun saralaymiz
    order = {k: i for i, k in enumerate(TOPICS)}
    unique_topics.sort(key=lambda k: order[k])
    return unique_topics


def get_formula_keyboard():
    builder = InlineKeyboardBuilder()
    for key in get_unique_formula_topics():
        builder.button(text=TOPICS[key], callback_data=f"formula_{key}")
    builder.adjust(2)
    return builder.as_markup()


GEOMETRY_SHAPE_TOPICS = {"triangle", "rectangle", "circle", "triangle7", "geometry7", "geometry9"}


def _draw_geometry_shape(topic):
    """Berilgan mavzu uchun oddiy, umumiy (raqamsiz) chizma yaratadi."""
    fig, ax = plt.subplots(figsize=(3.2, 3.2))
    ax.set_xlim(-1.5, 1.5)
    ax.set_ylim(-1.5, 1.5)
    ax.set_aspect("equal")
    ax.axis("off")

    if topic in ("triangle", "triangle7"):
        pts = [(-1, -0.8), (1, -0.8), (0, 1)]
        tri = plt.Polygon(pts, fill=False, linewidth=2)
        ax.add_patch(tri)
        ax.text(0, -1.1, "a", ha="center")
        ax.text(0.15, 0.1, "h", ha="left")
        ax.plot([0, 0], [-0.8, 1], linestyle="--", linewidth=1)
    elif topic == "circle":
        circ = plt.Circle((0, 0), 1, fill=False, linewidth=2)
        ax.add_patch(circ)
        ax.plot([0, 1], [0, 0], linewidth=1)
        ax.text(0.5, 0.08, "r", ha="center")
    else:  # rectangle / geometry7 / geometry9
        rect = plt.Rectangle((-1, -0.6), 2, 1.2, fill=False, linewidth=2)
        ax.add_patch(rect)
        ax.text(0, -0.85, "a", ha="center")
        ax.text(-1.25, 0, "b", va="center")

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def send_geometry_image_if_relevant(chat_id, topic):
    """Geometriya mavzulari uchun tushuntiruvchi chizma yuboradi.
    Boshqa mavzularda hech narsa qilmaydi va hech qachon asosiy oqimni buzmaydi."""
    if topic not in GEOMETRY_SHAPE_TOPICS:
        return
    try:
        image_bytes = _draw_geometry_shape(topic)
        photo = BufferedInputFile(image_bytes, filename="shape.png")
        await bot.send_photo(chat_id, photo)
    except Exception:
        pass


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

    is_correct = (chosen == correct_answer)
    log_daily_answer(user_id, is_correct)

    if is_correct:
        update_user(user_id, correct=user["correct"] + 1)
        update_topic_stat(user_id, topic, correct_delta=1)
        add_weekly_correct(user_id)
        new_streak = update_streak(user_id)
        result_text = f"{random.choice(MOTIVATIONS)} 🔥 Streak: {new_streak} kun"

        today_correct = get_today_correct(user_id)
        if today_correct == DAILY_GOAL:
            result_text += f"\n\n🎯 Bugungi {DAILY_GOAL} ta to‘g‘ri javob maqsadiga yetdingiz!"
    else:
        update_user(user_id, wrong=user["wrong"] + 1)
        update_topic_stat(user_id, topic, wrong_delta=1)
        old_question = user["current_question"] or "?"
        log_mistake(user_id, topic, old_question, correct_answer)
        # Xato qilingan mavzuni keyinroq takrorlashga qo‘yamiz.
        schedule_review(user_id, topic, stage=0)
        solution_hint = HINTS.get(topic, "")
        result_text = f"❌ Noto‘g‘ri. To‘g‘ri javob: {correct_answer}\n📖 Yechim uchun yo‘l: {solution_hint}"

    mode = user["current_mode"]

    # === Takrorlash rejimi ===
    if mode == "review":
        stage = get_review_stage(user_id, topic)
        if is_correct:
            new_stage = stage + 1
            if new_stage > 2:
                remove_review(user_id, topic)
                result_text += f"\n\n✅ {TOPICS[topic]} mavzusi mustahkamlandi!"
            else:
                schedule_review(user_id, topic, stage=new_stage)
        else:
            schedule_review(user_id, topic, stage=0)

        due = [t for t, s in get_due_reviews(user_id) if t != topic]
        if due:
            next_topic = due[0]
            example_text, answer = generate_example(user_id, next_topic, user["grade"])
            options = generate_options(answer, allow_negative=topic_allows_negative(next_topic))
            update_user(user_id, current_topic=next_topic, current_answer=answer,
                        current_mode="review", current_question=example_text)
            await callback.message.edit_text(
                f"{result_text}\n\n🔄 Takrorlash ({TOPICS[next_topic]}): {example_text}\n\n"
                f"To‘g‘ri javobni tanlang 👇",
                reply_markup=get_answer_keyboard(next_topic, options)
            )
            await send_geometry_image_if_relevant(callback.message.chat.id, next_topic)
        else:
            update_user(user_id, current_mode="normal", current_topic=None, current_answer=None)
            await callback.message.edit_text(
                f"{result_text}\n\n🔄 Bugungi barcha takrorlashlar tugadi!"
            )
        await callback.answer()
        return

    # === Oddiy / random / challenge rejim ===
    if mode == "random":
        next_topic = random.choice(topics_for_grade(user["grade"]))
    elif mode == "challenge":
        next_topic = random.choice(GRADE_TOPICS["hard"])
    else:
        next_topic = topic

    effective_grade = "hard" if mode == "challenge" else user["grade"]
    example_text, answer = generate_example(user_id, next_topic, effective_grade)
    options = generate_options(answer, allow_negative=topic_allows_negative(next_topic))
    update_user(user_id, current_topic=next_topic, current_answer=answer, current_question=example_text)

    topic_label = f" ({TOPICS[next_topic]})" if mode in ("random", "challenge") else ""
    await callback.message.edit_text(
        f"{result_text}\n\n📝 Keyingi misol{topic_label}: {example_text}\n\n"
        f"To‘g‘ri javobni tanlang 👇",
        reply_markup=get_answer_keyboard(next_topic, options)
    )
    await send_geometry_image_if_relevant(callback.message.chat.id, next_topic)
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

    topic = random.choice(GRADE_TOPICS["hard"])
    example_text, answer = generate_example(user_id, topic, "hard")
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))
    update_user(user_id, current_topic=topic, current_answer=answer, current_question=example_text)

    await message.answer(
        f"⭐ Challenge rejimi! 9-sinf darajasidagi murakkab savollar.\n\n"
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



@dp.message(Command("goal"))
async def goal_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)
    today_correct = get_today_correct(user_id)
    filled = min(10, round(today_correct / DAILY_GOAL * 10))
    bar = "🟩" * filled + "⬜" * (10 - filled)
    if today_correct >= DAILY_GOAL:
        text = f"🎯 Kunlik maqsad: {today_correct}/{DAILY_GOAL}\n{bar}\n\n✅ Bugungi maqsadga yetdingiz!"
    else:
        left = DAILY_GOAL - today_correct
        text = f"🎯 Kunlik maqsad: {today_correct}/{DAILY_GOAL}\n{bar}\n\nYana {left} ta to‘g‘ri javob kerak."
    await message.answer(text)


@dp.message(Command("progress"))
async def progress_handler(message: types.Message):
    user_id = message.from_user.id
    get_user(user_id, message.from_user.first_name)
    try:
        image_bytes = draw_progress_image(user_id)
        photo = BufferedInputFile(image_bytes, filename="progress.png")
        await message.answer_photo(photo, caption="📉 Oxirgi 7 kunlik natijangiz")
    except Exception:
        await message.answer("📉 Grafikni yaratishda vaqtinchalik xatolik yuz berdi.")


@dp.message(Command("review"))
async def review_handler(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name)
    due = get_due_reviews(user_id)
    if not due:
        await message.answer("🔄 Hozircha takrorlash kerak bo‘lgan mavzu yo‘q. Xato ishlangan savollar keyin shu yerga tushadi.")
        return

    topic = due[0][0]
    example_text, answer = generate_example(user_id, topic, user["grade"])
    options = generate_options(answer, allow_negative=topic_allows_negative(topic))
    update_user(user_id, current_topic=topic, current_answer=answer,
                current_mode="review", current_question=example_text)

    await message.answer(
        f"🔄 Takrorlash rejimi\n\nMavzu: {TOPICS[topic]}\n📝 {example_text}\n\n"
        f"To‘g‘ri javobni tanlang 👇",
        reply_markup=get_answer_keyboard(topic, options)
    )
    await send_geometry_image_if_relevant(message.chat.id, topic)


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
        types.BotCommand(command="goal", description="🎯 Kunlik maqsad"),
        types.BotCommand(command="progress", description="📉 Progress grafigi"),
        types.BotCommand(command="review", description="🔄 Takrorlash"),
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
