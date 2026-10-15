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
# разовый тест обходов загрузки фото: публикуется один раз сразу после этого времени (потом ключ test1 занят)
TEST_AFTER_UTC = datetime(2026, 10, 15, 18, 30, tzinfo=timezone.utc)
TEST2_AFTER_UTC = datetime(2026, 10, 15, 20, 55, tzinfo=timezone.utc)  # проба после включения сообщений
SLOT_WINDOW_MIN = 40  # слот считается открытым первые 40 минут часа
SIGNATURE = "\n\n👉 Мы в Telegram: https://t.me/TheHottest_NewsEveryDay"

MSK = timezone(timedelta(hours=3))


def current_slot(now=None):
    """Ключ открытого слота («2026-10-15-13») или None."""
    now = now or datetime.now(timezone.utc)
    if now >= TEST_AFTER_UTC and now < TEST_AFTER_UTC + timedelta(hours=2):  # разовый пробный слот (уже использован)
        return "test1"
    if now >= TEST2_AFTER_UTC and now < TEST2_AFTER_UTC + timedelta(hours=3):  # проба загрузки фото (только с фото)
        return "test2"
    now = now.astimezone(MSK)
    slots = SLOTS_MSK + EXTRA_SLOTS.get(now.strftime("%Y-%m-%d"), ())
    if now.hour in slots and now.minute < SLOT_WINDOW_MIN:
        return now.strftime("%Y-%m-%d-%H")
    return None


def channel_posts(channel, limit=20):
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


def _upload_to(upload_url, image_bytes):
    return requests.post(upload_url, files={"photo": ("photo.jpg", io.BytesIO(image_bytes))}, timeout=60).json()


def upload_via_messages(token, image_bytes):
    """Обход: загрузка как для сообщений (доступна ключу сообщества)."""
    srv = _call("photos.getMessagesUploadServer", token)
    up = _upload_to(srv["upload_url"], image_bytes)
    saved = _call("photos.saveMessagesPhoto", token, photo=up["photo"], server=up["server"], hash=up["hash"])[0]
    return f"photo{saved['owner_id']}_{saved['id']}"


def upload_via_album(token, group_id, image_bytes):
    """Обход: загрузка в альбом сообщества (альбом создаётся один раз и ищется по названию)."""
    title = "Новости"
    albums = _call("photos.getAlbums", token, owner_id=f"-{group_id}")["items"]
    album = next((a for a in albums if a["title"] == title), None)
    album_id = album["id"] if album else _call("photos.createAlbum", token, group_id=group_id, title=title)["id"]
    srv = _call("photos.getUploadServer", token, group_id=group_id, album_id=album_id)
    up = requests.post(srv["upload_url"], files={"file1": ("photo.jpg", io.BytesIO(image_bytes))}, timeout=60).json()
    saved = _call("photos.save", token, group_id=group_id, album_id=album_id, server=up["server"],
                  photos_list=up["photos_list"], hash=up["hash"])[0]
    return f"photo{saved['owner_id']}_{saved['id']}"


def post(text, image_url=None, link=None, photo_only=False):
    token, group_id = os.environ["VK_TOKEN"], os.environ["VK_GROUP_ID"].lstrip("-")
    owner = f"-{group_id}"
    if image_url:
        try:
            img = requests.get(image_url, timeout=30).content
        except Exception as e:
            print("! VK скачивание фото:", e, file=sys.stderr)
            img = None
        if img:
            user_token = os.environ.get("VK_USER_TOKEN")
            attempts = []
            if user_token:  # фото на стену обычно грузит только токен пользователя-админа
                attempts.append(("стена, токен пользователя", lambda: upload_photo(user_token, group_id, img)))
            attempts += [
                ("стена, ключ сообщества", lambda: upload_photo(token, group_id, img)),
                ("через сообщения", lambda: upload_via_messages(token, img)),
                ("через альбом сообщества", lambda: upload_via_album(token, group_id, img)),
            ]
            for name, fn in attempts:
                try:
                    photo = fn()
                except Exception as e:
                    print(f"! VK фото [{name}] загрузка: {e}", file=sys.stderr)
                    continue
                try:
                    r = _call("wall.post", token, owner_id=owner, from_group=1, message=text, attachments=photo)
                    print(f"VK фото [{name}] сработало, вложение {photo}")
                    return r["post_id"]
                except Exception as e:
                    print(f"! VK фото [{name}] публикация с вложением: {e}", file=sys.stderr)
    # не удалось приложить фото: прикрепляем ссылку на пост в Telegram, ВК покажет карточку с картинкой
    if photo_only:  # проба: без приложенного фото ничего не публикуем
        raise RuntimeError("фото ни одним способом не приложилось, пост не публикуется (проба)")
    # ссылка нужна только у постов с фото: у текстовой записи нет картинки для превью, и ВК такую ссылку отклоняет;
    # она же отклоняется, если картинка слишком мелкая (link_photo_sizing_rule): тогда публикуем просто текстом
    if image_url and link:
        try:
            r = _call("wall.post", token, owner_id=owner, from_group=1, message=text, attachments=link)
            return r["post_id"]
        except Exception as e:
            print(f"! VK карточка-ссылка отклонена, публикую текстом: {e}", file=sys.stderr)
    r = _call("wall.post", token, owner_id=owner, from_group=1, message=text, attachments="")
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
    if slot == "test2":  # для пробы нужен пост с фото
        with_photo = [p for p in posts if p[2]]
        if not with_photo:
            return False
        best = with_photo[-1]
        text = to_vk_text(best[1])
    post_id = post(text, best[2], link=f"https://t.me/{best[0]}", photo_only=(slot == "test2"))
    seen.add(f"__vk__={slot}")
    print("опубликовано во ВК, post_id", post_id)
    return True
