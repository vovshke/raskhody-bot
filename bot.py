import os
import re
import io
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

CATEGORY_RULES = [
    ("Топливо", ["бензин", "дизель", "топливо", "заправ", "азс"]),
    ("Еда", ["еда", "обед", "кофе", "ресторан", "кафе", "продукты", "ужин", "завтрак", "магазин продукт"]),
    ("Инструменты", ["инструмент", "дрель", "ключ", "болгарк", "сверл", "насадк"]),
    ("Запчасти", ["запчаст", "деталь", "ремонт", "масло моторн", "фильтр", "тормоз"]),
    ("Транспорт", ["такси", "автобус", "метро", "парковк", "штраф", "билет"]),
    ("Связь", ["интернет", "телефон", "связь", "тариф"]),
]
CATEGORY_EMOJI = {
    "Топливо": "⛽", "Еда": "🍔", "Инструменты": "🔧",
    "Запчасти": "⚙️", "Транспорт": "🚕", "Связь": "📶", "Другое": "📦",
}


def categorize(desc: str) -> str:
    low = desc.lower()
    for name, keywords in CATEGORY_RULES:
        if any(k in low for k in keywords):
            return name
    return "Другое"


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
            photo_file_id TEXT,
            category TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            monthly_limit REAL
        )
    """)
    try:
        c.execute("ALTER TABLE expenses ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def add_expense(user_id, amount, description, entry_date, category):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO expenses (user_id, amount, description, entry_date, photo_file_id, category) VALUES (?, ?, ?, ?, NULL, ?)",
        (user_id, amount, description, entry_date.isoformat(), category),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def delete_expense(user_id, expense_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def delete_last(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    expense_id = row[0]
    c.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    conn.close()
    return expense_id


def clear_all(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def attach_photo(user_id, file_id):
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
        "SELECT id, amount, description, entry_date, photo_file_id, category FROM expenses WHERE user_id=? ORDER BY entry_date DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_breakdown(user_id, since):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(category,'Другое'), SUM(amount) FROM expenses WHERE user_id=? AND entry_date>=? GROUP BY category ORDER BY SUM(amount) DESC",
        (user_id, since.isoformat()),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def export_rows(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT entry_date, amount, description, category FROM expenses WHERE user_id=? ORDER BY entry_date",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def set_limit(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (user_id, monthly_limit) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET monthly_limit=excluded.monthly_limit",
        (user_id, amount),
    )
    conn.commit()
    conn.close()


def get_limit(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT monthly_limit FROM settings WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


# ---------- PARSING ----------

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


def month_start():
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def week_start():
    now = datetime.now()
    return (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- HANDLERS ----------

WELCOME = (
    "Привет! Я записываю твои расходы.\n\n"
    "Пиши (или диктуй через микрофон клавиатуры) сумму и что купил:\n"
    "  300 бензин\n"
    "  вчера 50 запчасти\n"
    "  20.07 120 инструмент\n\n"
    "Категория определяется автоматически. После записи можешь прислать фото чека — прикрепится к последней записи.\n\n"
    "Просмотр и правка:\n"
    "/recent — последние записи (с номерами)\n"
    "/undo — удалить последнюю запись\n"
    "/delete N — удалить запись с номером N\n"
    "/clear — удалить вообще всё (нужно подтверждение)\n\n"
    "Отчёты:\n"
    "/total — сумма всего\n"
    "/today — сумма за сегодня\n"
    "/week — отчёт за 7 дней по категориям\n"
    "/month — отчёт за текущий месяц по категориям\n"
    "/chart — диаграмма расходов по категориям за месяц\n"
    "/export — выгрузка в CSV\n\n"
    "Лимит:\n"
    "/limit 500 — установить месячный лимит\n"
    "/limit — посмотреть текущий лимит и расход"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    amount, desc, date = parse_entry(text)
    category = categorize(desc)
    add_expense(user_id, amount, desc, date, category)

    total = get_total(user_id)
    reply = (
        f"Записал: {fmt_eur(amount)} € — {desc} {CATEGORY_EMOJI.get(category,'')} {category} ({date.strftime('%d.%m.%Y')})\n"
        f"Итого: {fmt_eur(total)} €"
    )

    limit = get_limit(user_id)
    if limit:
        month_total = get_total(user_id, since=month_start())
        ratio = month_total / limit if limit > 0 else 0
        if ratio >= 1:
            reply += f"\n\n⚠️ Лимит месяца превышен: {fmt_eur(month_total)} € из {fmt_eur(limit)} €"
        elif ratio >= 0.8:
            reply += f"\n\n⚠️ Уже {fmt_eur(month_total)} € из {fmt_eur(limit)} € лимита ({int(ratio*100)}%)"

    await update.message.reply_text(reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
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
    for expense_id, amount, desc, entry_date, photo_file_id, category in rows:
        d = datetime.fromisoformat(entry_date)
        mark = " 📷" if photo_file_id else ""
        cat = category or "Другое"
        lines.append(
            f"#{expense_id} · {d.strftime('%d.%m')} — {fmt_eur(amount)} € — {desc} "
            f"{CATEGORY_EMOJI.get(cat,'')}{mark}"
        )
    lines.append("\nУдалить запись: /delete N (номер после #)")
    await update.message.reply_text("\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    deleted_id = delete_last(user_id)
    if deleted_id:
        await update.message.reply_text(f"Удалил последнюю запись (#{deleted_id}).")
    else:
        await update.message.reply_text("Нечего удалять.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи номер записи: /delete 42 (номер смотри в /recent)")
        return
    try:
        expense_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом, например: /delete 42")
        return
    if delete_expense(user_id, expense_id):
        await update.message.reply_text(f"Запись #{expense_id} удалена.")
    else:
        await update.message.reply_text(f"Не нашёл запись #{expense_id} (проверь /recent).")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args and context.args[0] == "CONFIRM":
        clear_all(user_id)
        await update.message.reply_text("Все записи удалены.")
    else:
        await update.message.reply_text(
            "Это удалит ВСЕ твои записи без возможности восстановить.\n"
            "Если уверен — отправь: /clear CONFIRM"
        )


def format_breakdown(rows, total):
    if not rows:
        return "Пока нет записей за этот период."
    lines = []
    for category, amount in rows:
        cat = category or "Другое"
        lines.append(f"{CATEGORY_EMOJI.get(cat,'')} {cat}: {fmt_eur(amount)} €")
    lines.append(f"\nИтого: {fmt_eur(total)} €")
    return "\n".join(lines)


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = week_start()
    rows = get_breakdown(user_id, since)
    total = get_total(user_id, since=since)
    await update.message.reply_text("За последние 7 дней:\n\n" + format_breakdown(rows, total))


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = month_start()
    rows = get_breakdown(user_id, since)
    total = get_total(user_id, since=since)
    await update.message.reply_text(f"За {since.strftime('%B')}:\n\n" + format_breakdown(rows, total))


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        try:
            amount = float(context.args[0].replace(",", "."))
        except ValueError:
            await update.message.reply_text("Укажи число, например: /limit 500")
            return
        set_limit(user_id, amount)
        await update.message.reply_text(f"Месячный лимит установлен: {fmt_eur(amount)} €")
        return

    limit = get_limit(user_id)
    if not limit:
        await update.message.reply_text("Лимит не установлен. Задать: /limit 500")
        return
    month_total = get_total(user_id, since=month_start())
    ratio = month_total / limit if limit > 0 else 0
    await update.message.reply_text(
        f"Лимит: {fmt_eur(limit)} €\n"
        f"Потрачено в этом месяце: {fmt_eur(month_total)} € ({int(ratio*100)}%)"
    )


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = month_start()
    rows = get_breakdown(user_id, since)
    if not rows:
        await update.message.reply_text("Нет данных за этот месяц для графика.")
        return

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        await update.message.reply_text("Графики временно недоступны (не установлена библиотека).")
        return

    categories = [r[0] or "Другое" for r in rows]
    amounts = [r[1] for r in rows]
    colors = ["#17E8C0", "#FF9F2E", "#4C8BF5", "#FF5A5F", "#8B5CF6", "#22C55E", "#94A3B8"]

    fig, ax = plt.subplots(figsize=(6, 4))
    bars = ax.bar(categories, amounts, color=colors[: len(categories)])
    ax.set_ylabel("EUR")
    ax.set_title(f"Расходы за {since.strftime('%B')}")
    plt.xticks(rotation=30, ha="right")
    for bar, amount in zip(bars, amounts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), fmt_eur(amount),
                 ha="center", va="bottom", fontsize=9)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    await update.message.reply_photo(photo=buf)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import csv

    user_id = update.effective_user.id
    rows = export_rows(user_id)
    if not rows:
        await update.message.reply_text("Пока нечего выгружать.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата", "Сумма (EUR)", "Описание", "Категория"])
    for entry_date, amount, desc, category in rows:
        d = datetime.fromisoformat(entry_date)
        writer.writerow([d.strftime("%d.%m.%Y"), f"{amount:.2f}", desc, category or "Другое"])
    data = buf.getvalue().encode("utf-8-sig")
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
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
