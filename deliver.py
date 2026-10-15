"""Обработка предложок читателей (для Claude).

  python deliver.py --list                      показать материалы, ждущие правки
  python deliver.py <id> --text "заголовок\\nабзац 1\\n\\nабзац 2"   заменить текст и отправить админу
  python deliver.py <id>                        отправить админу без изменений
"""
import argparse
import json
import sys
import time

import listener as l


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("sid", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--text")
    a = ap.parse_args()
    if a.list:
        for f in sorted(l.INBOX.glob("*.json")):
            m = json.loads(f.read_text("utf-8"))
            if m.get("status") == "await_polish":
                print(f"--- {f.stem} | автор: {m['author']} | тип: {m['kind']} | город: {m.get('city')} | "
                      f"улица: {m.get('street')} | шок: {m.get('graphic')}\n{m['text']}\n")
        return
    f = l.INBOX / f"{a.sid}.json"
    m = json.loads(f.read_text("utf-8"))
    if a.text:
        m["text"] = a.text.replace("\\n", "\n").strip()
        m["polished"] = True
    m["status"] = "sent"
    m["ts"] = time.time()
    f.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    l.send_to_admin(a.sid, m)
    print("отправлено админу", a.sid)


if __name__ == "__main__":
    main()
