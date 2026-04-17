# Spiritual Salon Automation

Playwright + Python で営業フォーム送信を段階運用するツールです。

- `SEMI_AUTO` (default): フォーム入力 + スクリーンショット + 停止 (`prepared`)
- `FULL_AUTO`: 最終送信まで実行 (`sent`)
- `DETECT_ONLY`: フォーム有無だけ検出してキュー化（入力・送信しない）

既定値:

- `daily_limit = 10`（JST日次リセット）
- リード間隔 `min_delay_sec = max_delay_sec = 5`（5秒）

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

# モード指定（modeのみ変更。verifyモードは自動でONにならない）
python src/main.py --mode SEMI_AUTO
python src/main.py --mode FULL_AUTO
python src/main.py --mode DETECT_ONLY

# 非対話でSEMI_AUTOを100件回す（verifyプロンプトなし）
python src/main.py --mode SEMI_AUTO --limit 100

# verifyモードを明示的に有効化（5件 + 対話プロンプト）
python src/main.py --mode SEMI_AUTO --semi-auto-verify --semi-auto-limit 5

# verifyモードでプロンプト無効
python src/main.py --mode SEMI_AUTO --semi-auto-verify --semi-auto-limit 5 --no-prompt

# 対象CSVを実行時に差し替え（実データ）
python src/main.py --mode SEMI_AUTO --leads data/leads.csv

# モック検証用CSVを使う場合（明示的なmock運用）
python src/main.py --mode SEMI_AUTO --leads data/leads_test.csv --test

# レポートのみ表示（最新summary）
python src/main.py --report-only
```

`run.sh` / `run.bat` もそのまま使えます。

## Staff Review App (Local-Only)

prepared案件をスタッフが高速に確認するためのローカル専用ダッシュボードです。

```bash
streamlit run src/staff_review_app.py
```

または:

```bash
./run_dashboard.sh
run_dashboard.bat
```

保存先:

- オペレーター操作ログ: `results/operator_actions_YYYYMMDD.csv`（append-only）
- （既存互換）スタッフ操作ログ: `data/staff_actions_YYYYMMDD.csv`

UIの基本:

- 左サイドバー: 表示モード / データソース / ステータス / タグ / 検索 / 進捗
- メイン2カラム: 左=リード一覧、右=詳細 + 主要操作
- Visibility Mode:
  - `Review（ほぼ全件表示）`（既定）
  - `Strict（厳しめ）`
- Data source:
  - `Merged（推奨）`（既定）
  - `Review queue`
  - `Submissions`
- ステータスフィルタ:
  - `prepared_full / prepared_partial / prepared_external / prepared_review_needed`
  - `skipped_*`（任意表示）
- タグフィルタ:
  - `GOOD / BORDERLINE / EXCLUDE_CLEAR`
- どのstatus行でも選択・操作可能（prepared限定ではない）

レイアウト図（Task 8）:

```text
+--------------------------------------------------------------+
| 営業オペレーターダッシュボード                              |
+---------------------------+----------------------------------+
| サイドバー                | メイン                           |
| - 表示モード              |  左: リード一覧                  |
| - データソース            |   id / name / domain / status   |
| - ステータス/タグ         |   confidence / tag / reason     |
| - 検索                    |   last_action                    |
| - 日次進捗                |                                  |
|                           |  右: リード詳細                  |
|                           |   - URL群 / stop_state / notes  |
|                           |   - [Run Prefill]（主ボタン）    |
|                           |   - [Open Demo/Original/Contact]|
|                           |   - [Mark Sent/Skip/Undo]       |
+---------------------------+----------------------------------+
| 下部: スクリーンショット（01-04サムネイル）                 |
+--------------------------------------------------------------+
```

主要操作:

- `Run Playwright Prefill (no submit)`: Playwrightでフォーム入力し、送信せず停止
  - 既定でブラウザを開いたまま維持し、手動で閉じるまで待機
  - `stop_state`（`confirmation` / `submit_button` / `form_filled` / `unknown`）を記録
- `Open Demo / Open Original / Open Contact URL`
- 判定ボタン:
  - `Mark Sent`
  - `Mark Skip + 理由`
  - `Undo last action`

Playwright prefillをCLIで直接使う場合:

```bash
python src/prefill_only.py --lead-id 1100 --queue results/review_queue_YYYYMMDD.csv
```

このCLIは以下を実行します（送信はしません）:

- review_queue から対象行を取得（未指定時は最新ファイル）
- `final_step_url`（なければ `contact_url`）を headed Playwright で開く
- best-effortで入力し、`before_submit_or_confirm` で停止
- `screenshots/YYYYMMDD/{id}_01..04_*.png` を保存
- 1行JSONを stdout に出力（`status` / `reason` / `stop_state` / `stopped_at` など）
- `results/staff_actions_YYYYMMDD.csv` に `action=open_prefill` を追記（prefill helperログ）
- DOM変更などで保存済みセレクタが無効な場合は `prepared_review_needed` に降格し、`contact_url` 側へフォールバックして停止

キュー指定:

- 既定では最新の `results/review_queue_*.csv` を自動使用
- 環境変数で上書き: `REVIEW_QUEUE_PATH`

運用フロー（スタッフ向け）:

1. 左でフィルタ（表示モード / ステータス / タグ / 検索）
2. 中央一覧で対象リードを選択
3. 右で `Run Playwright Prefill (NO submit)` を実行
4. 必要に応じて `Open Demo / Open Original / Open Contact URL`
5. 下部スクリーンショットを確認
6. `Mark Sent` または `Mark Skip` で記録（必要なら `Undo last action`）

注意:

- ローカル運用専用です（外部公開しない）
- 実リード/結果CSV/スクリーンショットは機微情報を含むため、リポジトリは非公開運用を推奨

## SEMI_AUTO Workflow

1. サイトへ移動して問い合わせフォームを検出
2. フォーム入力
3. 送信ボタンを検出して画面内へスクロール + 強調表示（枠線）
4. スクリーンショット保存
5. 最終送信前で停止し `prepared_*` を記録
6. オペレーター待機（ENTER or `debug_pause=true` の場合 `page.pause()`）

出力例:

- `screenshots/YYYYMMDD/{salon_id}_01_before_fill.png`
- `screenshots/YYYYMMDD/{salon_id}_02_after_fill.png`
- `screenshots/YYYYMMDD/{salon_id}_03_before_submit_or_confirm.png`
- （確認ページあり）`screenshots/YYYYMMDD/{salon_id}_04_on_confirmation_page.png`

`results/review_queue_YYYYMMDD.csv` に手動確認キューが追記されます（同日同IDは重複追加しません）。

ログには停止位置を明示します:

- `SEMI_AUTO: stopped on confirmation page`
- `SEMI_AUTO: stopped before submit`

停止状態（`stop_state`）:

- `confirmation`
- `submit_button`
- `form_filled`
- `unknown`

補足:

- `SEMI_AUTO` ではブラウザを自動クローズしません。
- 実行終了時にオペレーターの明示操作（ENTER）を受け取ってから閉じます。

## Skip Policy: Maximize Recall

このシステムは **高リコール（回収率）** を精度より優先します:

- **スキップは3理由のみ**: `skipped_login`, `skipped_bot_protection`, `skipped_dead_site`
- **それ以外は全て prepared_review_needed**: 人間のレビューで判断
- **DETECT_ONLY は積極的に回収**: 不確実な候補も含む
- **Staff Review App で false positive を処理**: 自動スキップよりUXで解決

方針: 「100件の false positive をレビューするほうが、1件の true lead を逃すより良い」

ステータスフロー:

```
Contact page探索
  |
ログイン必須? -> skipped_login
Bot保護検出? -> skipped_bot_protection
サイト不通? -> skipped_dead_site
  |
フォーム明確に検出? -> prepared_full / prepared_partial / prepared_external
  |
不確実/タイムアウト/欠損? -> prepared_review_needed (証拠付き)
```

Evidence例:
- `no_obvious_contact_page_but_collected_5_candidate_links`
- `timeout_during_form_detection_after_30s`
- `address_fields_detected_needs_manual_completion`
- `required_fields_unfilled_needs_review`

## DETECT_ONLY Workflow

1. サイトへ移動して問い合わせ候補ページを検出
2. コンタクトページを表示して `01_contact_page` スクリーンショット保存
3. フォーム存在を緩和条件で判定
4. フォームありなら `prepared/form_detected` で review queue に追加（同日同IDは重複なし）
5. フォームなしなら `prepared_review_needed`（ログインフォームは `skipped_login`）

補足:

- `DETECT_ONLY` では入力・確認クリック・送信クリックを行いません
- フォーム検出時のみ `02_form_detected` スクリーンショットを保存します
- 実行後に `results/leads_prepared_YYYYMMDD.csv` を生成し、元リードに `prepared` 列を付けた手動送信向け一覧を出力します

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
- `aidnet_domain_list_path`（既定: `data/エイドネット_ドメインリスト - リスト_日本語学校.csv`）
  - 起動時にCSVの`URL`列を読み取り、ドメインを`blocklist_domains.txt`へ自動同期

bot保護検知（CAPTCHA / Cloudflare / verify human / HTTP 403/429）時:

- `status=skipped`, `reason=bot_protection`
- ドメインを7日ブロック（cooldown）
- 30-90秒ランダム待機して次へ

## Portal判定 (domain + URL keyword)

`config/settings.json` に任意で以下を設定できます（未設定時は安全側デフォルトを使用）:

- `skip_domains`
- `skip_url_keywords`
- `hard_skip_portals`（既定 `false`）

推奨値:

```json
{
  "skip_domains": [
    "hotpepper.jp",
    "beauty.hotpepper.jp",
    "ekiten.jp",
    "my-best.com"
  ],
  "skip_url_keywords": [
    "hotpepper",
    "ekiten",
    "my-best"
  ],
  "hard_skip_portals": false
}
```

判定ルール:

- ドメイン一致: `domain == d` または `domain.endswith("." + d)`
- URLキーワード一致: URL文字列にキーワードが含まれる
- 既定では**スキップせず** `exclude_clear:...` の証跡タグを付与して探索を継続
- `hard_skip_portals=true` のときのみ、従来どおり早期 `skipped` にできます

## Aggressive Skip (throughput優先)

不完全なリードを素早く処理するため、以下の設定を使えます（既定は探索優先）:

- `aggressive_skip: false`
- `skip_on_missing_demo_url: false`
- `max_contact_page_seconds: 25`
- `max_form_detect_seconds: 20`
- `max_fill_seconds: 20`
- `max_contact_pages_to_try: 8`
- `allow_querystring_urls: true`
- `contact_link_text_keywords: [...]`
- `skip_if_new_tabs_or_downloads: true`
- `skip_if_requires_login: true`
- `skip_if_iframe_only_form: false`
- `skip_if_too_many_required_fields: 10`
- `skip_if_unfilled_required_fields: true`
- `skip_if_submit_not_found: true`

主な動作:

- デモURL欠落/店名欠落/URL欠落を事前スキップ
- contact探索/フォーム検出/入力を wall-clock timeout で打ち切り
- ログイン要求・予約専用・popup/download・required過多を早期スキップ
- 住所系フィールド（郵便番号/都道府県/住所等）が必須なら `requires_address` でスキップ
- `fill_incomplete` / `no_submit_button` / 一部例外を `skipped` として継続処理

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

ステータス体系（拡張）:

- `prepared_full`
- `prepared_partial`
- `prepared_external`
- `prepared_review_needed`
- `skipped_login`
- `skipped_bot_protection`
- `skipped_dead_site`

`prepared_*` は運用上すべて「Prepared」として集計されます。

運用レポートCLI:

```bash
python src/ops_report.py
python src/ops_report.py --json
```

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

## DETECT_ONLY 高リコール運用

`DETECT_ONLY` は「フォームがありそうなURLを幅広く回収」する用途です。  
false positive は review app 側で捌く前提で、回収率を優先します。

主な設定（`config/settings.json`）:

- `mode: "DETECT_ONLY"`
- `max_contact_candidate_links: 80`
- `record_blocked_as_prepared: false/true`

挙動:

- 優先リンク（お問い合わせ/contact/form/予約/reserve/booking/inquiry/ご相談/申し込み/entry）を優先探索
- `/sitemap` / `/sitemap.xml` も候補探索
- 外部フォーム（Google Forms等）を `prepared` として採用
- `record_blocked_as_prepared=true` の場合、bot保護/ログイン必須も `prepared` で review queue に残す
- `review_queue_YYYYMMDD.csv` に `reason` と `evidence` を記録
- summary に探索指標（`pages_visited` / `candidate_contact_links_found` / `skipped_before_exploration`）を記録

## SEMI_AUTO 安全ポリシー

- 住所系必須（郵便番号/都道府県/住所等）は必ず `skipped: requires_address`
- 必須未充足/検出不安定時は送信せず `prepared` or `skipped` で停止
- 最終送信は `SEMI_AUTO` では常に実行しない
- メッセージは送信前に `sanitize_message_for_legacy_encodings()` を通し、
  罫線や特殊ダッシュなど文字化けしやすい文字を安全側に正規化

## Review Dashboard 実運用

起動:

```bash
python app.py
# または
streamlit run src/staff_review_app.py
```

アクションログ:

- `results/operator_actions_YYYYMMDD.csv`（append-only）
- 主な action:
  - `open_prefill`
  - `mark_sent`
  - `mark_skip`
  - `open_demo` / `open_original` / `open_contact`

使い方:

1. 左サイドバーで絞り込み（表示モード/ステータス/タグ/検索）
2. リードを選択
3. `Run Playwright Prefill (no submit)` を実行
4. スクショ確認
5. `Mark Sent` または `Mark Skip` で記録

## Git事故対処（機密ファイル）

注意: `git rm` だけでは履歴から消えません。公開済み履歴から除去が必要です。

例（`git filter-repo`）:

```bash
pip install git-filter-repo
git filter-repo --path data/leads.csv --path-glob "data/leads*.csv" --path data/state.json --path data/submission_ledger.csv --path results --path screenshots --path-glob ".~lock.*" --invert-paths
git push --force --all
git push --force --tags
```

運用推奨:

- このリポジトリは private で運用
- `.gitignore` に `data/`, `results/`, `screenshots/`, `*.xlsx`, `.~lock.*` を含める

## おすすめ運用コマンド集

```bash
# 高リコール検出（100件）
python src/main.py --mode DETECT_ONLY --limit 100

# 安全なSEMI_AUTO（100件、非対話）
python src/main.py --mode SEMI_AUTO --limit 100

# 目視確認用SEMI_AUTO（5件、対話あり）
python src/main.py --mode SEMI_AUTO --semi-auto-verify --semi-auto-limit 5

# review app 起動
python app.py
```
