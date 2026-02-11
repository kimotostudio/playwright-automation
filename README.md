# Spiritual Salon Automation

Playwright + Python で営業フォーム送信を段階運用するツールです。

- `SEMI_AUTO` (default): フォーム入力 + スクリーンショット + 停止 (`prepared`)
- `FULL_AUTO`: 最終送信まで実行 (`sent`)

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
# 通常実行 (settings.jsonのmodeを使用)
python src/main.py

# テスト件数で実行
python src/main.py --test

# 強制モード指定
python src/main.py --mode SEMI_AUTO
python src/main.py --mode FULL_AUTO

# レポートのみ表示（最新summary）
python src/main.py --report-only
```

`run.sh` / `run.bat` もそのまま使えます。

## SEMI_AUTO Workflow

1. サイトへ移動して問い合わせフォームを検出
2. フォーム入力
3. スクリーンショット保存
4. 最終送信前で停止し `prepared` を記録

出力例:

- `screenshots/YYYYMMDD/{salon_id}_01_before_fill.png`
- `screenshots/YYYYMMDD/{salon_id}_02_after_fill.png`
- `screenshots/YYYYMMDD/{salon_id}_03_before_submit_or_confirm.png`
- （確認ページあり）`screenshots/YYYYMMDD/{salon_id}_04_on_confirmation_page.png`

`results/review_queue_YYYYMMDD.csv` に手動確認キューが追記されます（同日同IDは重複追加しません）。

ログには停止位置を明示します:

- `SEMI_AUTO: stopped on confirmation page`
- `SEMI_AUTO: stopped before submit`

## Name Filling Policy

- 単一名前フィールド（お名前/氏名/Name）: `KIMOTO STUDIO`
- 姓/名分割フィールド:
 - 姓/Last Name: `木許`
 - 名/First Name: `裕輔`
- 会社名/屋号フィールド: `KIMOTO STUDIO`
- フリガナ欄:
 - 任意なら未入力のまま
 - 必須なら `姓フリガナ=キモト` / `名フリガナ=ユウスケ`

## Message Formatting Options

`config/settings.json` で本文整形を制御できます:

- `wrap_message`: `true/false`
- `wrap_width`: `40-60` 推奨（default `56`）

本文は段落区切り（空行）を維持しつつ折り返し、`────────────────` 区切りを付与します。

## Manual Submit (prepared 1件送信)

```bash
python src/resume_submit.py --salon-id 1100
```

動作:

1. review queue から `prepared` 行を取得
2. `final_step_url` を headed ブラウザで開く
3. 保存済み selector 優先 + ボタン文言フォールバックで送信ボタン再特定
4. ENTER確認後に送信
5. `screenshots/.../{salon_id}_04_after_submit.png` 保存
6. `ledger/state/results/review_queue` を更新

失敗時は `ledger` に `failed` を追記し、`completed_ids` は更新しません。

## Safety / Anti-Duplicate

- 二重送信防止は **state + ledger の二重チェック**
  - `data/state.json` `completed_ids`（sentのみ）
  - `data/submission_ledger.csv`（sent履歴）
- `SEMI_AUTO` の `prepared` は `completed_ids` に入れない
- `FULL_AUTO` / manual submit で実送信成功時のみ `completed_ids` に追加

## Blocklist / Cooldown

- `data/blocklist_domains.txt`
- `data/blocklist_urls.txt`
- `data/domain_cooldowns.json`

bot保護検知（CAPTCHA / Cloudflare / verify human / HTTP 403/429）時:

- `status=skipped`, `reason=bot_protection`
- ドメインを7日ブロック（cooldown）
- 30-90秒ランダム待機して次へ

## State.json (atomic)

`data/state.json` は temp file -> replace で原子的に更新します。

管理項目:

- `last_run_date` (JST)
- `today_count` (sentのみ)
- `total_sent`
- `completed_ids` (sentのみ)

日付がJSTで変わると `today_count` を自動リセットします。

## Ledger

`data/submission_ledger.csv` columns:

- `timestamp`
- `run_mode`
- `salon_id`
- `salon_name`
- `domain`
- `contact_url`
- `final_step_url`
- `status`
- `reason`

## Reporting

`results/summary_YYYYMMDD.md` を毎回生成:

- processed/sent/prepared/failed/skipped
- top reasons
- newly blocked domains
- next lead index/id

## Main Outputs

- `results/submissions_YYYYMMDD.csv`
- `results/review_queue_YYYYMMDD.csv`
- `results/summary_YYYYMMDD.md`
- `results/logs/YYYYMMDD.log`
- `results/logs/YYYYMMDD.jsonl` (`log_format=jsonl` のとき)
- `screenshots/YYYYMMDD/*.png`
- `data/submission_ledger.csv`
- `data/state.json`

## SEMI_AUTO Test Procedure

ローカルのモックフォームで、実送信なしに安全検証する手順です。

1. モックサーバー起動

```bash
python tests/mock_server.py --host 127.0.0.1 --port 5000
```

モックサーバーは2種類のフォームを提供します:

- `/contact_single`: 単一名前 + 会社名(任意)
- `/contact_split`: 姓/名分割 + フリガナ必須 + 会社名(任意)

2. `config/settings.json` を以下で固定
 - `mode = "SEMI_AUTO"`
 - `headless = false`
 - `daily_limit = 2`
 - `stop = false`
 - `min_delay_sec = 1`
 - `max_delay_sec = 2`
 - `leads_csv_path = "data/leads_test.csv"`

3. 実行（実送信なし）

```bash
python src/main.py
```

4. 期待結果
 - `screenshots/YYYYMMDD/<id>_01_before_fill.png`
 - `screenshots/YYYYMMDD/<id>_02_after_fill.png`
 - `screenshots/YYYYMMDD/<id>_03_before_submit_or_confirm.png`
 - （確認ページあり）`screenshots/YYYYMMDD/<id>_04_on_confirmation_page.png`
 - `results/submissions_YYYYMMDD.csv` に `status=prepared` が2件
 - `results/review_queue_YYYYMMDD.csv` に `final_step_url` と selector が記録
 - `data/state.json` の `today_count` は増えない（sentのみカウント）

5. 同じコマンドを再実行して安定性確認
 - `review_queue` は同日同IDで重複追加されない
 - 二重送信は state+ledger の重複チェックで防止される
