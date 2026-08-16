#!/usr/bin/env python3
"""JBA TICKET 公式リセールの出品を監視して、新規出品をメール通知する。

- Shopify の products.json を優先して使い、ダメなら HTML から拾うフォールバック付き
- 前回の出品一覧を state.json に保存し、差分が出たときだけ通知する
"""

import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

# 監視したいイベント。カンマ区切りで複数指定できる
EVENT_IDS = [
    e.strip()
    for e in os.environ.get(
        "EVENT_ID", "0647c25d-cb81-4cf1-8914-3a6b3393987c"
    ).split(",")
    if e.strip()
]

BASE = "https://resale.jba-ticket.jp"
STATE_FILE = "state.json"
TIMEOUT = 20
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def collection_url(event_id: str) -> str:
    return f"{BASE}/collections/resale/event_id:{event_id}"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read().decode("utf-8", errors="replace")


def listings_from_json(event_id: str):
    """products.json から出品一覧を取る（成功したら list、失敗したら None）"""
    url = f"{collection_url(event_id)}/products.json?limit=250"
    try:
        data = json.loads(fetch(url))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None

    products = data.get("products")
    if products is None:
        return None

    items = []
    for p in products:
        handle = p.get("handle", "")
        items.append(
            {
                "key": str(p.get("id") or handle),
                "title": p.get("title") or handle,
                "url": f"{BASE}/products/{handle}" if handle else collection_url(event_id),
            }
        )
    return items


def listings_from_html(event_id: str):
    """HTML から商品リンクを拾うフォールバック"""
    html = fetch(collection_url(event_id))
    handles = []
    for h in re.findall(r'/products/([A-Za-z0-9%\-_]+)', html):
        if h not in handles:
            handles.append(h)
    return [
        {"key": h, "title": h, "url": f"{BASE}/products/{h}"} for h in handles
    ]


def get_listings(event_id: str):
    items = listings_from_json(event_id)
    if items is None:
        print(f"[warn] products.json が使えないので HTML にフォールバック: {event_id}")
        items = listings_from_html(event_id)
    return items


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_mail(subject: str, body: str) -> None:
    user = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASS")
    to = os.environ.get("MAIL_TO") or user

    if not (user and password and to):
        print("[warn] メール設定が無いのでコンソール出力のみ")
        print(subject)
        print(body)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"[info] メール送信済み: {to}")


def main() -> int:
    state = load_state()
    changed = False

    for event_id in EVENT_IDS:
        try:
            items = get_listings(event_id)
        except Exception as e:  # ネットワーク断などで落ちても次回に持ち越す
            print(f"[warn] 取得失敗 ({event_id}): {e}")
            continue

        current = {i["key"]: i for i in items}
        seen = set(state.get(event_id, []))
        new_keys = [k for k in current if k not in seen]

        print(f"[info] {event_id}: 出品 {len(current)} 件 / うち新規 {len(new_keys)} 件")

        if new_keys:
            lines = ["リセールの出品を検知しました。", ""]
            for k in new_keys:
                lines.append(f"・{current[k]['title']}")
                lines.append(f"  {current[k]['url']}")
            lines += ["", "一覧ページ:", collection_url(event_id)]
            send_mail(
                f"【JBAリセール】新規出品 {len(new_keys)}件",
                "\n".join(lines),
            )

        # 売り切れて消えた分は state からも消す（再出品されたら再通知される）
        if state.get(event_id, []) != sorted(current):
            state[event_id] = sorted(current)
            changed = True

    if changed:
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
