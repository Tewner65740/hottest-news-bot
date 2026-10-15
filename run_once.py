"""Один запуск для GitHub Actions: ~4,5 минуты слушает Telegram, потом выходит.

Между запусками состояние (seen.json, inbox/, pending/, awaiting.json) хранится в кэше Actions.
Каждые NEWS_EVERY секунд дополнительно подтягивает новости (черновики админу).
"""
import json
import os
import subprocess
import sys
import time

import requests

import listener as l

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "270"))
NEWS_EVERY = int(os.environ.get("NEWS_EVERY", "1800"))
AWAIT_FILE = l.nb.BASE / "awaiting.json"
NEWS_STAMP = l.nb.BASE / "last_news.txt"
OFFSET_FILE = l.nb.BASE / "offset.txt"


def load_awaiting():
    if AWAIT_FILE.exists():
        raw = json.loads(AWAIT_FILE.read_text("utf-8"))
        l.awaiting.update({int(k): tuple(v) for k, v in raw.items()})


def save_awaiting():
    AWAIT_FILE.write_text(json.dumps({str(k): list(v) for k, v in l.awaiting.items()}), encoding="utf-8")


def maybe_news():
    last = float(NEWS_STAMP.read_text()) if NEWS_STAMP.exists() else 0
    if time.time() - last < NEWS_EVERY:
        return
    NEWS_STAMP.write_text(str(time.time()))
    subprocess.run([sys.executable, "news_bot.py", "--limit", "3"], cwd=l.nb.BASE, check=False)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_awaiting()
    offset = int(OFFSET_FILE.read_text()) if OFFSET_FILE.exists() else None
    end = time.time() + RUN_SECONDS
    maybe_news()
    while time.time() < end:
        wait = max(0, min(25, int(end - time.time())))
        try:
            r = requests.get(
                f"{l.API}/getUpdates",
                params={"timeout": wait, "offset": offset,
                        "allowed_updates": json.dumps(["callback_query", "message"])},
                timeout=wait + 20,
            ).json()
        except Exception as e:
            print("!", e)
            time.sleep(3)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    l.handle_message(u["message"])
                elif "callback_query" in u:
                    l.handle(u["callback_query"])
            except Exception as e:
                print("!", e)
        if r.get("result"):
            OFFSET_FILE.write_text(str(offset))
            save_awaiting()
        l.flush_stale()
    # подтверждаем обработанные обновления, чтобы они не пришли повторно
    if offset:
        requests.get(f"{l.API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30)
    save_awaiting()


if __name__ == "__main__":
    main()
