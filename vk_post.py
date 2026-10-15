"""Публикация в сообщество ВКонтакте: 3 поста в сутки, лучшие из вышедших в Telegram-канале.

Нужны переменные окружения (секреты GitHub): VK_TOKEN (ключ доступа сообщества, права «Стена» и «Фотографии»),
VK_GROUP_ID (число из адреса сообщества). Без них модуль ничего не делает.
"""
import html
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.vk.com/method/"
VERSION = "5.199"
SLOTS_MSK = (9, 13, 20)  # утро, обед, вечер по Москве
EXTRA_SLOTS = {"2026-10-15": (21,)}  # разовый слот: пробный пост сегодня в 21:00 (только на эту дату)
SLOT_WINDOW_MIN = 40  # слот считается открытым первые 40 минут часа
SIGNATURE = "\n\n👉 Мы в Telegram: https://t.me/TheHottest_NewsEveryDay"

MSK = timezone(timedelta(hours=3))


def current_slot(now=None):
    """Ключ открытого слота («2026-10-15-13») или None."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    slots = SLOTS_MSK + EXTRA_SLOTS.get(now.strftime("%Y-%m-%d"), ())
    if now.hour in slots and now.minute < SLOT_WINDOW_MIN:
        return now.strftime("%Y-%m-%d-%H")
    return None


def channel_posts(channel, limit=12):
    """Последние посты нашего Telegram-канала: [(id, текст, URL фото или None)]."""
    page = requests.get(f"https://t.me/s/{channel.lstrip('@')}", timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"}).text
    out = []
    for block in page.split('data-post="')[1:]:
        post = block.split('"', 1)[0]
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not m:
            continue
        raw = re.sub(r"<br\s*/?>", "\n", m.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        p = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
        out.append((post, text, p.group(1) if p else None))
    return out[-limit:]


def to_vk_text(text):
    """Telegram-пост -> текст для ВК: без служебной подписи, заголовок заглавными."""
    lines = [l for l in text.split("\n")]
    cleaned = [l for l in lines if "Подписаться" not in l and "Не для слабонервных" not in l]
    body = "\n".join(cleaned).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    head, _, rest = body.partition("\n")
    head = head.replace("📰", "").strip()
    return (f"🔥 {head.upper()}" + (f"\n{rest}" if rest.strip() else "")).strip() + SIGNATURE


def pick_best(posts, is_hot):
    """Горячий с фото > любой с фото > горячий > самый свежий."""
    def score(p):
        _, text, img = p
        return (bool(is_hot(text)) + bool(img) * 2, 0)
    ranked = sorted(posts, key=score, reverse=True)
    best_score = score(ranked[0]) if ranked else None
    # среди равных берём самый свежий (он стоит позже в списке)
    for p in reversed(posts):
        if score(p) == best_score:
            return p
    return None


def _call(method, token, **params):
    params.update({"access_token": token, "v": VERSION})
    r = requests.post(API + method, data=params, timeout=60).json()
    if "error" in r:
        raise RuntimeError(f"VK {method}: {r['error'].get('error_msg')}")
    return r["response"]


def upload_photo(token, group_id, image_bytes):
    srv = _call("photos.getWallUploadServer", token, group_id=group_id)
    up = requests.post(srv["upload_url"], files={"photo": ("photo.jpg", io.BytesIO(image_bytes))}, timeout=60).json()
    saved = _call("photos.saveWallPhoto", token, group_id=group_id,
                  photo=up["photo"], server=up["server"], hash=up["hash"])[0]
    return f"photo{saved['owner_id']}_{saved['id']}"


def post(text, image_url=None):
    token, group_id = os.environ["VK_TOKEN"], os.environ["VK_GROUP_ID"].lstrip("-")
    attachments = ""
    if image_url:
        try:
            img = requests.get(image_url, timeout=30).content
            attachments = upload_photo(token, group_id, img)
        except Exception as e:
            print("! VK фото:", e, file=sys.stderr)
    r = _call("wall.post", token, owner_id=f"-{group_id}", from_group=1, message=text, attachments=attachments)
    return r["post_id"]


def run_slot(seen, is_hot, dry_run=False, force=False):
    """Публикует лучший пост в открытый слот; возвращает True, если опубликовал. seen: множество с отметками слотов."""
    if not (os.environ.get("VK_TOKEN") and os.environ.get("VK_GROUP_ID")) and not dry_run:
        return False
    slot = "forced" if force else current_slot()
    if not slot or f"__vk__={slot}" in seen:
        return False
    posts = channel_posts(os.environ.get("TELEGRAM_CHANNEL", "@TheHottest_NewsEveryDay"))
    best = pick_best(posts, is_hot)
    if not best:
        return False
    text = to_vk_text(best[1])
    if dry_run:
        print("[VK]", slot, "\n" + text, "\n[фото]" if best[2] else "\n[без фото]")
        return False
    post_id = post(text, best[2])
    seen.add(f"__vk__={slot}")
    print("опубликовано во ВК, post_id", post_id)
    return True
