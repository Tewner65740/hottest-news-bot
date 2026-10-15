"""Ждёт нажатий кнопок в личке админа и публикует/отклоняет черновики.
Запуск: python listener.py   (должен работать постоянно)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import news_bot as nb

nb.load_env()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"


def api(method, **data):
    return requests.post(f"{API}/{method}", data=data, timeout=60).json()


INBOX = nb.BASE / "inbox"
SUGGEST_KB = lambda sid: json.dumps({"inline_keyboard": [
    [{"text": "✅ В канал", "callback_data": f"sub_ok:{sid}"},
     {"text": "🙈 Со спойлером", "callback_data": f"sub_sp:{sid}"}],
    [{"text": "❌ Отклонить", "callback_data": f"sub_no:{sid}"}],
]})


def post_media(chat_id, kind, file_id, caption, spoiler=False, markup=None, title="", image=None):
    """Отправка по file_id: kind = photo | video | text (текст: фото города или наша карточка)."""
    if kind == "text":
        path = image if image and os.path.exists(image) else None  # нет фото: просто текст
        nb.send(caption, path, spoiler, chat_id=chat_id if chat_id != "channel" else None,
                reply_markup=json.loads(markup) if markup else None)
        return
    data = {"chat_id": os.environ["TELEGRAM_CHANNEL"] if chat_id == "channel" else chat_id,
            kind: file_id, "caption": caption, "parse_mode": "HTML", "has_spoiler": spoiler,
            "disable_notification": chat_id == "channel" and os.environ.get("SILENT_MODE", "0") == "1"}
    if markup:
        data["reply_markup"] = markup
    r = api("sendPhoto" if kind == "photo" else "sendVideo", **data)
    if not r.get("ok"):
        raise RuntimeError(r)


awaiting = {}  # user_id -> sid: ждём от читателя название города

CITIES = {
    "москв": "Москва", "санкт-петербург": "Санкт-Петербург", "петербург": "Санкт-Петербург",
    "питер": "Санкт-Петербург", "новосибирск": "Новосибирск", "екатеринбург": "Екатеринбург",
    "казан": "Казань", "нижн\\w+ новгород": "Нижний Новгород", "челябинск": "Челябинск",
    "самар": "Самара", "омск": "Омск", "ростов": "Ростов-на-Дону", "уф[аеуы]": "Уфа",
    "красноярск": "Красноярск", "воронеж": "Воронеж", "перм": "Пермь", "волгоград": "Волгоград",
    "краснодар": "Краснодар", "саратов": "Саратов", "тюмен": "Тюмень", "тольятти": "Тольятти",
    "ижевск": "Ижевск", "барнаул": "Барнаул", "ульяновск": "Ульяновск", "иркутск": "Иркутск",
    "хабаровск": "Хабаровск", "владивосток": "Владивосток", "ярославл": "Ярославль",
    "махачкал": "Махачкала", "томск": "Томск", "оренбург": "Оренбург", "кемерово": "Кемерово",
    "новокузнецк": "Новокузнецк", "рязан": "Рязань", "астрахан": "Астрахань", "пенз": "Пенза",
    "липецк": "Липецк", "киров": "Киров", "чебоксар": "Чебоксары", "калининград": "Калининград",
    "тул[аеуы]": "Тула", "курск": "Курск", "севастопол": "Севастополь", "сочи": "Сочи",
    "ставрополь": "Ставрополь", "белгород": "Белгород", "брянск": "Брянск", "смоленск": "Смоленск",
    "мурманск": "Мурманск", "архангельск": "Архангельск", "донецк": "Донецк", "луганск": "Луганск",
    "мариупол": "Мариуполь", "симферопол": "Симферополь", "киев": "Киев", "харьков": "Харьков",
    "минск": "Минск", "одесс": "Одесса", "днепр": "Днепр",
}


STREET_RE = re.compile(
    r"(?i:ул\.?|улиц\w+|пр-т|проспект\w*|пр\.|шоссе|переул\w+|пер\.|бульвар\w*|набережн\w+|площад\w+)"
    r"\s+[А-ЯЁA-Z0-9][\w\-]*(?:\s+[А-ЯЁ][\w\-]*)?"
)


def find_city(text):
    for stem, name in CITIES.items():
        if re.search(r"(?<!\w)" + stem, text, re.I):
            return name
    return None


def wiki_city(query):
    """(название, URL фото) с русской Википедии: главное фото статьи о городе."""
    hdr = {"User-Agent": "TheHottestNewsBot/1.0 (news channel bot)"}
    try:
        s = requests.get("https://ru.wikipedia.org/w/api.php", timeout=20, headers=hdr, params={
            "action": "opensearch", "search": query, "limit": 1, "format": "json"}).json()
        title = s[1][0] if s[1] else query
        d = requests.get("https://ru.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title),
                         timeout=20, headers=hdr).json()
        img = (d.get("originalimage") or d.get("thumbnail") or {}).get("source")
        # заглавное фото статьи часто коллаж: ищем обычную фотографию города
        bad = re.compile(r"collage|коллаж|герб|флаг|flag|coat|map|карта|logo|locator|panorama_collage|\.svg", re.I)
        try:
            media = requests.get("https://ru.wikipedia.org/api/rest_v1/page/media-list/"
                                 + requests.utils.quote(d.get("title") or title), timeout=20, headers=hdr).json()
            for it in media.get("items", []):
                name = it.get("title", "")
                if it.get("type") == "image" and name.lower().endswith((".jpg", ".jpeg")) and not bad.search(name):
                    src = (it.get("srcset") or [{}])[-1].get("src")
                    if src:
                        img = "https:" + src if src.startswith("//") else src
                        break
        except Exception:
            pass
        return d.get("title") or title, img
    except Exception as e:
        print("! wiki:", e)
        return None, None


def download_city(url, sid):
    INBOX.mkdir(exist_ok=True)
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "TheHottestNewsBot/1.0 (news channel bot)"})
        r.raise_for_status()
        p = INBOX / f"{sid}.jpg"
        p.write_bytes(r.content)
        return str(p)
    except Exception as e:
        print("! city image:", e)
        return None


def build_caption(meta):
    import html as h
    lines = meta["text"].split("\n")
    caption = f"<b>{h.escape(lines[0])}</b>" if lines[0] else ""
    if meta.get("city"):
        place = meta["city"] + (f", {meta['street']}" if meta.get("street") else "")
        caption += f"\n\n📍 {h.escape(place)}"
    if len(lines) > 1 and "\n".join(lines[1:]).strip():
        caption += "\n\n" + h.escape("\n".join(lines[1:]).strip())
    caption += nb.SIGNATURE
    if meta.get("graphic"):
        caption = "⚠️ <b>Не для слабонервных</b>\n\n" + caption
    return caption[:1024]


def handle_message(m):
    """Сообщение от читателя: сохраняем и показываем админу с кнопками."""
    if m["chat"]["type"] != "private" or m["from"]["id"] == ADMIN:
        return
    text = (m.get("caption") or m.get("text") or "").strip()
    uid = m["from"]["id"]
    if uid in awaiting and text and not text.startswith("/") and not m.get("photo") and not m.get("video"):
        sid, stage = awaiting.pop(uid)
        f = INBOX / f"{sid}.json"
        if not f.exists():
            return
        meta = json.loads(f.read_text("utf-8"))
        if stage == "city":
            name, img = wiki_city(text)
            meta["city"] = name or text.strip()[:40]
            if img and meta["kind"] == "text":
                meta["image"] = download_city(img, sid)
            if not meta.get("street"):
                f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                awaiting[uid] = (sid, "street")
                api("sendMessage", chat_id=m["chat"]["id"],
                    text="На какой улице или рядом с каким местом? Если не знаете, напишите «не знаю».")
                return
        else:
            if not re.match(r"(?i)\s*(не знаю|нет|-|пропустить)", text):
                meta["street"] = text.strip()[:80]
        f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        deliver_to_admin(sid, meta)
        api("sendMessage", chat_id=m["chat"]["id"], text="Спасибо! Отправили на модерацию.")
        return
    if text.startswith("/start"):
        api("sendMessage", chat_id=m["chat"]["id"],
            text="Привет! Пришлите новость, фото или видео, и мы рассмотрим публикацию в канале. "
                 "По вопросам рекламы пишите @Vovandr22")
        return
    if m.get("photo"):
        kind, fid = "photo", m["photo"][-1]["file_id"]
    elif m.get("video"):
        kind, fid = "video", m["video"]["file_id"]
    elif text:
        kind, fid = "text", ""
    else:
        return
    if nb.is_ad(text):
        api("sendMessage", chat_id=m["chat"]["id"], text="Похоже, это реклама. По вопросам рекламы в канале пишите @Vovandr22")
        return
    import uuid
    sid = uuid.uuid4().hex[:10]
    INBOX.mkdir(exist_ok=True)
    clean_text = nb.strip_branding(text)
    author = m["from"].get("username") or m["from"].get("first_name", "")
    meta = {"kind": kind, "file_id": fid, "text": clean_text, "author": author,
            "graphic": nb.is_graphic(text), "city": None, "image": None}
    city = find_city(text)
    if city:
        meta["city"] = city
        name, img = wiki_city(city)
        if img and kind == "text":
            meta["image"] = download_city(img, sid)
    (INBOX / f"{sid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    street = STREET_RE.search(text)
    if street:
        meta["street"] = street.group(0).strip()
    (INBOX / f"{sid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if not city:
        awaiting[uid] = (sid, "city")
        api("sendMessage", chat_id=m["chat"]["id"],
            text="В каком городе это произошло? Напишите название города одним сообщением.")
        return
    if not street:
        awaiting[uid] = (sid, "street")
        api("sendMessage", chat_id=m["chat"]["id"],
            text="На какой улице или рядом с каким местом? Если не знаете, напишите «не знаю».")
        return
    deliver_to_admin(sid, meta)
    api("sendMessage", chat_id=m["chat"]["id"], text="Спасибо! Отправили на модерацию.")


POLISH_WAIT = 15 * 60  # сколько ждём правки текста, потом уходит как есть


def deliver_to_admin(sid, meta):
    """С POLISH=1 предложка ждёт правки Claude (deliver.py); иначе сразу идёт админу."""
    if os.environ.get("POLISH", "1") == "1" and not meta.get("polished"):
        meta["status"] = "await_polish"
        meta["ts"] = time.time()
        (INBOX / f"{sid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return
    send_to_admin(sid, meta)


def flush_stale():
    """Если правка не пришла за POLISH_WAIT, отправляем админу исходный текст."""
    for f in INBOX.glob("*.json") if INBOX.exists() else []:
        try:
            meta = json.loads(f.read_text("utf-8"))
            if meta.get("status") == "await_polish" and time.time() - meta.get("ts", 0) > POLISH_WAIT:
                meta["status"] = "sent"
                f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                send_to_admin(f.stem, meta)
        except Exception as e:
            print("!", e)


def send_to_admin(sid, meta):
    try:
        post_media(ADMIN, meta["kind"], meta["file_id"],
                   f"📩 От читателя {meta['author']}\n\n" + build_caption(meta),
                   markup=SUGGEST_KB(sid), title=meta["text"].split("\n")[0], image=meta.get("image"))
    except Exception as e:
        print("!", e)


def handle_suggestion(cb, action, sid):
    f = INBOX / f"{sid}.json"
    if not f.exists():
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано")
        return
    meta = json.loads(f.read_text("utf-8"))
    status = "❌ Отклонено"
    if action in ("sub_ok", "sub_sp"):
        if action == "sub_sp":
            meta["graphic"] = True
        post_media("channel", meta["kind"], meta["file_id"], build_caption(meta), meta["graphic"],
                   title=meta["text"].split("\n")[0], image=meta.get("image"))
        status = "✅ Опубликовано"
    f.unlink()
    if meta.get("image"):
        Path(meta["image"]).unlink(missing_ok=True)
    api("answerCallbackQuery", callback_query_id=cb["id"], text=status)
    api("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"], message_id=cb["message"]["message_id"],
        reply_markup=json.dumps({"inline_keyboard": [[{"text": status, "callback_data": "done"}]]}))


def handle(cb):
    if cb["from"]["id"] != ADMIN:
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Нет доступа")
        return
    action, _, pid = cb["data"].partition(":")
    if action.startswith("sub_"):
        handle_suggestion(cb, action, pid)
        return
    meta_file = nb.PENDING / f"{pid}.json"
    msg = cb["message"]
    if not meta_file.exists():
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано")
        return
    meta = json.loads(meta_file.read_text("utf-8"))
    if action == "ok":
        nb.send(meta["caption"], nb.PENDING / meta["media"], meta["spoiler"])
        status = "✅ Опубликовано"
    else:
        status = "❌ Пропущено"
    meta_file.unlink()
    (nb.PENDING / meta["media"]).unlink(missing_ok=True)
    api("answerCallbackQuery", callback_query_id=cb["id"], text=status)
    api(
        "editMessageReplyMarkup",
        chat_id=msg["chat"]["id"],
        message_id=msg["message_id"],
        reply_markup=json.dumps({"inline_keyboard": [[{"text": status, "callback_data": "done"}]]}),
    )


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    offset = None
    print("Слушаю кнопки...")
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["callback_query", "message"])},
                timeout=45,
            ).json()
        except Exception as e:
            print("!", e)
            time.sleep(5)
            continue
        flush_stale()
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            if "message" in u:
                try:
                    handle_message(u["message"])
                except Exception as e:
                    print("!", e)
            if "callback_query" in u:
                try:
                    handle(u["callback_query"])
                except Exception as e:
                    print("!", e)


if __name__ == "__main__":
    main()
