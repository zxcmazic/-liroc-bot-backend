import asyncio
import logging
import os

from aiohttp import web
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CONFIGURATION =================
# ВАЖНО: не храните секреты в коде. Задавайте их через переменные окружения
# (например, в панели вашего хостинга — Railway/Render и т.п.).
# Значения ниже — fallback ТОЛЬКО чтобы бот не падал молча, если забыли
# настроить окружение.
#
# ВНИМАНИЕ: если токен бота и ключ OpenRouter из самой первой версии файла
# всё ещё не отозваны — сделайте это в @BotFather и в кабинете OpenRouter,
# они были показаны в открытом чате.

BOT_TOKEN = os.getenv("BOT_TOKEN", "8951677948:AAF_iNm9bExQ7JVvweYGzp_KScQwYDwdx1g")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# --- AdsGram ---
# blockid и token берутся из личного кабинета AdsGram (https://partner.adsgram.ai)
# blockid — только числовая часть, БЕЗ префикса "bot-"
ADSGRAM_BLOCK_ID = os.getenv("ADSGRAM_BLOCK_ID", "47499")
ADSGRAM_TOKEN = os.getenv("ADSGRAM_TOKEN", "72adc6ac8da94726b9dca86022b36eec")
ADSGRAM_LANGUAGE = os.getenv("ADSGRAM_LANGUAGE", "ru")

DEFAULT_ATTEMPTS = 3
REWARD_ATTEMPTS = 3
# Этот порт нужен ТОЛЬКО для одного эндпоинта — подтверждения награды от AdsGram
# (см. api_adsgram_reward ниже). Сам чат бота от порта не зависит.
PORT = int(os.getenv("PORT", 8080))
# =================================================

logging.basicConfig(level=logging.INFO)

# Общий словарь попыток: {user_id: count}
# ПРИМЕЧАНИЕ: это in-memory хранилище — при перезапуске бота все данные теряются.
# Для продакшена лучше использовать SQLite/Redis/Postgres.
user_attempts = {}


def get_ai_solution(prompt: str, system_prompt: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "openrouter/auto",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        logging.error(f"AI Error: status={response.status_code} body={response.text[:300]}")
    except Exception as e:
        logging.error(f"AI Error: {e}")
    return "❌ Ошибка получения ответа от ИИ."


def get_adsgram_ad(tgid: int, language: str = ADSGRAM_LANGUAGE) -> dict | None:
    """
    Запрашивает рекламный блок у AdsGram для показа в чате бота.
    Документация: https://docs.adsgram.ai/bots/block-integration
    """
    if not ADSGRAM_BLOCK_ID or not ADSGRAM_TOKEN:
        logging.warning("AdsGram не настроен: отсутствует ADSGRAM_BLOCK_ID или ADSGRAM_TOKEN")
        return None

    url = "https://api.adsgram.ai/advbot"
    params = {
        "tgid": tgid,
        "blockid": ADSGRAM_BLOCK_ID,
        "language": language,
        "token": ADSGRAM_TOKEN,
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        logging.warning(f"AdsGram: no fill or error, status={response.status_code} body={response.text[:300]}")
    except Exception as e:
        logging.error(f"AdsGram request error: {e}")
    return None


async def send_adsgram_ad(update: Update, tgid: int) -> bool:
    """
    Показывает пользователю рекламу от AdsGram прямо в чате.
    Место показа рекламы: строго здесь — когда у пользователя закончились
    бесплатные попытки и он хочет получить бонусные (+REWARD_ATTEMPTS).
    Это единственное место в боте, где показывается реклама — так модератору
    легко это найти и проверить.
    """
    ad = get_adsgram_ad(tgid)
    if not ad:
        await update.message.reply_text(
            "❌ Реклама сейчас недоступна, попробуйте немного позже."
        )
        return False

    buttons = []
    if ad.get("button_name") and ad.get("click_url"):
        buttons.append([InlineKeyboardButton(ad["button_name"], url=ad["click_url"])])
    if ad.get("button_reward_name") and ad.get("reward_url"):
        buttons.append([InlineKeyboardButton(ad["button_reward_name"], url=ad["reward_url"])])

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    text_html = ad.get("text_html", "")

    # protect_content=True — запрещает пересылку рекламного сообщения,
    # это прямое требование модерации AdsGram.
    try:
        if ad.get("image_url"):
            await update.message.reply_photo(
                photo=ad["image_url"],
                caption=text_html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=True,
            )
        else:
            await update.message.reply_text(
                text_html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                protect_content=True,
                disable_web_page_preview=True,
            )
    except Exception as e:
        logging.error(f"Failed to send AdsGram ad: {e}")
        return False

    await update.message.reply_text(
        f"⚡️ Нажмите «{ad.get('button_reward_name', 'Claim reward')}» после просмотра, "
        f"чтобы получить +{REWARD_ATTEMPTS} попыток. "
        f"Начисление произойдёт автоматически — оно подтверждается сервером AdsGram, "
        f"а не приложением."
    )
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_attempts:
        user_attempts[user_id] = DEFAULT_ATTEMPTS

    await update.message.reply_text(
        f"Привет! Я твой ИИ-помощник в чате.\n\n"
        f"⚡ Доступно попыток: {user_attempts[user_id]}\n"
        f"Просто напиши мне вопрос!"
    )


async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_attempts:
        user_attempts[user_id] = DEFAULT_ATTEMPTS

    if user_attempts[user_id] <= 0:
        await update.message.reply_text(
            "❌ Закончились бесплатные попытки!\n"
            "Посмотрите рекламу ниже, чтобы получить ещё немного энергии ⚡"
        )
        await send_adsgram_ad(update, user_id)
        return

    user_attempts[user_id] -= 1
    status_msg = await update.message.reply_text("🧠 ИИ в чате думает...")

    solution = get_ai_solution(
        update.message.text,
        "Ты — ИИ-помощник в Telegram-чате. Отвечай кратко и по делу.",
    )

    await status_msg.edit_text(
        f"{solution}\n\n📊 _Осталось энергии: {user_attempts[user_id]}_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def api_adsgram_reward(request):
    """
    Серверный webhook для подтверждения награды от AdsGram.
    Этот URL нужно указать в кабинете AdsGram в настройках рекламного блока
    как "Reward URL" (поле обязательно для блоков формата Bot) — AdsGram сам
    обратится сюда GET-запросом, когда пользователь честно завершил
    просмотр рекламы, и передаст его Telegram ID.

    Это единственная причина, по которой боту вообще нужен публичный
    HTTPS-адрес — весь остальной бот работает через polling и никакого
    домена не требует.
    """
    tgid_raw = request.query.get("tgid") or request.query.get("user_id")
    if not tgid_raw:
        return web.json_response({"status": "error", "reason": "missing tgid"}, status=400)

    try:
        user_id = int(tgid_raw)
    except ValueError:
        return web.json_response({"status": "error", "reason": "invalid tgid"}, status=400)

    user_attempts[user_id] = user_attempts.get(user_id, DEFAULT_ATTEMPTS) + REWARD_ATTEMPTS
    logging.info(f"AdsGram reward granted to user {user_id}: +{REWARD_ATTEMPTS} attempts")

    bot_app: Application = request.app["bot_app"]
    try:
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=f"🎉 Начислено +{REWARD_ATTEMPTS} ⚡ за просмотр рекламы! Всего: {user_attempts[user_id]}",
        )
    except Exception as e:
        # Не критично для самого начисления — пользователь просто не получит уведомление,
        # например если он ни разу не писал боту.
        logging.warning(f"Could not notify user {user_id} about reward: {e}")

    return web.json_response({"status": "ok", "attempts": user_attempts[user_id]})


async def main():
    if BOT_TOKEN.startswith("PUT_YOUR"):
        raise RuntimeError(
            "Не задан BOT_TOKEN. Установите переменную окружения BOT_TOKEN "
            "(и не используйте токен, который был засвечен ранее)."
        )

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_message))

    # Маленький HTTP-сервер только ради одного эндпоинта — подтверждения
    # награды AdsGram. Никакого фронтенда, никакого CORS — это не публичный
    # сайт, а просто webhook.
    server = web.Application()
    server["bot_app"] = bot_app
    server.router.add_get("/api/adsgram/reward", api_adsgram_reward)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    print(f"Бот запущен. Webhook для наград AdsGram слушает порт {PORT}...")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
