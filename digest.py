"""Итоги дня: раз в сутки собирает главные новости нашего канала и присылает админу на подтверждение.

Каждая новость оформляется цитатным блоком Telegram: жирный заголовок, строка автора («— Лавров») и одно предложение сути.
"""
import html
import re
from datetime import datetime, timedelta, timezone

import requests

MSK = timezone(timedelta(hours=3))
DIGEST_HOUR = 21  # по Москве
WINDOW_MIN = 40  # окно после начала часа, когда бот ещё может прислать итоги
MAX_ITEMS = 6
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def current_digest_slot(now=None):
    """Ключ дня («2026-10-17»), если сейчас открыто окно публикации итогов, иначе None."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    if now.hour == DIGEST_HOUR and now.minute < WINDOW_MIN:
        return now.strftime("%Y-%m-%d")
    return None


def channel_items(channel, since):
    """Посты канала не старше since: [(время, текст, есть_медиа)]."""
    page = requests.get(f"https://t.me/s/{channel.lstrip('@')}", timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    out = []
    for block in page.split('data-post="')[1:]:
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        t = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not m or not t:
            continue
        dt = datetime.fromisoformat(t.group(1).replace("Z", "+00:00"))
        if dt < since:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1)))).strip()
        media = "tgme_widget_message_photo_wrap" in block or "tgme_widget_message_video" in block
        out.append((dt, text, media))
    return out


def parse_item(text):
    """Разбор поста канала: (заголовок, автор или None, суть одним предложением)."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    lines = [l for l in lines if "Подписаться" not in l and "Не для слабонервных" not in l]
    if not lines:
        return None
    head = lines[0].replace("📰", "").strip()
    author = next((l.lstrip("—– ").strip() for l in lines[1:3] if l.startswith(("—", "–"))), None)
    rest = " ".join(l for l in lines[1:] if not l.startswith(("—", "–")))
    norm = lambda s: re.sub(r"\W+", "", s).lower()
    detail = ""
    for s in re.split(r"(?<=[.!?])\s+", rest):
        if s and norm(s) not in norm(head) and norm(head) not in norm(s):
            detail = s if len(s) <= 220 else s[:220].rsplit(" ", 1)[0] + "…"
            break
    return head, author, detail


def build(posts, is_hot, now=None):
    """Текст итогов дня (HTML) или None, если новостей мало."""
    items = []
    for dt, text, media in posts:
        p = parse_item(text)
        if not p or len(p[0]) < 15:
            continue
        score = 2 * bool(is_hot(text)) + bool(media) + bool(p[2])
        items.append((score, dt, p))
    if len(items) < 3:
        return None
    words = lambda s: {w[:5] for w in re.findall(r"[а-яёa-z0-9]{4,}", s.lower())}  # по корням: «инфраструктуры» = «инфраструктуру»
    best = []
    for it in sorted(items, key=lambda x: (x[0], x[1]), reverse=True):
        w = words(it[2][0])
        # повтор той же новости из другого источника: пересечение заголовков по словам больше половины
        if any(len(w & words(b[2][0])) >= 0.5 * max(1, min(len(w), len(words(b[2][0])))) for b in best):
            continue
        best.append(it)
        if len(best) == MAX_ITEMS:
            break
    best.sort(key=lambda x: x[1])  # по времени в течение дня
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    parts = [f"<b>🗞 Итоги дня · {now.day} {MONTHS[now.month - 1]}</b>"]
    for _, _, (head, author, detail) in best:
        lines = [f"<b>{html.escape(head)}</b>"]
        if author:
            lines.append(f"— {html.escape(author)}")
        if detail:
            lines.append(html.escape(detail))
        parts.append("<blockquote>" + "\n".join(lines) + "</blockquote>")
    return "\n\n".join(parts)


def run(seen, is_hot, signature, channel, send_draft, dry_run=False, force=False):
    """Раз в сутки собирает итоги и присылает админу на подтверждение. Возвращает True, если отправлено."""
    slot = "forced" if force else current_digest_slot()
    if not slot or f"__digest__={slot}" in seen:
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    core = build(channel_items(channel, since), is_hot)
    if not core:
        return False
    core += signature
    if dry_run:
        print(core)
        return False
    send_draft(core, [], False)
    seen.add(f"__digest__={slot}")
    print("итоги дня отправлены админу на подтверждение")
    return True
