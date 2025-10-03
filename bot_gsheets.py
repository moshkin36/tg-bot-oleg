# bot_webhook.py
# Вебхук-бот: FastAPI + PTB 21.6 + OpenAI + Google Sheets (SERVICE_ACCOUNT_JSON)

import os, time, json, logging, tempfile
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
load_dotenv()

import gspread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# ----------------- ENV -----------------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-transcribe")
GSHEET_ID = os.getenv("GSHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")  # содержимое JSON ключа
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "supersecret123")  # задайте в Render
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")  # Render даёт RENDER_EXTERNAL_URL
PORT = int(os.getenv("PORT", "8000"))

# ----------------- LOG -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ----------------- Clients -----------------
if not (BOT_TOKEN and OPENAI_API_KEY and GSHEET_ID):
    raise SystemExit("Нужны TELEGRAM_TOKEN, OPENAI_API_KEY, GSHEET_ID.")

client = OpenAI(api_key=OPENAI_API_KEY)

def gspread_client():
    if not SERVICE_ACCOUNT_JSON:
        raise SystemExit("Задайте SERVICE_ACCOUNT_JSON (полный текст service.json).")
    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Некорректный SERVICE_ACCOUNT_JSON: {e}")
    return gspread.service_account_from_dict(creds_dict)

def open_sheet():
    gc = gspread_client()
    sh = gc.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("answers")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title="answers", rows=1000, cols=6)
        ws.append_row(["timestamp", "user_id", "username", "q_index", "question", "transcript"])
    return ws

# ----------------- Q&A -----------------
QUESTIONS = [
    "Кто ты из Гарри Поттера?",
    "Почему небо голубое?",
    "Сколько времени нужно, чтобы дойти пешком 5 км в гору ночью?",
]
MAIN_KB = ReplyKeyboardMarkup([["/next", "/repeat", "/help"]], resize_keyboard=True)
user_state: Dict[int, Dict[str, Any]] = {}

# ----------------- PTB Application -----------------
app_tg = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"i": 0}
    greet = ("Привет! Сейчас я задам тебе несколько важных вопросов — не думай, отвечай душой! "
             "Запиши голосовое сообщение — это важно!")
    await update.message.reply_text(greet, reply_markup=MAIN_KB)
    await ask_current(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отвечай ГОЛОСОМ. /repeat — повторить вопрос, /next — следующий.")

async def ask_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    i = user_state.get(uid, {"i": 0})["i"]
    if i >= len(QUESTIONS):
        await update.message.reply_text("Вопросы закончились. Спасибо! Набери /start, чтобы пройти заново.")
        return
    await update.message.reply_text(
        f"Вопрос {i+1}/{len(QUESTIONS)}:\n\n{QUESTIONS[i]}\n\nОтветь ГОЛОСОВЫМ сообщением."
    )

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = user_state.setdefault(uid, {"i": 0})
    if st["i"] < len(QUESTIONS) - 1:
        st["i"] += 1
    await ask_current(update, context)

async def repeat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_current(update, context)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    st = user_state.setdefault(uid, {"i": 0})
    i = st["i"]

    if i >= len(QUESTIONS):
        await update.message.reply_text("Серия завершена. Набери /start, чтобы начать заново.")
        return

    # Скачиваем голос и шифруем
    try:
        vfile = await update.message.voice.get_file()
        # Telegram отдаёт OGG/Opus — OpenAI нормально ест
        with tempfile.TemporaryDirectory() as tmpd:
            ogg_path = Path(tmpd) / f"voice_{uid}_{int(time.time())}.ogg"
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

    # Записываем в таблицу
    try:
        ws = open_sheet()
        username = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip()
        ws.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            str(uid),
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

# Регистрация хэндлеров
app_tg.add_handler(CommandHandler("start", start))
app_tg.add_handler(CommandHandler("help", help_cmd))
app_tg.add_handler(CommandHandler("next", next_cmd))
app_tg.add_handler(CommandHandler("repeat", repeat_cmd))
app_tg.add_handler(MessageHandler(filters.VOICE, voice_handler))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# ----------------- FastAPI -----------------
api = FastAPI()

@api.get("/", response_class=PlainTextResponse)
async def health():
    return "ok"

@api.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    # Преобразуем апдейт и передаём в PTB
    update = Update.de_json(data, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}

@api.on_event("startup")
async def setup_webhook():
    # Сбросить возможный старый вебхук + поставить новый
    from httpx import AsyncClient
    base_url = PUBLIC_URL
    if not base_url:
        logging.warning("PUBLIC_URL не задан (RENDER_EXTERNAL_URL). Вебхук не будет установлен автоматически.")
        return

    webhook_url = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    logging.info("Устанавливаю webhook -> %s", webhook_url)

    async with AsyncClient(timeout=10) as http:
        # удаляем старый
        await http.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
                       params={"drop_pending_updates": "true"})
        # ставим новый
        r = await http.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                           params={"url": webhook_url, "secret_token": WEBHOOK_SECRET})
        logging.info("setWebhook status=%s, body=%s", r.status_code, r.text)

# ----------------- Local run -----------------
if __name__ == "__main__":
    # локально: uvicorn + /webhook
    uvicorn.run("bot_webhook:api", host="0.0.0.0", port=PORT, reload=False)
