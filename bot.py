import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "expenses.db")


# ---------- DATABASE ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            photo_file_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def add_expense(user_id, amount, description, entry_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO expenses (user_id, amount, description, entry_date, photo_file_id) VALUES (?, ?, ?, ?, NULL)",
        (user_id, amount, description, entry_date.isoformat()),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def attach_photo(user_id, file_id):
    """Attach a photo to the user's most recent expense that has no photo yet."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM expenses WHERE user_id=? AND photo_file_id IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = c.fetchone()
    if row is None:
        conn.close()
        return None
    expense_id = row[0]
    c.execute("UPDATE expenses SET photo_file_id=? WHERE id=?", (file_id, expense_id))
    conn.commit()
    conn.close()
    return expense_id


def get_total(user_id, since=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if since:
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND entry_date>=?",
            (user_id, since.isoformat()),
        )
    else:
        c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=?", (user_id,))
    total = c.fetchone()[0]
    conn.close()
    return total


def get_recent(user_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT amount, description, entry_date, photo_file_id FROM expenses WHERE user_id=? ORDER BY entry_date DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def export_rows(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT entry_date, amount, description FROM expenses WHERE user_id=? ORDER BY entry_date",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ---------- PARSING (mirrors the tracker artifact's logic) ----------

def extract_date(raw_text: str):
    now = datetime.now()
    date = now
    matched = None

    m = re.search(r"(\d+)\s*дн(?:я|ей|ь)?\s*назад", raw_text, re.IGNORECASE)
    if m:
        date = now - timedelta(days=int(m.group(1)))
        matched = m.group(0)
    elif re.search(r"позавчера", raw_text, re.IGNORECASE):
        date = now - timedelta(days=2)
        matched = re.search(r"позавчера", raw_text, re.IGNORECASE).group(0)
    elif re.search(r"вчера", raw_text, re.IGNORECASE):
        date = now - timedelta(days=1)
        matched = re.search(r"вчера", raw_text, re.IGNORECASE).group(0)
    elif re.search(r"сегодня", raw_text, re.IGNORECASE):
        matched = re.search(r"сегодня", raw_text, re.IGNORECASE).group(0)
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", raw_text)
        if m:
            day = int(m.group(1))
            month = int(m.group(2))
            year_str = m.group(3)
            if year_str:
                year = 2000 + int(year_str) if len(year_str) == 2 else int(year_str)
            else:
                year = now.year
            if 1 <= day <= 31 and 1 <= month <= 12:
                try:
                    date = now.replace(year=year, month=month, day=day)
                    matched = m.group(0)
                except ValueError:
                    pass

    cleaned = raw_text.replace(matched, "").strip() if matched else raw_text
    return date, cleaned


def parse_entry(raw_text: str):
    date, cleaned = extract_date(raw_text.strip())
    m = re.search(r"-?\d+(?:[.,]\d+)?", cleaned)
    amount = 0.0
    desc = cleaned
    if m:
        amount = float(m.group(0).replace(",", "."))
        desc = (cleaned[: m.start()] + cleaned[m.end():]).strip()
        desc = re.sub(r"^[\s\-–—:.,]+|[\s\-–—:.,]+$", "", desc)
    if not desc:
        desc = "без описания"
    return amount, desc, date


def fmt_eur(n: float) -> str:
    return f"{n:,.2f}".replace(",", " ").replace(".", ",")


# ---------- HANDLERS ----------

WELCOME = (
    "Привет! Я записываю твои расходы.\n\n"
    "Просто пиши (или диктуй через микрофон клавиатуры) сумму и что купил:\n"
    "  300 бензин\n"
    "  вчера 50 запчасти\n"
    "  20.07 120 инструмент\n\n"
    "После записи можешь прислать фото чека — оно прикрепится к последней записи.\n\n"
    "Команды:\n"
    "/total — сумма всего\n"
    "/today — сумма за сегодня\n"
    "/recent — последние записи\n"
    "/export — выгрузить всё в CSV"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    amount, desc, date = parse_entry(text)
    add_expense(user_id, amount, desc, date)
    total = get_total(user_id)
    await update.message.reply_text(
        f"Записал: {fmt_eur(amount)} € — {desc} ({date.strftime('%d.%m.%Y')})\n"
        f"Итого: {fmt_eur(total)} €"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]  # highest resolution
    expense_id = attach_photo(user_id, photo.file_id)
    if expense_id:
        await update.message.reply_text("Фото чека прикреплено к последней записи.")
    else:
        await update.message.reply_text(
            "Не нашёл запись без фото — сначала пришли текстом сумму и описание, потом фото."
        )


async def total_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = get_total(user_id)
    await update.message.reply_text(f"Итого: {fmt_eur(total)} €")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = get_total(user_id, since=start_of_day)
    await update.message.reply_text(f"Сегодня: {fmt_eur(total)} €")


async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_recent(user_id)
    if not rows:
        await update.message.reply_text("Пока нет ни одной записи.")
        return
    lines = []
    for amount, desc, entry_date, photo_file_id in rows:
        d = datetime.fromisoformat(entry_date)
        mark = " 📷" if photo_file_id else ""
        lines.append(f"{d.strftime('%d.%m')} — {fmt_eur(amount)} € — {desc}{mark}")
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import csv
    import io

    user_id = update.effective_user.id
    rows = export_rows(user_id)
    if not rows:
        await update.message.reply_text("Пока нечего выгружать.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата", "Сумма (EUR)", "Описание"])
    for entry_date, amount, desc in rows:
        d = datetime.fromisoformat(entry_date)
        writer.writerow([d.strftime("%d.%m.%Y"), f"{amount:.2f}", desc])
    data = buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Cyrillic correctly
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename="raskhody.csv",
    )



def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set (set it as an environment variable).")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("total", total_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("recent", recent_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
