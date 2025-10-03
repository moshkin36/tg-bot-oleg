# bot_gsheets.py
# Голосовые ответы -> транскрипция OpenAI -> запись в Google Sheets (Render/облако, без service.json на диске)

import os, time, tempfile, logging, json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # локально читает .env; в облаке берёт из Variables

import gspread
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# ---------- ENV ----------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-transcribe")
GSHEET_ID = os.getenv("GSHEET_ID")

# ключ сервис-аккаунта как ТЕКСТ JSON (переменная окружения)
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
# (опционально) путь к файлу для локального запуска
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service.json")

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- OpenAI ----------
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY. Задай переменную окружения.")
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- ВОПРОСЫ ----------
QUESTIONS = [
    "Кто ты из Гарри Поттера?",
    "Почему небо голубое?",
    "Сколько времени нужно, чтобы дойти пешком 5 км в гору ночью?"
]
MAIN_KB = ReplyKeyboardMarkup([["/next", "/repeat", "/help"]], resize_keyboard=True)
user_state: dict[int, dict] = {}

# ---------- Google Sheets ----------
def gspread_client():
    """Создаём gspread-клиент: приоритетно из SERVICE_ACCOUNT_JSON, иначе из файла."""
    if SERVICE_ACCOUNT_JSON:
        try:
            creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as e:
            raise SystemExit(f"SERVICE_ACCOUNT_JSON не валиден: {e}")
        return gspread.service_account_from_dict(creds_dict)
    if Path(SERVICE_ACCOUNT_FILE).exists():
        return gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    raise SystemExit("Нет SERVICE_ACCOUNT_JSON и нет файла service.json. Задай один из вариантов.")

def open_sheet():
    gc = gspread_client()
    sh = gc.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("answers")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title="answers", rows=1000, cols=6)
        ws.append_row(["timestamp", "user_id", "username", "q_index", "question", "transcript"])
    return ws

# ---------- Handlers ----------
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
    i = user_state.get(user_id, {"i": 0})["i"]
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

    # Скачать голос и расшифровать
    try:
        vfile = await update.message.voice.get_file()
        with tempfile.TemporaryDirectory() as tmpd:
            ogg_path = Path(tmpd) / f"voice_{user_id}_{int(time.time())}.ogg"
            await vfile.download_to_drive(ogg_path.as_posix())
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

    # Записать в Google Sheets
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

    if st["i"] < len(QUESTIONS) - 1:
        st["i"] += 1
        await ask_current(update, context)
    else:
        await update.message.reply_text("🎉 Это был последний вопрос. Лист «answers» обновлён.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пожалуйста, отвечай ГОЛОСОМ. Нажми /repeat для текущего вопроса.")

# ---------- Entry ----------
def main():
    missing = [k for k, v in {
        "TELEGRAM_TOKEN": BOT_TOKEN,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "GSHEET_ID": GSHEET_ID,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Нет переменных: {', '.join(missing)}")

    app = Application.builder().token(BOT_TOKEN).build()

    async def post_init(application):
        me = await application.bot.get_me()
        logging.info(f"Бот запущен как @{me.username} (id={me.id})")
        # жёстко переключаемся на polling и очищаем старые апдейты
        await application.bot.delete_webhook(drop_pending_updates=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("repeat", repeat_cmd))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logging.info("Бот запускается…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        post_init=post_init,
        drop_pending_updates=True,
        close_loop=False,
        stop_signals=None,
    )

if __name__ == "__main__":
    main()
