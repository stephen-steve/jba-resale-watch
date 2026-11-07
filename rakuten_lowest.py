#!/usr/bin/env python3
"""楽天市場で「実質最安値」を探す。

最安値の定義:
    実質価格 = 代金 − クーポン − ポイント還元 + 送料

送料の考え方:
- 原則「送料無料」を前提に比較する。
- 送料込み商品 → 送料 0 円。
- 送料別商品   → 合計 3,980 円以上買えば送料無料（39ショップ想定）。
                  3,980 円未満なら送料（既定 800 円）がかかる。

単品が一番安く見えても送料がかかるなら、それは最安値ではない。
そこで送料別の商品は次の 2 プランを計算し、安い方を採用する。
  A) 必要数だけ買って送料を払う
  B) 3,980 円以上になるまで個数を増やして送料無料にする（まとめ買い）
比較は「1 個あたりの実質価格」で行い、支払総額と購入個数も表示する。

使い方:
    export RAKUTEN_APP_ID=xxxxxxxx         # 楽天ウェブサービスのアプリID
    export RAKUTEN_ACCESS_KEY=xxxxxxxx     # 新APIの場合のアクセスキー（任意）
    python rakuten_lowest.py "キーワード" [オプション]

主なオプション:
    --qty N              欲しい個数（既定 1）
    --coupon SHOP=500    ショップ別クーポン。500 / 10% / 500@3000（3000円以上で500円引）
    --coupon '*=300@3980' 全ショップ共通クーポン
    --extra-point 4      SPU などで上乗せされるポイント倍率（%）
    --shipping 800       送料別商品で 3,980 円未満のときの想定送料
    --ng 中古,ジャンク   除外キーワード

標準ライブラリだけで動く（追加インストール不要）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 設定値

FREE_SHIPPING_THRESHOLD = 3980  # この金額以上で送料無料
DEFAULT_SHIPPING = 800          # 送料別・閾値未満のときの想定送料
BASE_POINT_RATE = 1             # 楽天の通常ポイント（1%）
TAX_RATE = 0.10                 # ポイントは税抜価格に付く

LEGACY_ENDPOINT = (
    "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
)
OPENAPI_ENDPOINT = (
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
)
TIMEOUT = 20
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- データ構造


@dataclass
class Coupon:
    """クーポン。amount が円引き、percent が % 引き（どちらか一方）。"""

    amount: int = 0
    percent: float = 0.0
    min_total: int = 0  # 利用条件（この金額以上で使える）

    def discount(self, total: int) -> int:
        if total < self.min_total:
            return 0
        if self.percent:
            return math.floor(total * self.percent / 100)
        return min(self.amount, total)


@dataclass
class Item:
    name: str
    price: int            # 税込単価
    shop_code: str
    shop_name: str
    url: str
    postage_included: bool  # True = 送料込み
    point_rate: int = 1     # ショップのポイント倍率（1 = 1%）
    tax_included: bool = True


@dataclass
class Plan:
    """ある商品をどう買うかの計算結果。"""

    item: Item
    qty: int              # 購入個数
    subtotal: int         # 代金合計
    coupon: int           # クーポン値引
    points: int           # ポイント還元
    shipping: int         # 送料
    note: str = ""
    breakdown: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        """実質支払額 = 代金 − クーポン − ポイント + 送料"""
        return self.subtotal - self.coupon - self.points + self.shipping

    @property
    def unit_cost(self) -> float:
        """1 個あたりの実質価格（ランキングの基準）"""
        return self.total / self.qty

    @property
    def cash_out(self) -> int:
        """レジで払う金額（ポイントは後日付与なので含めない）"""
        return self.subtotal - self.coupon + self.shipping


# ---------------------------------------------------------------- 計算ロジック


def parse_coupon(text: str) -> tuple[str, Coupon]:
    """'SHOP=500' / 'SHOP=10%' / 'SHOP=500@3000' / '*=300' を解釈する。"""
    if "=" not in text:
        raise ValueError(f"クーポン指定の形式が違います: {text}")
    shop, spec = text.split("=", 1)
    min_total = 0
    if "@" in spec:
        spec, min_s = spec.split("@", 1)
        min_total = int(min_s)
    spec = spec.strip()
    if spec.endswith("%"):
        return shop.strip(), Coupon(percent=float(spec[:-1]), min_total=min_total)
    return shop.strip(), Coupon(amount=int(spec), min_total=min_total)


def calc_points(amount_after_coupon: int, rate_percent: float, tax_included: bool) -> int:
    """ポイントはクーポン適用後・税抜価格に対して付与される。"""
    base = amount_after_coupon / (1 + TAX_RATE) if tax_included else amount_after_coupon
    return math.floor(base * rate_percent / 100)


def make_plan(
    item: Item,
    qty: int,
    coupons: dict[str, Coupon],
    extra_point: float,
    shipping_fee: int,
    note: str = "",
) -> Plan:
    subtotal = item.price * qty

    # クーポン: ショップ専用と全店共通のうち値引額が大きい方を 1 枚使う
    candidates = [c for c in (coupons.get(item.shop_code), coupons.get("*")) if c]
    coupon = max((c.discount(subtotal) for c in candidates), default=0)

    rate = max(item.point_rate, BASE_POINT_RATE) + extra_point
    points = calc_points(subtotal - coupon, rate, item.tax_included)

    if item.postage_included or subtotal >= FREE_SHIPPING_THRESHOLD:
        shipping = 0
    else:
        shipping = shipping_fee

    return Plan(
        item=item,
        qty=qty,
        subtotal=subtotal,
        coupon=coupon,
        points=points,
        shipping=shipping,
        note=note,
        breakdown={"point_rate": rate},
    )


def best_plan(
    item: Item,
    want_qty: int = 1,
    coupons: dict[str, Coupon] | None = None,
    extra_point: float = 0,
    shipping_fee: int = DEFAULT_SHIPPING,
    allow_bulk: bool = True,
) -> Plan:
    """欲しい個数で買う場合と、3,980 円以上にまとめ買いする場合を比べて安い方を返す。"""
    coupons = coupons or {}
    plain = make_plan(item, want_qty, coupons, extra_point, shipping_fee)

    if plain.shipping == 0:
        if item.postage_included:
            plain.note = "送料込み"
        else:
            plain.note = f"{FREE_SHIPPING_THRESHOLD:,}円以上で送料無料"
        return plain

    plain.note = f"送料 {plain.shipping:,}円 がかかる"
    if not allow_bulk:
        return plain

    bulk_qty = max(want_qty, math.ceil(FREE_SHIPPING_THRESHOLD / item.price))
    bulk = make_plan(
        item,
        bulk_qty,
        coupons,
        extra_point,
        shipping_fee,
        note=f"{bulk_qty}個まとめ買いで送料無料",
    )
    return bulk if bulk.unit_cost < plain.unit_cost else plain


def rank(
    items: list[Item],
    want_qty: int = 1,
    coupons: dict[str, Coupon] | None = None,
    extra_point: float = 0,
    shipping_fee: int = DEFAULT_SHIPPING,
    allow_bulk: bool = True,
) -> list[Plan]:
    plans = [
        best_plan(i, want_qty, coupons, extra_point, shipping_fee, allow_bulk)
        for i in items
    ]
    # 1個あたり実質価格 → 支払総額 の順で安い方を上に
    plans.sort(key=lambda p: (p.unit_cost, p.total))
    return plans


# ---------------------------------------------------------------- 楽天 API


def _point_rate_now(raw: dict) -> int:
    """ポイント倍率の期間外なら通常倍率に戻す。"""
    rate = int(raw.get("pointRate") or BASE_POINT_RATE)
    start, end = raw.get("pointRateStartTime"), raw.get("pointRateEndTime")
    if not (start and end):
        return rate
    try:
        fmt = "%Y-%m-%d %H:%M"
        now = datetime.now(JST).replace(tzinfo=None)
        if datetime.strptime(start, fmt) <= now <= datetime.strptime(end, fmt):
            return rate
        return BASE_POINT_RATE
    except ValueError:
        return rate


def to_item(raw: dict) -> Item:
    return Item(
        name=raw.get("itemName", ""),
        price=int(raw.get("itemPrice", 0)),
        shop_code=raw.get("shopCode", ""),
        shop_name=raw.get("shopName", ""),
        url=raw.get("itemUrl", ""),
        postage_included=int(raw.get("postageFlag", 1)) == 0,
        point_rate=_point_rate_now(raw),
        tax_included=int(raw.get("taxFlag", 0)) == 0,
    )


def _origin(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def search(
    keyword: str,
    app_id: str,
    access_key: str | None = None,
    pages: int = 3,
    min_price: int | None = None,
    max_price: int | None = None,
    ng_keyword: str | None = None,
    endpoint: str | None = None,
    referer: str | None = None,
) -> list[Item]:
    """楽天市場商品検索 API を呼び、在庫ありの商品を返す。"""
    if endpoint is None:
        endpoint = OPENAPI_ENDPOINT if access_key else LEGACY_ENDPOINT

    items: list[Item] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        params = {
            "applicationId": app_id,
            "keyword": keyword,
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            "page": page,
            "sort": "+itemPrice",
            "availability": 1,
        }
        if access_key:
            params["accessKey"] = access_key
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price
        if ng_keyword:
            params["NGKeyword"] = ng_keyword

        url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        headers = {"User-Agent": "rakuten-lowest/1.0"}
        if referer:
            # 新APIは、アプリ設定の「WebサイトURL」と一致する Referer が必須
            headers["Referer"] = referer
            headers["Origin"] = _origin(referer)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"楽天APIエラー HTTP {e.code}: {detail}\n"
                "  → 403 の場合: RAKUTEN_REFERER が楽天アプリ設定の『WebサイトURL』と"
                "一致しているか、アプリID・アクセスキーが正しいか確認してください"
            ) from None

        raws = data.get("Items") or data.get("items") or []
        for raw in raws:
            raw = raw.get("Item", raw)  # formatVersion=1 にも対応
            item = to_item(raw)
            if item.url in seen or item.price <= 0:
                continue
            seen.add(item.url)
            items.append(item)

        if page >= int(data.get("pageCount", 1)):
            break
        time.sleep(1)  # API は 1 秒 1 リクエストまで
    return items


# ---------------------------------------------------------------- 出力


def format_table(plans: list[Plan], top: int, markdown: bool = False) -> str:
    rows = []
    header = ["順位", "1個実質", "個数", "代金", "クーポン", "ポイント", "送料",
              "実質合計", "支払額", "備考", "ショップ", "商品"]
    for n, p in enumerate(plans[:top], 1):
        name = p.item.name if len(p.item.name) <= 40 else p.item.name[:40] + "…"
        if markdown:
            name = f"[{name.replace('|', '｜')}]({p.item.url})"
        rows.append([
            str(n), f"{p.unit_cost:,.0f}円", str(p.qty), f"{p.subtotal:,}",
            f"-{p.coupon:,}", f"-{p.points:,}", f"+{p.shipping:,}",
            f"{p.total:,}円", f"{p.cash_out:,}円", p.note, p.item.shop_name, name,
        ])

    if markdown:
        out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        out += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(out)

    out = []
    for r in rows:
        out.append(
            f"{r[0]:>2}. {r[1]:>9}  ×{r[2]}  代金{r[3]} 券{r[4]} pt{r[5]} 送{r[6]}"
            f" = {r[7]}（支払{r[8]}） {r[9]} / {r[10]}\n    {r[11]}"
        )
        out.append(f"    {plans[int(r[0]) - 1].item.url}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="楽天市場の実質最安値を探す")
    ap.add_argument("keyword", help="検索キーワード")
    ap.add_argument("--qty", type=int, default=1, help="欲しい個数（既定 1）")
    ap.add_argument("--coupon", action="append", default=[],
                    help="SHOP=500 / SHOP=10%% / SHOP=500@3000 / '*=300'（複数可）")
    ap.add_argument("--extra-point", type=float, default=0,
                    help="SPU 等の上乗せポイント倍率（%%）")
    ap.add_argument("--shipping", type=int, default=DEFAULT_SHIPPING,
                    help="送料別で3,980円未満のときの想定送料")
    ap.add_argument("--no-bulk", action="store_true",
                    help="まとめ買いプランを考えない（欲しい個数だけ買う）")
    ap.add_argument("--min-price", type=int)
    ap.add_argument("--max-price", type=int)
    ap.add_argument("--ng", help="除外キーワード（カンマ区切り）")
    ap.add_argument("--pages", type=int, default=3, help="取得ページ数（1ページ30件）")
    ap.add_argument("--top", type=int, default=10, help="表示件数")
    ap.add_argument("--markdown", action="store_true", help="Markdown 表で出力")
    args = ap.parse_args(argv)

    app_id = os.environ.get("RAKUTEN_APP_ID")
    if not app_id:
        print("[error] 環境変数 RAKUTEN_APP_ID を設定してください", file=sys.stderr)
        return 2

    coupons = dict(parse_coupon(c) for c in args.coupon)
    ng = " ".join(s for s in (args.ng or "").split(",") if s.strip()) or None

    try:
        items = search(
            args.keyword,
            app_id,
            access_key=os.environ.get("RAKUTEN_ACCESS_KEY") or None,
            pages=args.pages,
            min_price=args.min_price,
            max_price=args.max_price,
            ng_keyword=ng,
            endpoint=os.environ.get("RAKUTEN_API_ENDPOINT") or None,
            referer=os.environ.get("RAKUTEN_REFERER") or None,
        )
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    if not items:
        print("該当商品が見つかりませんでした")
        return 1

    plans = rank(items, args.qty, coupons, args.extra_point,
                 args.shipping, allow_bulk=not args.no_bulk)

    title = (f"「{args.keyword}」実質最安値（{len(items)}件中 上位{min(args.top, len(plans))}件）"
             f"  ※実質 = 代金 − クーポン − ポイント + 送料")
    body = format_table(plans, args.top, markdown=args.markdown)
    print(f"## {title}\n\n{body}" if args.markdown else f"{title}\n\n{body}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## {title}\n\n{format_table(plans, args.top, markdown=True)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
