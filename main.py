import asyncio
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
import tempfile

import google.generativeai as genai
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("srt-bot")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("8871786955:AAGy7aWgp8OyKIpBUb1pFV6O9JsYleDs8NQ")
GEMINI_API_KEY = os.environ.get("AQ.Ab8RN6JnfAgP4wunO1fYY278tuK_3KIxwmZIQsQs6PeYJXSh_Q")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

try:
    MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "65536"))
except ValueError:
    MAX_OUTPUT_TOKENS = 65536

MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram Bot API download limit (20 MB)

EXT_TO_MIME = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}

PROMPT = """You are a professional subtitle writer.
Listen to the attached media carefully and produce subtitles in natural, fluent Myanmar (Burmese) language using Myanmar Unicode script.
- If the speech is already Burmese, transcribe it accurately.
- If the speech is in another language, translate it into natural Myanmar.

Output rules (very important):
1. Output ONLY valid SRT content. No explanations, no greetings, no markdown, no code fences.
2. Number cues sequentially starting at 1.
3. Timestamp format must be exactly HH:MM:SS,mmm --> HH:MM:SS,mmm (comma before milliseconds).
4. Each cue should last about 2 to 7 seconds and contain at most 2 short lines.
5. Timestamps must match the actual speech timing, and cover the whole media from start to end.
6. Separate cues with one blank line.
7. Skip sections with no speech (silence or pure music).
"""

# Only one media job runs at a time (keeps RAM/disk low on small Render plans).
PROCESS_LOCK = asyncio.Semaphore(1)

# --------------------------------------------------------------------------
# Flask keep-alive web server (runs in a daemon thread)
# --------------------------------------------------------------------------
app_web = Flask(__name__)


@app_web.route("/")
def home():
    return "Bot is running!"


@app_web.route("/health")
def health():
    return "ok"
def clean_srt(text: str) -> str:
    """Strip markdown fences and any preamble before the first SRT cue."""
    fence = chr(96) * 3  # three backtick characters
    text = (text or "").strip()

    if text.startswith(fence):
        newline = text.find("\n")
        text = text[newline + 1:] if newline != -1 else ""
    text = text.strip()
    if text.endswith(fence):
        text = text[:-3]
    text = text.strip()

    match = re.search(r"(?m)^\s*\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", text)
    if match:
        text = text[match.start():].lstrip()
    return text

def run_web() -> None:
    try:
        port = int(os.environ.get("PORT", 8080))
    except ValueError:
        port = 8080
    log.info("Starting keep-alive web server on port %s", port)
    app_web.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive() -> None:
    thread = threading.Thread(target=run_web, daemon=True)
    thread.start()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resolve_mime(mime_type, file_name):
    """Return a Gemini-friendly mime type, or None if the file is unsupported."""
    ext = Path(file_name).suffix.lower() if file_name else ""
    if mime_type and (mime_type.startswith("audio/") or mime_type.startswith("video/")):
        return mime_type
    if ext in EXT_TO_MIME:
        return EXT_TO_MIME[ext]
    guessed = mimetypes.guess_type(file_name or "")[0]
    if guessed and (guessed.startswith("audio/") or guessed.startswith("video/")):
        return guessed
    return None


def pick_extension(mime_type, file_name):
    if file_name and Path(file_name).suffix:
        return Path(file_name).suffix.lower()
    if mime_type:
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            return ext
    return ".bin"


def safe_stem(file_name):
    stem = Path(file_name).stem if file_name else "subtitle"
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return stem or "subtitle"


def wait_until_active(uploaded, timeout=900):
    """Wait until Gemini has finished processing the uploaded file."""
    started = time.time()
    current = uploaded
    while current.state.name == "PROCESSING":
        if time.time() - started > timeout:
            raise TimeoutError("Gemini file processing timed out.")
        time.sleep(5)
        current = genai.get_file(current.name)
    if current.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini could not process the file (state: {current.state.name}).")
    return current


def generate_srt(local_path: str, mime_type: str) -> str:
    """Blocking: upload to Gemini, generate SRT, ALWAYS delete the uploaded file."""
    uploaded = None
    try:
        uploaded = genai.upload_file(
            path=local_path,
            mime_type=mime_type,
            display_name=f"media-{uuid.uuid4().hex[:8]}",
        )
        active = wait_until_active(uploaded)

        model = genai.GenerativeModel(MODEL_NAME)
        last_error = None
        for attempt in range(1, 3):
            try:
                response = model.generate_content(
                    [active, PROMPT],
                    generation_config=genai.GenerationConfig(
                        temperature=0.2,
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                    ),
                    request_options={"timeout": 1200},
                )
                srt = clean_srt(response.text)
                if "-->" not in srt:
                    raise ValueError("Gemini returned no valid SRT content.")
                return srt
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("Gemini attempt %d failed: %s", attempt, exc)
                time.sleep(3)
        raise RuntimeError(f"Gemini failed after retries: {last_error}")
    finally:
        if uploaded is not None:
            try:
                genai.delete_file(uploaded.name)
                log.info("Deleted Gemini file %s", uploaded.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not delete Gemini file: %s", exc)


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001
        pass


def remove_file(path) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "မင်္ဂလာပါ 👋\n"
        "MP4 သို့မဟုတ် MP3 file (အများဆုံး 20MB) ပို့လိုက်ပါ။\n"
        "မြန်မာ subtitle (.srt) ပြန်ထုတ်ပေးပါမယ်။"
    )
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    media = (
        message.audio
        or message.video
        or message.voice
        or message.video_note
        or message.document
    )
    if media is None:
        return

    file_name = getattr(media, "file_name", None)
    mime_type = resolve_mime(getattr(media, "mime_type", None), file_name)
    if mime_type is None:
        await message.reply_text("⚠️ ဒီ file အမျိုးအစားကို မထောက်ပံ့ပါ။ MP4 / MP3 ပို့ပါ။")
        return

    if media.file_size and media.file_size > MAX_FILE_BYTES:
        await message.reply_text("⚠️ File က 20MB ထက် ကြီးနေပါတယ်။ အသေးချုံ့ပြီး ပြန်ပို့ပါ။")
        return

    status = await message.reply_text("⏳ လက်ခံရရှိပါပြီ၊ စောင့်ပေးပါ...")

    local_path = None
    srt_path = None
    try:
        async with PROCESS_LOCK:
            ext = pick_extension(mime_type, file_name)
            local_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}{ext}")

            await safe_edit(status, "📥 File ဒေါင်းလုဒ်လုပ်နေပါတယ်...")
            tg_file = await context.bot.get_file(media.file_id)
            await tg_file.download_to_drive(custom_path=local_path)

            if os.path.getsize(local_path) > MAX_FILE_BYTES:
                await safe_edit(status, "⚠️ File က 20MB ထက် ကြီးနေပါတယ်။")
                return

            await safe_edit(status, "🤖 Gemini နဲ့ စာတန်းထိုး ထုတ်နေပါတယ် (ခဏကြာနိုင်ပါတယ်)...")
            srt_text = await asyncio.to_thread(generate_srt, local_path, mime_type)

            stem = safe_stem(file_name)
            srt_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}.srt")
            with open(srt_path, "w", encoding="utf-8") as fh:
                fh.write(srt_text)

            with open(srt_path, "rb") as fh:
                await message.reply_document(
                    document=fh,
                    filename=f"{stem}.srt",
                    caption="✅ Myanmar subtitle ပြီးပါပြီ",
                )
            try:
                await status.delete()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.exception("Processing failed")
        await safe_edit(status, f"❌ Error: {str(exc)[:300]}")
    finally:
        remove_file(local_path)
        remove_file(srt_path)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("Missing environment variable: TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    if not GEMINI_API_KEY:
        log.error("Missing environment variable: GEMINI_API_KEY")
        sys.exit(1)

    genai.configure(api_key=GEMINI_API_KEY)
    keep_alive()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .build()
    )

    media_filter = (
        filters.AUDIO
        | filters.VIDEO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.Document.ALL
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(media_filter, handle_media))
    application.add_error_handler(on_error)

    log.info("Bot started with model %s", MODEL_NAME)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
