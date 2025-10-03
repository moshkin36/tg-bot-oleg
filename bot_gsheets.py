# bot_gsheets.py
# Телеграм-бот: задаёт вопросы, принимает голос, расшифровывает в OpenAI и пишет в Google Sheets.

import os, time, tempfile, logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # читает .env из текущей папки

import gspread
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# === Настройки из .env ===
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-transcribe")
GSHEET_ID = os.getenv("GSHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service.json")

# === Логи ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === Клиент OpenAI ===
client = OpenAI(api_key=OPENAI_API_KEY)

# === Вопросы ===
QUESTIONS = [
    "Кто ты из Гарри Поттера?",
    "Почему небо голубое?",
    "Сколько времени нужно, чтобы дойти пешком 5 км в гору ночью?"
]

MAIN_KB = ReplyKeyboardMarkup([["/next", "/repeat", "/help"]], resize_keyboard=True)

# простая память в ОЗУ: user_id -> {"i": int текущего вопроса}
user_state = {}

# === Работа с Google Sheets ===
def open_sheet():
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("answers")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title="answers", rows=1000, cols=6)
        ws.append_row(["timestamp", "user_id", "username", "q_index", "question", "transcript"])
    return ws

# === Хэндлеры ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_state[user_id] = {"i": 0}
    greet = ("Привет! Сейчас я задам тебе несколько важных вопросов — не думай, отвечай душой! "
             "Запиши голосовое сообщение — это важно!")
    await update.message.reply_text(greet, reply_markup=MAIN_KB)
    await ask_current(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отвечай ГОЛОСОМ. /repeat — повторить вопрос, /next — следующий.")

async def ask_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = user_state.get(user_id, {"i": 0})
    i = st["i"]
    if i >= len(QUESTIONS):
        await update.message.reply_text("Вопросы закончились. Спасибо! Набери /start, чтобы пройти заново.")
        return
    await update.message.reply_text(
        f"Вопрос {i+1}/{len(QUESTIONS)}:\n\n{QUESTIONS[i]}\n\nОтветь ГОЛОСОВЫМ сообщением."
    )

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = user_state.setdefault(user_id, {"i": 0})
    if st["i"] < len(QUESTIONS) - 1:
        st["i"] += 1
    await ask_current(update, context)

async def repeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_current(update, context)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    st = user_state.setdefault(user_id, {"i": 0})
    i = st["i"]

    if i >= len(QUESTIONS):
        await update.message.reply_text("Серия завершена. Набери /start, чтобы начать заново.")
        return

    try:
        # скачиваем голос
        vfile = await update.message.voice.get_file()
        with tempfile.TemporaryDirectory() as tmpd:
            ogg_path = Path(tmpd) / f"voice_{user_id}_{int(time.time())}.ogg"
            await vfile.download_to_drive(ogg_path.as_posix())

            # транскрипция
            with open(ogg_path, "rb") as audio:
                tr = client.audio.transcriptions.create(
                    model=OPENAI_MODEL,
                    file=audio,
                    response_format="text"
                )
            transcript = (tr or "").strip()
    except Exception as e:
        logging.exception("Ошибка транскрипции")
        await update.message.reply_text(f"⚠️ Не удалось расшифровать голос: {e}")
        return

    # запись в таблицу
    try:
        ws = open_sheet()
        username = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip()
        ws.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            str(user_id),
            username,
            str(i),
            QUESTIONS[i],
            transcript
        ])
    except Exception as e:
        logging.exception("Ошибка записи в Google Sheets")
        await update.message.reply_text(f"⚠️ Не удалось записать в таблицу: {e}")
        return

    await update.message.reply_text(
        f"✅ Получил и записал ответ.\nТекст: {transcript[:400] + ('…' if len(transcript)>400 else '')}"
    )

    # следующий вопрос / финал
    if st["i"] < len(QUESTIONS) - 1:
        st["i"] += 1
        await ask_current(update, context)
    else:
        await update.message.reply_text("🎉 Это был последний вопрос. Данные в листе «answers» обновлены.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пожалуйста, отвечай ГОЛОСОМ. Нажми /repeat для текущего вопроса.")

# === Точка входа ===
def main():
    # быстрый чек окружения
    missing = [k for k, v in {
        "TELEGRAM_TOKEN": BOT_TOKEN,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "GSHEET_ID": GSHEET_ID,
        "SERVICE_ACCOUNT_FILE": SERVICE_ACCOUNT_FILE,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Отсутствуют переменные в .env: {', '.join(missing)}")

    if not Path(SERVICE_ACCOUNT_FILE).exists():
        raise SystemExit(f"Файл сервис-аккаунта не найден: {SERVICE_ACCOUNT_FILE}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("repeat", repeat_cmd))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logging.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling()

if __name__ == "__main__":
    main()
