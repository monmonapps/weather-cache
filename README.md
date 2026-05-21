# weather-cache

Open-Meteo の天気予報を 3 時間ごとに取得し、独自スキーマに変換して GitHub Pages
で配信するための中継リポジトリ。SimpleAlarm Android アプリの天気予報機能から
参照される。

設計の詳細は SimpleAlarm リポジトリ内 `WeatherSupport/SW_Design.md` を参照。

---

## 構成

```
weather-cache/
├── .github/workflows/
│   └── fetch-weather.yml          GitHub Actions: 手動トリガ（cron は dev 中停止）
├── fetch/
│   ├── fetch.py                   メインフェッチャ（並列 + LIMIT 対応）
│   ├── transform.py               Open-Meteo → 独自スキーマ
│   ├── url_hasher.py              HMAC-SHA256 ファイル名生成
│   └── requirements.txt           Python 依存（現状 stdlib のみ）
├── grid/
│   ├── generate_grid.py           ISJ から grid_cells.json を生成（年1回手動）
│   ├── grid_cells.json            対象格子点（2,632 セル、全国カバー）
│   └── cache/                     ISJ ZIP のローカルキャッシュ（gitignore）
└── docs/                          gh-pages 出力先（orphan branch にデプロイ）
```

---

## 現状フェーズ

| フェーズ | 状態 | 概要 |
|---|---|---|
| Phase 1 | 完了 | 1セル取得 → スキーマ変換 → gh-pages 配信が end-to-end で稼働 |
| **Phase 2** | **完了** | **2,632 セル分の grid_cells.json を生成（位置参照情報ベース）** |
| Phase 3+ | 未着手 | Android クライアント実装 |

### Phase 2 の要点

- **データ源**: 国土交通省「位置参照情報（大字・町丁目レベル）」13.0a 版（CC BY 4.0 互換）
  - 47 都道府県 ZIP（計 ~80MB）を `generate_grid.py` が自動 DL
  - 13.7M 件の街区センターを 10km グリッドに集約
  - 設計書の e-Stat 国勢調査 1km メッシュからの代替（結果は同等以上のカバレッジ）
- **セル数**: 2,632 セル（北海道〜沖縄まで人が居住する全エリア）
- **Open-Meteo コスト見積もり**:
  - 2,632 × 8回/日 × 30日 = **631,680 calls/month**
  - €29 Standard プラン 1,000,000 calls/月 の **63%**
- **cron は dev 中停止**: Free 10,000/day を超過するため、自動実行は €29 移行後に再開

---

## ローカル実行

### grid_cells.json を再生成（年1回程度）

```powershell
python grid\generate_grid.py
```

- 初回は 47 ZIP を DL（数分）。2 回目以降は `grid/cache/` から読む
- `--refresh` で再ダウンロード強制、`--version 14.0a` で ISJ 版指定

### 天気フェッチをローカルで試す

```powershell
$env:URL_SALT = "<32文字hex>"
$env:FETCH_LIMIT = "10"        # 開発中は少数で
python fetch\fetch.py
ls docs\v1\
```

`FETCH_LIMIT` を外す（または `0` に）と全 2,632 セル取得。Free モードでは
レート上限・日次 10K 制限に確実に当たります。

---

## GitHub Actions 設定

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録:

| Secret 名 | 内容 | 必須？ |
|---|---|---|
| `URL_SALT` | HMAC 用ソルト（32文字 hex 推奨） | **必須** |
| `OPENMETEO_APIKEY` | Open-Meteo 商用プラン API キー | 任意（未設定なら Free モード） |

SALT 生成例（PowerShell）:

```powershell
-join ((1..32) | ForEach { '{0:x}' -f (Get-Random -Maximum 16) })
```

### 手動実行

1. **Actions** タブ → `fetch-weather`
2. **Run workflow** → `fetch_limit` を入力（dev 中は `10` 程度推奨、`0` で全セル）
3. 完了後 **Settings → Pages** で gh-pages branch を Source に設定

### 本番運用への切替

1. Open-Meteo €29 Standard を契約 → API キー取得
2. GitHub Secrets に `OPENMETEO_APIKEY` を登録
3. `.github/workflows/fetch-weather.yml` の cron コメントを外す
4. これで 3 時間ごとに自動フェッチが回る

---

## 環境変数

| 変数 | 用途 | 既定値 |
|---|---|---|
| `URL_SALT` | HMAC-SHA256 ソルト | （必須） |
| `OPENMETEO_APIKEY` | Commercial API キー | 未設定 |
| `OPENMETEO_MODE` | `free` or `commercial` | APIKEY 設定時は `commercial`、それ以外は `free` |
| `FETCH_LIMIT` | 先頭 N セルだけ取得（0 = 制限なし） | 0 |
| `FETCH_WORKERS` | 並列ワーカー数 | 8 |

---

## 出力スキーマ

`docs/v1/<HMAC>.json` の中身は SimpleAlarm の `WeatherSupport/SW_Design.md` §4.1
に準拠。Android クライアントはこのスキーマのみを認識し、Open-Meteo のレスポンス
形式には依存しない。

---

## データ出典の表記

Play Store のアプリ説明文に以下を含める必要があります（CC BY 4.0 / 政府標準利用規約 2.0 準拠）:

> 本アプリは「位置参照情報（国土交通省）」のデータを加工して
> 天気予報の対象地域決定に使用しています。

Open-Meteo の利用条件（出典表記）も Play Store 説明文に含めること:

> 天気予報データは [Open-Meteo.com](https://open-meteo.com/) を使用しています。
