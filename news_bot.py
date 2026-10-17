"""Автопостинг новостей в Telegram-канал.

RSS -> фильтр дублей -> пересказ через Claude (если задан ANTHROPIC_API_KEY) -> публикация.
Запуск: python news_bot.py [--dry-run] [--limit N]
"""
import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

from card import news_card

BASE = Path(__file__).parent
SEEN_FILE = BASE / "seen.json"

FEEDS = [
    "https://lenta.ru/rss/news",
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://tass.ru/rss/v2.xml",
    "https://www.interfax.ru/rss.asp",
]

TG_CHANNELS = ["novosti_efir", "rhymestg", "ostorozhno_novosti", "moscowach", "rian_ru", "bbbreaking", "Dyadyabatya0"]
IMAGE_TG_OK = {"Dyadyabatya0", "bbbreaking"}  # каналы, откуда берём фото и видео (владелец: водяных знаков нет)

# Фото берём только у источников, чьи изображения проверены на отсутствие водяных знаков.
# Остальные (ТАСС, РИА, Интерфакс, чужие Telegram-каналы) заменяет наша карточка.
IMAGE_OK = "icdn.lenta.ru"

SIGNATURE = '\n\n👉 <a href="https://t.me/TheHottest_NewsEveryDay">The Hottest News. Подписаться</a>'

MODEL = "claude-haiku-5-5"
PROMPT = (
    "Перепиши новость для Telegram-канала своими словами: 2-3 коротких предложения, "
    "нейтральный тон, только факты из текста, без выдумок. Не упоминай названия "
    "каналов, @имена, призывы подписаться и рекламу. Первой строкой дай "
    "короткий заголовок (до 80 символов) с одним подходящим эмодзи в начале. "
    "Затем пустая строка и текст. Без хэштегов и ссылок.\n\n"
    "Заголовок: {title}\nТекст: {summary}"
)


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


BRAND_RE = re.compile(
    r"@\w+|t\.me/\S*|https?://\S+|подпис|присылай|прислать|предложить новост|"
    r"реклам|на\s+связи|"
    + "|".join(TG_CHANNELS) + r"|эфир|рифмы|осторожно|москвач|риа новости|дядя бат|батя",
    re.I,
)


AD_RE = re.compile(
    r"реклам|erid|промокод|промо-код|скидк|акци[яи]\b|купи(ть|те)?\b|розыгрыш|"
    r"конкурс|выигра|партн[её]рск|спонсор|ООО\s|ИП\s|ИНН|казино|букмекер|ставк[аиу] на|"
    r"заработ(ок|ай|ать)|подработк|инвестици|курс(ы|ов)? по\b|вакансия|"
    r"переходи|жми|жмите|регистрируйся|бонус",
    re.I,
)


# темы, которые не публикуем (решение владельца канала)
SKIP_RE = re.compile(
    r"ракетн\w+ опасност|беспилотн\w+ опасност|опасност\w* (?:БПЛА|беспилот)|сирен|воздушн\w+ тревог|"
    r"\bотбой\b|режим[а-я]* [«\"]?(?:ракетн|беспилотн)",
    re.I,
)


def is_ad(*texts):
    """Реклама или запрещённая тема: такие посты пропускаем."""
    return any(AD_RE.search(t or "") or SKIP_RE.search(t or "") for t in texts)


PLATFORMS_RE = re.compile(r"twitch|youtube|ютуб|kick\b|tiktok|тикток|instagram|инстаграм|вконтакте|rutube|рутуб|дзен", re.I)
PROMO_LINE_RE = re.compile(
    r"обсуждаем|на\s+стриме|смотрите\s+(?:на|в)\b|стрим\s|подписывайт|ставьте\s+лайк|жмите|"
    r"наш\s+(?:канал|чат|бот)|присоединяйтесь|мы\s+в\s+(?:telegram|телеграм|max|вк)",
    re.I,
)


def is_promo_line(line):
    """Рекламная строка канала-источника: призыв на стрим/в соцсети или перечень платформ."""
    if PROMO_LINE_RE.search(line):
        return True
    return len(PLATFORMS_RE.findall(line)) >= 2  # «TWITCH | YouTube | KICK | TikTok»


def strip_branding(text):
    """Убирает строки с подписями, рекламой и упоминаниями чужих каналов."""
    return "\n".join(
        l for l in text.split("\n") if not BRAND_RE.search(l) and not is_promo_line(l)
    ).strip()


FIRST_PERSON_RE = re.compile(r"^\W*(?:я|мы|мне|мой|моя|мо[ёе]|наш\w*|меня|нас)\b", re.I)
SPEAKER_RE = re.compile(
    r"([А-ЯЁ][\w\-]+)\s(?:заявил[аи]?|сказал[аи]?|подчеркнул[аи]?|отметил[аи]?|"
    r"рассказал[аи]?|признал[аи]?|пояснил[аи]?|добавил[аи]?)"
)


ATTRIB_RE = re.compile(
    r"^(?P<head>.{15,}?)[,—–-]\s*(?P<verb>заявил[аи]?|сообщил[аи]?|отметил[аи]?|подчеркнул[аи]?|"
    r"рассказал[аи]?|пояснил[аи]?|сказал[аи]?|признал[аи]?|предупредил[аи]?|уточнил[аи]?|"
    r"считает|полагает|добавил[аи]?|напомнил[аи]?|пишет|сообщает|передаёт|передает)\s+"
    r"(?P<who>[^.!?]{3,120}?)\s*[.!?]?$",
    re.I,
)


def split_attribution(title):
    """«Цена выросла, заявил вице-премьер Александр Новак» -> («Цена выросла», «Заявил вице-премьер Александр Новак»).
    Автор слов выносится в отдельную строку, чтобы сразу было видно, чьи это слова."""
    m = ATTRIB_RE.match(title.strip())
    if not m:
        return title, None
    who = m.group("who").strip()
    # «в интервью ТАСС Александр Шохин» / «на пресс-конференции глава МИД Лавров» -> оставляем только того, кто сказал
    if re.match(r"(?i)(?:в|на|по|для|о|об|при)\s", who):
        tail = re.search(r"((?:[а-яё\-]+\s){0,2}[А-ЯЁ][а-яё\-]+(?:\s[А-ЯЁ][а-яё\-]+){0,2})$", who)
        if tail:
            who = tail.group(1).strip()
            # «глава МИД Сергей Лавров» -> оставляем должность и имя, но отбрасываем предлог/вводное слово в начале
            who = re.sub(r"(?i)^(?:в|на|по|для|о|об|при|интервью|беседе|разговоре)\s+", "", who)
    line = who[:1].upper() + who[1:]  # «Александр Новак», «вице-премьер Александр Новак»: подпись «— автор» под заголовком
    head = m.group("head").strip().rstrip(",:;—–-")
    return head, line


QUOTE_RE = re.compile(r"«[^»]{25,}»")


def fmt_paragraph(p):
    """Прямая речь («цитата ≥25 знаков») оформляется цитатой Telegram, остальное обычным абзацем."""
    esc = html.escape(p)
    return f"<blockquote>{esc}</blockquote>" if QUOTE_RE.search(p) else esc


def attribute_quote(title, summary):
    """Заголовок от первого лица без автора вводит в заблуждение: ставим имя говорящего.
    Возвращает новый заголовок или None, если автора найти не удалось."""
    if not FIRST_PERSON_RE.search(title):
        return title
    m = SPEAKER_RE.search(summary or "")
    if not m:
        return None
    speaker = m.group(1).strip()
    return f"{speaker}: «{title.rstrip('.')}»"


def is_placeholder(path):
    """Заглушка с логотипом сайта: почти целиком чёрная или однотонная картинка."""
    try:
        from PIL import Image

        im = Image.open(path).convert("L").resize((64, 64))
        px = list(im.tobytes())
        dark = sum(p < 20 for p in px) / len(px)
        light = sum(p > 235 for p in px) / len(px)
        return dark > 0.6 or light > 0.85
    except Exception:
        return False


MAX_VIDEO_MB = 45  # лимит Telegram на загрузку файла ботом 50 МБ
MAX_ALBUM_MB = 150  # общий лимит на все видео одного поста


def download_all(urls):
    """Скачивает все видео поста (до 10); слишком большие и недоступные пропускаются."""
    paths, total = [], 0
    for i, u in enumerate(urls[:10]):
        p = download(u, BASE / f"video_{i}.mp4")
        if not p:
            continue
        total += p.stat().st_size
        if total > MAX_ALBUM_MB * 1024 * 1024:
            break
        paths.append(p)
    return paths


def download(url, path):
    if "lenta_og" in url or "/assets/" in url:  # служебная картинка сайта, не фото новости
        return None
    is_video = ".mp4" in url.split("?")[0].lower()
    if is_video:
        path = path.with_suffix(".mp4")
    try:
        r = requests.get(url, timeout=120 if is_video else 30, stream=is_video)
        r.raise_for_status()
        if is_video:
            if int(r.headers.get("content-length", 0)) > MAX_VIDEO_MB * 1024 * 1024:
                return None
            data = r.content
            if len(data) > MAX_VIDEO_MB * 1024 * 1024:
                return None
            path.write_bytes(data)
            return path
        path.write_bytes(r.content)
        if is_placeholder(path):
            return None
        return path
    except Exception:
        return None


def tg_posts(channel):
    """Последние посты публичного канала через веб-превью t.me/s/."""
    page = requests.get(f"https://t.me/s/{channel}", timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    out = []
    for block in page.split('data-post="')[1:]:
        post = block.split('"', 1)[0]
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not m:
            continue
        raw = clean(re.sub(r"<br\s*/?>", "\n", m.group(1)))
        if is_ad(raw):
            continue
        text = re.sub(r"[\s📲👉…]+$", "", strip_branding(raw))
        if len(text) <= 40:
            continue
        img = None
        if channel in IMAGE_TG_OK:  # фото и видео берём только у каналов без водяных знаков
            vids = list(dict.fromkeys(re.findall(r'<video[^>]+src="([^"]+)"', block)))  # все видео альбома
            p = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
            if vids:  # видео важнее фото; несколько видео отдаём списком
                img = vids if len(vids) > 1 else vids[0]
            else:
                img = p.group(1) if p else None
        title = re.split(r"(?<=[.!?])\s+", text.split("\n")[0])[0]
        if len(title) > 260:  # не режем посреди слова
            title = title[:260].rsplit(" ", 1)[0]
        out.append((f"https://t.me/{post}", title, text, img))
    return out[-10:]


BOT_WALL_RE = re.compile(
    r"not a bot|captcha|access denied|are you a robot|verify you are human|cloudflare|"
    r"support team|enable javascript|checking your browser|ddos|включите javascript|подтвердите, что вы не робот",
    re.I,
)


def purge_junk():
    """Удаляет из канала посты с мусором (страницы защиты от ботов), если такие просочились."""
    ch = os.environ["TELEGRAM_CHANNEL"].lstrip("@")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        page = requests.get(f"https://t.me/s/{ch}", timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception as e:
        print("! purge:", e, file=sys.stderr)
        return
    for block in page.split('data-post="')[1:]:
        post = block.split('"', 1)[0]
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if m and BOT_WALL_RE.search(clean(m.group(1))):
            r = requests.post(f"https://api.telegram.org/bot{token}/deleteMessage",
                              data={"chat_id": os.environ["TELEGRAM_CHANNEL"], "message_id": post.rsplit("/", 1)[1]},
                              timeout=30)
            print("удалён мусорный пост", post, r.status_code, r.text[:80])


def is_russian(text):
    """Нормальный русский текст: кириллицы заметно больше, чем латиницы."""
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return cyr > 20 and cyr > lat * 2


def article_text(url, limit=1500):
    """Основной текст статьи: абзацы со страницы, без меню и подписей."""
    try:
        page = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception:
        return ""
    if BOT_WALL_RE.search(page[:5000]) and len(page) < 20000:  # страница защиты от ботов вместо статьи
        return ""
    paras = [clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S)]
    paras = [p for p in paras if len(p) > 60 and not is_ad(p) and is_russian(p) and not BOT_WALL_RE.search(p)]
    return " ".join(paras)[:limit]


def extract(text, max_chars=450):
    """Первые предложения статьи (там самая важная информация) до max_chars."""
    media = re.compile(r"ленте\.ру|лента\.ру|газет|telegram|телеграм|риа|тасс|интерфакс|изве?сти|коммерсант|рбк|\.ru\b", re.I)
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        if s.count("«") != s.count("»") or media.search(s):
            continue
        if out and len(out) + len(s) > max_chars:
            break
        out += (" " if out else "") + s
    return out


def rewrite(title, summary):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return f"📰 {title}\n{extract(summary)}".strip()
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": MODEL,
            "max_tokens": 400,
            "messages": [{"role": "user", "content": PROMPT.format(title=title, summary=summary[:1500])}],
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


# «Не для слабонервных»: только явная жесть, 18+ и кровь. Слова ищутся с начала слова (\b), а не внутри других слов
# (раньше «ранен» срабатывало на «сохранении»). Общие слова вроде «взрыв», «пожар», «погиб» сюда не входят.
GRAPHIC_RE = re.compile(
    r"\b(?:труп\w*|тел[ао]\s+погибш\w*|обезглавл\w*|расчленен\w*|расстрелян\w*|казн[ьи]\b|изнасил\w*|"
    r"кровав\w*|кров[ьи]\b|вскрыт\w*\s+вен\w*|порно\w*|интим\w*\s+(?:фото|видео)|обнажённ\w*|обнажен\w*|"
    r"голая|голый|голые|18\+|пытк\w*|самоубийств\w*|расчлен\w*)",
    re.I,
)


HOT_RE = re.compile(
    r"срочно|молни|теракт|взрыв|землетряс|эвакуац|обрушени|покушени|"
    r"захват заложн|погибл[иоа]? \d+|чрезвычайн|введ[её]н режим|задержан[ао]? (?:министр|губернатор|замминистр)|"
    r"отставк|объявил[аи]? (?:мобилизацию|войну)|крушени|падени[ею] самол",
    re.I,
)
MIN_GAP = 45 * 60  # обычные посты: примерно 1 в 45–60 минут, около 24 в сутки как у каналов-образцов


FAST_CHANNELS = ("bbbreaking",)  # берём все новости оттуда сразу, там они выходят быстрее всех


def is_fast(link):
    return any(link.startswith(f"https://t.me/{c}/") for c in FAST_CHANNELS)


def is_hot(*texts):
    return any(HOT_RE.search(t or "") for t in texts)


def is_graphic(*texts):
    return any(GRAPHIC_RE.search(t or "") for t in texts)


def send(caption, media_path, spoiler=False, chat_id=None, reply_markup=None, hot=False):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    if media_path is None:  # пост без картинки: обычное текстовое сообщение
        data = {
            "chat_id": chat_id or os.environ["TELEGRAM_CHANNEL"],
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": os.environ.get("SILENT_MODE", "0") == "1" and not hot and not chat_id,
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=60).raise_for_status()
        return
    is_video = str(media_path).lower().endswith((".mp4", ".mov"))
    method, field = ("sendVideo", "video") if is_video else ("sendPhoto", "photo")
    data = {
        "has_spoiler": spoiler,
        "chat_id": chat_id or os.environ["TELEGRAM_CHANNEL"],
        "caption": caption,
        "parse_mode": "HTML",
        # все посты со звуком; чтобы сделать обычные посты тихими, поставьте SILENT_MODE=1
        "disable_notification": os.environ.get("SILENT_MODE", "0") == "1" and not hot and not chat_id,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    with open(media_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", data=data, files={field: f}, timeout=120
        )
    r.raise_for_status()


PENDING = BASE / "pending"
WARN = "⚠️ <b>Не для слабонервных</b>\n\n"


def compose_caption(core, graphic):
    """Подпись поста: при необходимости с пометкой «Не для слабонервных» сверху."""
    return (WARN if graphic else "") + core


def safe_cut(text, limit):
    """Обрезка по границе абзаца, чтобы не оставить незакрытый тег (<blockquote>)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n\n" in cut:
        cut = cut.rsplit("\n\n", 1)[0]
    else:
        cut = cut.rsplit(" ", 1)[0]
    if cut.count("<blockquote>") != cut.count("</blockquote>"):
        cut = cut.rsplit("<blockquote>", 1)[0].rstrip()
    return cut


def send_media_group(paths, caption=None, spoiler=False, chat_id=None, hot=False):
    """Альбом из нескольких видео/фото (до 10); подпись ставится на первый элемент."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    media, files, handles = [], {}, []
    try:
        for i, p in enumerate(paths[:10]):
            video = str(p).lower().endswith((".mp4", ".mov"))
            item = {"type": "video" if video else "photo", "media": f"attach://f{i}", "has_spoiler": bool(spoiler)}
            if video:
                item["supports_streaming"] = True
            if i == 0 and caption:
                item["caption"], item["parse_mode"] = caption, "HTML"
            media.append(item)
            fh = open(p, "rb")
            handles.append(fh)
            files[f"f{i}"] = fh
        data = {
            "chat_id": chat_id or os.environ["TELEGRAM_CHANNEL"],
            "media": json.dumps(media),
            "disable_notification": os.environ.get("SILENT_MODE", "0") == "1" and not hot and not chat_id,
        }
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMediaGroup", data=data, files=files, timeout=600)
        r.raise_for_status()
    finally:
        for fh in handles:
            fh.close()


def draft_keyboard(pid, n, selected, graphic):
    """Кнопки черновика: выбор видео (если их несколько), публикация, пропуск и пометка «Не для слабонервных»."""
    rows = []
    if n > 1:
        btns = [
            {"text": ("✅ " if i in selected else "⬜ ") + f"Видео {i + 1}", "callback_data": f"tg:{pid}:{i}"}
            for i in range(n)
        ]
        rows += [btns[i:i + 3] for i in range(0, n, 3)]
        rows.append([{"text": "Выбрать все", "callback_data": f"all:{pid}"},
                     {"text": "Снять все", "callback_data": f"none:{pid}"}])
    label = "✅ Опубликовать" + (f" ({len(selected)} из {n})" if n > 1 else "")
    rows.append([{"text": label, "callback_data": f"ok:{pid}"},
                 {"text": "❌ Пропустить", "callback_data": f"no:{pid}"}])
    rows.append([{"text": "⚠️ Пометку убрать" if graphic else "⚠️ Добавить пометку «Не для слабонервных»",
                  "callback_data": f"wm:{pid}"}])
    return {"inline_keyboard": rows}


def send_draft(core, media_paths, graphic):
    """Черновик админу в личку: один клип или несколько. Публикует listener после нажатия кнопок."""
    import uuid

    if isinstance(media_paths, (str, Path)):
        media_paths = [media_paths]
    PENDING.mkdir(exist_ok=True)
    pid = uuid.uuid4().hex[:10]
    names = []
    for i, p in enumerate(media_paths):
        dst = PENDING / f"{pid}_{i}{Path(p).suffix}"
        dst.write_bytes(Path(p).read_bytes())
        names.append(dst.name)
    n = len(names)
    selected = list(range(n))
    meta = {"core": core, "media": names, "graphic": bool(graphic), "selected": selected}
    (PENDING / f"{pid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    admin = os.environ["ADMIN_CHAT_ID"]
    kb = draft_keyboard(pid, n, selected, graphic)
    caption = compose_caption(core, graphic)
    if n == 1:
        send(caption, PENDING / names[0], graphic, chat_id=admin, reply_markup=kb)
    else:
        send_media_group([PENDING / x for x in names], None, graphic, chat_id=admin)  # предпросмотр: все видео подряд
        send(caption + f"\n\n🎞 Видео в посте: {n}. Выберите нужные и нажмите «Опубликовать».", None,
             chat_id=admin, reply_markup=kb)


DESCRIPTION = "🔥 Самые горячие новости каждый день: коротко, по делу и без воды.\n\nПрислать новость: @Your_Best_Lucky_Friend_bot\nПо вопросам рекламы: @Vovandr22"


def set_description():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/setChatDescription",
        data={"chat_id": os.environ["TELEGRAM_CHANNEL"], "description": DESCRIPTION},
        timeout=30,
    )
    print(r.status_code, r.text)


def set_avatar(image_path):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    with open(image_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/setChatPhoto",
            data={"chat_id": os.environ["TELEGRAM_CHANNEL"]},
            files={"photo": f},
            timeout=60,
        )
    print(r.status_code, r.text)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap =argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="не публиковать, только показать")
    ap.add_argument("--set-avatar", action="store_true", help="установить avatar.png как фото канала")
    ap.add_argument("--set-description", action="store_true", help="установить описание канала")
    ap.add_argument("--vk-preview", action="store_true", help="показать, какой пост ушёл бы во ВК (без публикации)")
    ap.add_argument("--limit", type=int, default=5, help="максимум постов за запуск")
    args = ap.parse_args()

    load_env()
    if args.vk_preview:
        import vk_post

        vk_post.run_slot(set(), is_hot, dry_run=True, force=True)
        return
    if args.set_description:
        set_description()
        return
    if args.set_avatar:
        set_avatar(BASE / "avatar.png")
        return
    seen = set(json.loads(SEEN_FILE.read_text("utf-8"))) if SEEN_FILE.exists() else set()
    first_run = not seen

    fresh = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"! {url}: {e}", file=sys.stderr)
            continue
        for e in feed.entries[:10]:
            link = e.get("link")
            if link and link not in seen and not is_ad(e.get("title"), e.get("summary")):
                img = next((x.get("url") for x in e.get("enclosures", []) if IMAGE_OK in x.get("url", "")), None)
                fresh.append((link, clean(e.get("title")), clean(e.get("summary")), img))

    for ch in TG_CHANNELS:
        try:
            fresh += [p for p in tg_posts(ch) if p[0] not in seen]
        except Exception as e:
            print(f"! t.me/{ch}: {e}", file=sys.stderr)

    # горячее первым; обычные посты выходят не чаще одного за MIN_GAP, чтобы лента не лилась пачкой
    fresh.sort(key=lambda p: not (is_hot(p[1], p[2]) or is_fast(p[0])))
    # время последнего обычного поста хранится служебной записью в seen.json (он уже есть в кэше Actions)
    last_post = max((float(s.split("=", 1)[1]) for s in seen if s.startswith("__last_post__=")), default=0.0)
    posted = 0
    video_drafts = 0
    for link, title, summary, img in fresh:
        if posted >= args.limit:
            break
        hot = is_hot(title, summary) or is_fast(link)  # быстрые источники идут без паузы между постами
        if not hot and time.time() - last_post < MIN_GAP and not args.dry_run:
            break  # дальше только обычные, ждём следующего слота
        try:
            if "t.me/" not in link and len(summary) < 200:
                summary = article_text(link) or summary
                if len(summary) < 60 or BOT_WALL_RE.search(summary):
                    # сайт не отдал статью (защита от ботов) или текста нет: такой пост не публикуем
                    seen.add(link)
                    continue
            attributed = attribute_quote(title, summary)
            if attributed is None:  # прямая речь без понятного автора не публикуем
                seen.add(link)
                continue
            title, speaker_line = split_attribution(attributed)
            body = strip_branding(rewrite(title, summary))
            if not body:
                continue
            head, _, rest = body.partition("\n")
            text = f"<b>{html.escape(head.strip())}</b>"
            if speaker_line:  # кто сказал: отдельной строкой под заголовком, как «— Лукашенко»
                text += f"\n— {html.escape(speaker_line)}"
            if rest.strip():
                # короткие абзацы по одному-два предложения, как в образцах
                sents = re.split(r"(?<=[.!?])\s+", rest.strip())
                norm = lambda s: re.sub(r"\W+", "", s).lower()
                while sents and (norm(sents[0]) in norm(head) or norm(head) in norm(sents[0])):
                    sents.pop(0)  # первое предложение повторяет заголовок
                paras = [" ".join(sents[i:i + 2]) for i in range(0, len(sents), 2)]
                if paras:
                    text += "\n\n" + "\n\n".join(fmt_paragraph(p) for p in paras)
            # картинка только настоящая; нет хорошего фото: публикуем просто текстом
            if isinstance(img, list):  # в посте несколько видео: берём все
                media = download_all(img)
            else:
                p = download(img, BASE / "photo_tmp.jpg") if img else None
                media = [p] if p else []
            photo = media[0] if media else None
            if photo and str(photo).lower().endswith(".mp4"):
                if video_drafts >= 1:  # не заваливаем админа клипами: один пост с видео на запуск
                    continue
                video_drafts += 1
            # пометка «Не для слабонервных» и спойлер только для картинок/видео и только при явной жести в тексте
            graphic = bool(media) and is_graphic(title, summary)
            limit = (1024 - len(WARN) if media else 4096) - len(SIGNATURE)
            core = safe_cut(text, limit) + SIGNATURE
            if args.dry_run:
                print(compose_caption(core, graphic), f"\n[медиа: {len(media)}]" if media else "\n[только текст]", "\n---")
            elif photo and str(photo).lower().endswith((".mp4", ".mov")):
                send_draft(core, media, graphic)  # клипы только через подтверждение админа
            else:
                send(compose_caption(core, graphic), photo, spoiler=graphic, hot=hot)
                if not hot:
                    seen = {s for s in seen if not s.startswith("__last_post__=")}
                    last_post = time.time()
                    seen.add(f"__last_post__={last_post}")
            seen.add(link)
            posted += 1
        except Exception as e:
            print(f"! {link}: {e}", file=sys.stderr)

    if not args.dry_run:
        try:  # ВКонтакте: 3 лучших поста в сутки (если заданы VK_TOKEN и VK_GROUP_ID)
            import vk_post

            vk_post.run_slot(seen, is_hot)
        except Exception as e:
            print("! ВК:", e, file=sys.stderr)
        SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        purge_junk()
    print(f"Опубликовано: {posted}" + (" (первый запуск)" if first_run else ""))


if __name__ == "__main__":
    main()
