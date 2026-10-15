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

TG_CHANNELS = ["novosti_efir", "rhymestg", "ostorozhno_novosti", "moscowach", "rian_ru", "bbbreaking"]

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
    + "|".join(TG_CHANNELS) + r"|эфир|рифмы|осторожно|москвач|риа новости",
    re.I,
)


AD_RE = re.compile(
    r"реклам|erid|промокод|промо-код|скидк|акци[яи]\b|купи(ть|те)?\b|розыгрыш|"
    r"конкурс|выигра|партн[её]рск|спонсор|ООО\s|ИП\s|ИНН|казино|букмекер|ставк[аиу] на|"
    r"заработ(ок|ай|ать)|подработк|инвестици|курс(ы|ов)? по\b|вакансия|"
    r"переходи|жми|жмите|регистрируйся|бонус",
    re.I,
)


def is_ad(*texts):
    return any(AD_RE.search(t or "") for t in texts)


def strip_branding(text):
    """Убирает строки с подписями, рекламой и упоминаниями чужих каналов."""
    return "\n".join(l for l in text.split("\n") if not BRAND_RE.search(l)).strip()


def download(url, path):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
        return path
    except Exception:
        return None


def tg_posts(channel):
    """Последние посты публичного канала через веб-превью t.me/s/."""
    page = requests.get(f"https://t.me/s/{channel}", timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    out = []
    for post, body in re.findall(
        r'data-post="([^"]+)".*?class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S
    ):
        raw = clean(re.sub(r"<br\s*/?>", "\n", body))
        if is_ad(raw):
            continue
        text = strip_branding(raw)
        if len(text) > 40:
            first = text.split("\n")[0][:120]
            out.append((f"https://t.me/{post}", first, text))
    return out[-10:]


def article_text(url, limit=1500):
    """Основной текст статьи: абзацы со страницы, без меню и подписей."""
    try:
        page = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception:
        return ""
    paras = [clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S)]
    paras = [p for p in paras if len(p) > 60 and not is_ad(p)]
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


GRAPHIC_RE = re.compile(
    r"труп|тела? погиб|погиб|убит|убий|расстрел|кровь|крови|кровав|расчлен|обезглав|"
    r"смерт|жертв|взрыв|теракт|пожар|дтп|авари|катастроф|раненн|ранен|насили|изнасил|"
    r"пытк|самоубий|тело|мертв|мёртв|черв|паразит|18\+|шок",
    re.I,
)


def is_graphic(*texts):
    return any(GRAPHIC_RE.search(t or "") for t in texts)


def send(caption, media_path, spoiler=False, chat_id=None, reply_markup=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    is_video = str(media_path).lower().endswith((".mp4", ".mov"))
    method, field = ("sendVideo", "video") if is_video else ("sendPhoto", "photo")
    data = {
        "has_spoiler": spoiler,
        "chat_id": chat_id or os.environ["TELEGRAM_CHANNEL"],
        "caption": caption,
        "parse_mode": "HTML",
        # SILENT=1 в .env: посты без звука/уведомлений. Для обычного режима поставьте SILENT=0.
        "disable_notification": os.environ.get("SILENT", "1") == "1" if not chat_id else False,
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    with open(media_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", data=data, files={field: f}, timeout=120
        )
    r.raise_for_status()


PENDING = BASE / "pending"


def send_draft(caption, media_path, spoiler):
    """Присылает черновик админу в личку с кнопками; публикует listener.py после нажатия."""
    import uuid

    PENDING.mkdir(exist_ok=True)
    pid = uuid.uuid4().hex[:10]
    media = PENDING / f"{pid}{Path(media_path).suffix}"
    media.write_bytes(Path(media_path).read_bytes())
    (PENDING / f"{pid}.json").write_text(
        json.dumps({"caption": caption, "media": media.name, "spoiler": spoiler}, ensure_ascii=False),
        encoding="utf-8",
    )
    markup = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"ok:{pid}"},
        {"text": "❌ Пропустить", "callback_data": f"no:{pid}"},
    ]]}
    send(caption, media, spoiler, chat_id=os.environ["ADMIN_CHAT_ID"], reply_markup=markup)


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
    ap.add_argument("--limit", type=int, default=5, help="максимум постов за запуск")
    args = ap.parse_args()

    load_env()
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
            fresh += [p + (None,) for p in tg_posts(ch) if p[0] not in seen]
        except Exception as e:
            print(f"! t.me/{ch}: {e}", file=sys.stderr)

    posted = 0
    for link, title, summary, img in fresh:
        if posted >= args.limit:
            break
        try:
            if "t.me/" not in link and len(summary) < 200:
                summary = article_text(link) or summary
            body = strip_branding(rewrite(title, summary))
            if not body:
                continue
            head, _, rest = body.partition("\n")
            text = f"<b>{html.escape(head.strip())}</b>"
            if rest.strip():
                # короткие абзацы по одному-два предложения, как в образцах
                sents = re.split(r"(?<=[.!?])\s+", rest.strip())
                paras = [" ".join(sents[i:i + 2]) for i in range(0, len(sents), 2)]
                text += "\n\n" + "\n\n".join(html.escape(p) for p in paras)
            graphic = is_graphic(title, summary)
            if graphic:
                text = "⚠️ <b>Не для слабонервных</b>\n\n" + text
            text = text[: 1024 - len(SIGNATURE)] + SIGNATURE
            image_path = BASE / "card_tmp.png"
            photo = download(img, BASE / "photo_tmp.jpg") if img else None
            if not photo:
                photo = news_card(head.strip(), image_path)
            if args.dry_run:
                print(text, "\n[фото из новости]" if img else "\n[своя карточка]", "\n---")
            elif os.environ.get("APPROVAL", "0") == "1":
                send_draft(text, photo, graphic)
            else:
                send(text, photo, spoiler=graphic)
            seen.add(link)
            posted += 1
        except Exception as e:
            print(f"! {link}: {e}", file=sys.stderr)

    if not args.dry_run:
        SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    print(f"Опубликовано: {posted}" + (" (первый запуск)" if first_run else ""))


if __name__ == "__main__":
    main()
