# jba-resale-watch
## 楽天市場 実質最安値サーチ（`rakuten_lowest.py`）

**実質価格 = 代金 − クーポン − ポイント還元 + 送料** で比較します。

- 送料込み商品 → 送料0円
- 送料別商品 → 合計3,980円以上なら送料無料、未満なら想定送料（既定800円）を加算
- 単品が安くても送料がかかる場合は、「3,980円以上になるまでまとめ買い」した場合の
  **1個あたり実質価格**も計算し、安い方で順位付けします（購入個数・支払額も表示）
- ポイントは「クーポン適用後の税抜価格 × (ショップ倍率 + 上乗せ倍率)」で概算

### 準備
1. [楽天ウェブサービス](https://webservice.rakuten.co.jp/) でアプリIDを発行
2. リポジトリの Settings → Secrets → Actions に `RAKUTEN_APP_ID`
   （新APIでアクセスキーがある場合は `RAKUTEN_ACCESS_KEY` も）を登録

### 実行
- GitHub: Actions → **Rakuten lowest price** → Run workflow（結果はジョブの Summary に表で出ます）
- ローカル:
  ```bash
  export RAKUTEN_APP_ID=xxxx
  python rakuten_lowest.py "ボディソープ 詰め替え" --qty 2 --coupon 'shopA=500@3000' --extra-point 4
  ```

### 注意
- クーポン情報は楽天APIで取得できないため、手入力（`--coupon`）です
- 送料別商品の送料額はAPIで取れないため想定値です（`--shipping` で変更可）
- 3,980円送料無料は39ショップ前提。対象外ショップや沖縄・離島は別途確認してください
