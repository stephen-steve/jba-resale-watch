#!/usr/bin/env python3
"""JBA TICKET 公式リセールの出品を監視して、新規出品をメール通知する。（v2）

v2 の変更点:
- テンプレート由来の偽リンク（_handle_ など）を除外
- 「商品が見つかりませんでした」表示を最優先で判定
- ページ上の「N件」表示と突き合わせて、ズレたら警告を出す
- 新規出品は商品名・価格・在庫状況まで取得してメールに載せる
"""

import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

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

# 在庫ゼロ・売り切れ時にサイトが出す文言
EMPTY_MARKERS = [
    "検索に一致する商品が見つかりませんでした",
    "商品が見つかりませんでした",
]

# 商品ではないのに /products/ を含むもの（テンプレート雛形や標準商品）
IGNORE_HANDLES = {"_handle_", "gift-card"}


def collection_url(event_id: str) -> str:
    return f"{BASE}/collections/resale/event_id:{event_id}"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read().decode("utf-8", errors="replace")


def is_real_handle(handle: str) -> bool:
    """テンプレート由来の偽リンクを弾く"""
    if handle in IGNORE_HANDLES:
        return False
    if handle.startswith("_") or handle.endswith("_"):
        return False
    # Liquid / JS のテンプレート記法が混ざっているもの
    if any(t in handle for t in ("{{", "}}", "%7B", "$", "handle")):
        return False
    return True


def product_detail(handle: str) -> dict:
    """Shopify の商品JSONから名前と価格を取る。失敗しても致命的ではない。"""
    info = {"title": handle, "price": None, "available": None}
    try:
        data = json.loads(fetch(f"{BASE}/products/{handle}.js"))
    except Exception:
        return info

    info["title"] = data.get("title") or handle
    price = data.get("price")
    if isinstance(price, int):
        info["price"] = f"{price // 100:,}円"
    info["available"] = data.get("available")
    return info


def get_listings(event_id: str):
    """(出品リスト, ページ表示件数) を返す"""
    html = fetch(collection_url(event_id))

    # ページ上の「N件」表示（照合用）
    m = re.search(r">\s*(\d+)\s*件", html)
    shown_count = int(m.group(1)) if m else None

    # 「見つかりませんでした」が出ていたら、リンクに関係なく0件
    if any(marker in html for marker in EMPTY_MARKERS):
        return [], 0

    handles = []
    for h in re.findall(r'href="/products/([A-Za-z0-9\-_%]+)', html):
        if is_real_handle(h) and h not in handles:
            handles.append(h)

    return [{"key": h, "handle": h} for h in handles], shown_count


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
    print("[info] メール送信しました")


def main() -> int:
    state = load_state()
    changed = False

    for event_id in EVENT_IDS:
        try:
            items, shown_count = get_listings(event_id)
        except Exception as e:
            print(f"[warn] 取得失敗 ({event_id}): {e}")
            continue

        current = {i["key"]: i for i in items}

        # ページ表示件数と拾えた数が食い違ったら知らせる（解析ズレの早期発見用）
        if shown_count is not None and shown_count != len(current):
            print(
                f"[warn] ページ表示 {shown_count} 件 に対し "
                f"{len(current)} 件しか解析できていません。要確認"
            )

        seen = set(state.get(event_id, []))
        new_keys = [k for k in current if k not in seen]

        print(f"[info] {event_id}: 出品 {len(current)} 件 / うち新規 {len(new_keys)} 件")

        if new_keys:
            lines = ["リセールの出品を検知しました。", ""]
            for k in new_keys:
                d = product_detail(current[k]["handle"])
                label = d["title"]
                if d["price"]:
                    label += f" / {d['price']}"
                if d["available"] is False:
                    label += "（既に売切）"
                lines.append(f"・{label}")
                lines.append(f"  {BASE}/products/{current[k]['handle']}")
            lines += ["", "一覧ページ:", collection_url(event_id)]
            send_mail(f"【JBAリセール】新規出品 {len(new_keys)}件", "\n".join(lines))

        if state.get(event_id, []) != sorted(current):
            state[event_id] = sorted(current)
            changed = True

    if changed:
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
