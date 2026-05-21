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
│   └── fetch-weather.yml          GitHub Actions: 3時間ごと cron
├── fetch/
│   ├── fetch.py                   メインフェッチャ
│   ├── transform.py               Open-Meteo → 独自スキーマ
│   ├── url_hasher.py              HMAC-SHA256 ファイル名生成
│   └── requirements.txt           Python 依存（Phase 1 は stdlib のみ）
├── grid/
│   └── grid_cells.json            対象格子点（Phase 1 は 1 セルのみ）
└── docs/                          gh-pages 出力先（orphan branch にデプロイ）
```

---

## Phase 1（現状）

- 1 セル（東京駅 35.685N, 139.775E）のみ取得
- Open-Meteo Free / Commercial 両対応（環境変数で切替）
- レスポンスを独自スキーマに変換して `docs/v1/<HMAC>.json` に書き出し
- `docs/v1/index.json` にメタデータを書き出し

---

## ローカル実行

```bash
# 1. SALT を準備（実際の値は GitHub Secrets と同じものを使う）
export URL_SALT="your-32-char-hex-salt"

# 2. 開発中は無料エンドポイントで十分
unset OPENMETEO_APIKEY    # 未設定なら自動で free モード

# 3. 実行
python fetch/fetch.py

# 4. 出力確認
ls docs/v1/
```

PowerShell 版:

```powershell
$env:URL_SALT = "your-32-char-hex-salt"
Remove-Item Env:OPENMETEO_APIKEY -ErrorAction SilentlyContinue
python fetch\fetch.py
```

---

## GitHub Actions 設定

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録:

| Secret 名 | 内容 | Phase 1 で必須か |
|---|---|---|
| `URL_SALT` | HMAC 用ソルト（32文字 hex 推奨） | **必須** |
| `OPENMETEO_APIKEY` | Open-Meteo 商用プラン API キー | 任意（未設定なら Free モード） |

SALT 生成例（PowerShell）:

```powershell
-join ((1..32) | ForEach { '{0:x}' -f (Get-Random -Maximum 16) })
```

初回手動実行:

1. リポジトリの **Actions** タブ
2. `fetch-weather` ワークフローを選択
3. **Run workflow** で手動トリガ
4. 完了後 **Settings → Pages** で gh-pages branch を Source に設定

---

## 環境変数

| 変数 | 用途 | 既定値 |
|---|---|---|
| `URL_SALT` | HMAC-SHA256 ソルト | （必須） |
| `OPENMETEO_APIKEY` | Commercial API キー | 未設定 |
| `OPENMETEO_MODE` | `free` or `commercial` | APIKEY 設定時は `commercial`、それ以外は `free` |

---

## 出力スキーマ

`docs/v1/<HMAC>.json` の中身は SimpleAlarm の `WeatherSupport/SW_Design.md` §4.1
に準拠。Android クライアントはこのスキーマのみを認識し、Open-Meteo のレスポンス
形式には依存しない。

---

## 移行計画

- **Phase 2**: `grid/generate_grid.py` を追加し、e-Stat の国勢調査人口メッシュから
  約 1,000 セルの `grid_cells.json` を生成する
- **Phase 1 → 本番**: GitHub Secrets に `OPENMETEO_APIKEY` を登録するだけで
  自動的に Commercial エンドポイントに切り替わる
