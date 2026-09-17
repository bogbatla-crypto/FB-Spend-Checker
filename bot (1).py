"""
Telegram-бот: проверка спенда рекламного объявления из Facebook Ad Library.

Как это работает:
1. Пользователь присылает боту ссылку на объявление из Facebook Ad Library
   (вида https://www.facebook.com/ads/library/?id=1234567890) или просто ID.
2. Бот открывает эту же публичную страницу через настоящий headless-браузер
   (Playwright + Chromium) — это нужно, потому что Facebook блокирует
   обычные HTTP-запросы без исполнения JavaScript антибот-проверкой.
3. Вытаскивает из отрендеренной страницы данные о показах и спенде —
   ровно те же данные, что видно в самом браузере для объявлений с охватом ЕС.
4. Отвечает пользователю числом.

Требуется переменная окружения TELEGRAM_BOT_TOKEN — токен от BotFather.
"""

import os
import re
import logging
import time

import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fbspendbot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise SystemExit("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

ID_RE = re.compile(r"(?:id=|library/)(\d{8,})")
PLAIN_ID_RE = re.compile(r"^\d{8,}$")

_playwright = None
_browser = None


def get_browser():
    """Запускаем один общий браузер на всё время жизни процесса (дёшево и быстро)."""
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    return _browser


def extract_ad_id(text: str):
    """Достаём числовой ID объявления из ссылки или из голого текста."""
    text = text.strip()
    m = ID_RE.search(text)
    if m:
        return m.group(1)
    if PLAIN_ID_RE.match(text):
        return text
    return None


def fetch_spend(ad_id: str):
    """
    Открывает публичную страницу объявления в headless-браузере и пытается
    вытащить показы/спенд из отрендеренного HTML.
    """
    browser = get_browser()
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = context.new_page()
    try:
        url = f"https://www.facebook.com/ads/library/?id={ad_id}&country=ALL"
        page.goto(url, timeout=30000, wait_until="networkidle")
        # Даём странице немного времени дорендерить боковую панель с цифрами
        page.wait_for_timeout(2000)
        html = page.content()
    finally:
        context.close()

    result = {
        "found": False,
        "shows": None,
        "spend_lower": None,
        "spend_upper": None,
        "spend_exact": None,
        "currency": None,
    }

    # Вариант 1: точные цифры (как в европейской карточке DSA) —
    # что-то вроде "eu_total_reach":3840 и "spend":{"amount":"46.08","currency":"USD"}
    m_reach = re.search(r'"eu_total_reach"\s*:\s*(\d+)', html)
    if m_reach:
        result["shows"] = int(m_reach.group(1))
        result["found"] = True

    m_spend_exact = re.search(
        r'"spend"\s*:\s*\{\s*"amount"\s*:\s*"?([\d.]+)"?\s*,\s*"currency"\s*:\s*"([A-Z]{3})"',
        html,
    )
    if m_spend_exact:
        result["spend_exact"] = m_spend_exact.group(1)
        result["currency"] = m_spend_exact.group(2)
        result["found"] = True

    # Вариант 2: диапазон (типично для political/issue объявлений)
    m_range = re.search(
        r'"spend"\s*:\s*\{\s*"lower_bound"\s*:\s*"?(\d+)"?\s*,\s*"upper_bound"\s*:\s*"?(\d+)"?',
        html,
    )
    if m_range:
        result["spend_lower"] = m_range.group(1)
        result["spend_upper"] = m_range.group(2)
        result["found"] = True

    m_impr_range = re.search(
        r'"impressions"\s*:\s*\{\s*"lower_bound"\s*:\s*"?(\d+)"?\s*,\s*"upper_bound"\s*:\s*"?(\d+)"?',
        html,
    )
    if m_impr_range:
        result["impr_lower"] = m_impr_range.group(1)
        result["impr_upper"] = m_impr_range.group(2)

    # Диагностика: если явно видим текст про то, что объявление не найдено
    if "isn't any ads" in html.lower() or "no ads" in html.lower():
        result["not_found_on_page"] = True

    return result


def format_reply(ad_id: str, data: dict) -> str:
    if not data["found"]:
        return (
            f"По объявлению {ad_id} не нашёл данных о спенде.\n"
            "Скорее всего, оно не показывалось на аудиторию ЕС — "
            "для остальных стран Meta эти цифры не раскрывает нигде."
        )

    lines = [f"Объявление {ad_id}:"]
    if data.get("shows") is not None:
        lines.append(f"Показы: {data['shows']:,}".replace(",", " "))
    elif data.get("impr_lower"):
        lines.append(f"Показы: {data['impr_lower']}–{data['impr_upper']}")

    if data.get("spend_exact") is not None:
        cur = data.get("currency") or ""
        lines.append(f"Спенд: {data['spend_exact']} {cur}".strip())
    elif data.get("spend_lower") is not None:
        lines.append(f"Спенд: {data['spend_lower']}–{data['spend_upper']}")

    return "\n".join(lines)


def send_message(chat_id: int, text: str):
    requests.post(
        f"{API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Привет! Пришли ссылку на объявление из Facebook Ad Library "
            "(или просто его ID), и я скажу текущий спенд, если он доступен "
            "(работает только для объявлений с показами на ЕС).",
        )
        return

    ad_id = extract_ad_id(text)
    if not ad_id:
        send_message(chat_id, "Не нашёл ID объявления в сообщении. Пришли ссылку из Ad Library или сам ID.")
        return

    try:
        data = fetch_spend(ad_id)
        reply = format_reply(ad_id, data)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка при обработке %s", ad_id)
        reply = (
            f"Не получилось обработать объявление {ad_id}: {e}\n"
            "Попробуй ещё раз через минуту, если не поможет — напиши мне."
        )

    send_message(chat_id, reply)


def main():
    log.info("Бот запущен, жду сообщений...")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=40)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в основном цикле, жду 5 секунд и пробую снова")
            time.sleep(5)


if __name__ == "__main__":
    main()
