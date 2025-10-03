# bot_gsheets.py
# Вебхук-бот: FastAPI + PTB 21.6 + OpenAI + Google Sheets (без service.json на диске)

import os, time, json, logging, tempfile
from pathlib import Path
from typing import Dict, Any

from dotenv import load_dotenv
load_dotenv()  # локально читает .env; в Render берёт из Variables

import gspread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from openai import OpenAI

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------- ENV ----------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-transcribe")
GSHEET_ID = os.getenv("GSHEET_ID")

SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")  # полное содержимое service.json
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "supersecret123")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")  # Render даёт RENDER_EXTERNAL_URL

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Guards ----------
if not (BOT_TOKEN and OPENAI_API_KEY and GSHEET_ID):
    raise SystemExit("Нужны переменные TELEGRAM_TOKEN, OPENAI_API_KEY и GSHEET_ID.")
if not SERVICE_ACCOUNT_JSON:
    raise SystemExit("Нужна переменная SERVICE_ACCOUNT_JSON (вставьте полный JSON ключа сервиса).")

# ---------- Clients ----------
client = OpenAI(api_key=OPENAI_API_KEY)

def gspread_client():
    try:
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise SystemExit(f"SERVICE_ACCOUNT_JSON не валиден: {e}")
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

# ---------- Q&A ----------
QUESTIONS = [
    "Кто ты из Гарри Поттера?",
    "Почему небо голубое?",
    "Сколько времени нужно, чтобы дойти пешком 5 км в гору ночью?",
]
MAIN_KB = ReplyKeyboardMarkup([["/next", "/repeat", "/help"]], resize_keyboard=True)
user_state: Dict[int, Dict[str, Any]] = {}

# ---------- Telegram Application ----------
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

    # Скачать голос и расшифровать
    try:
        vfile = await update.message.voice.get_file()
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

    # Записать в Google Sheets
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

# Регистрируем хэндлеры
app_tg.add_handler(CommandHandler("start", start))
app_tg.add_handler(CommandHandler("help", help_cmd))
app_tg.add_handler(CommandHandler("next", next_cmd))
app_tg.add_handler(CommandHandler("repeat", repeat_cmd))
app_tg.add_handler(MessageHandler(filters.VOICE, voice_handler))
app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# ---------- FastAPI ----------
api = FastAPI()

@api.get("/", response_class=PlainTextResponse)
async def health():
    return "ok"

@api.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    # Телеграм может прислать заголовок 'X-Telegram-Bot-Api-Secret-Token'
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret and header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")

    data = await request.json()
    update = Update.de_json(data, app_tg.bot)
    await app_tg.process_update(update)
    return {"ok": True}

@api.on_event("startup")
async def setup_webhook():
    # 1) Ставим вебхук
    from httpx import AsyncClient
    if not PUBLIC_URL:
        logging.warning("PUBLIC_URL (RENDER_EXTERNAL_URL) не задан – вебхук не будет установлен автоматически.")
    else:
        webhook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        logging.info("Устанавливаю webhook -> %s", webhook_url)
        async with AsyncClient(timeout=10) as http:
            await http.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
                params={"drop_pending_updates": "true"}
            )
            r = await http.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                params={"url": webhook_url, "secret_token": WEBHOOK_SECRET}
            )
            logging.info("setWebhook status=%s, body=%s", r.status_code, r.text)

    # 2) ВАЖНО: инициализируем и запускаем PTB-приложение
    await app_tg.initialize()
    await app_tg.start()
    me = await app_tg.bot.get_me()
    logging.info("PTB initialized. Бот: @%s (id=%s)", me.username, me.id)

@api.on_event("shutdown")
async def shutdown():
    # Корректная остановка PTB
    await app_tg.stop()
    await app_tg.shutdown()
    logging.info("PTB stopped/shutdown completed")

# Запуск локально:
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot_gsheets:api", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
