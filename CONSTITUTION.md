# CONSTITUTION - KIMOTO STUDIO

## 0. Mission Statement

このレポジトリは **KIMOTO STUDIO** の事業を支える技術基盤である。

**目的:**
- 個人事業主向けWeb改修・新規制作の営業活動を支援
- リード収集・デモ生成・提案書作成を自動化
- 統計とテンプレートで再現性を最大化

**Core Value: 勾配を掴む**
- 営業は確率ゲーム。微分＝改善の方向。
- 統計で判断、型で高速化、デモで信頼獲得。

---

## 1. Non-Negotiables

### 1.1 Security First
**絶対にコミットしないもの:**
- API keys (OpenAI, Anthropic, etc.)
- Service account JSON
- `.env` files
- Personal info (emails, phone numbers)
- Client data (real names, URLs without permission)

**Always:**
- Use `.env` for secrets
- Add sensitive patterns to `.gitignore`
- Use placeholder data in examples

### 1.2 Stability Over Features
- メインワークフローは常に動作する状態を維持
- 破壊的変更は禁止（必要な場合は明示的なマイグレーションパス提供）
- 既存の出力形式（CSV/JSON）は後方互換性を保つ

### 1.3 Explainability
- 全ての変更は「なぜ・何を・どう確認」が3行で説明できること
- コードよりも意図を残す（コメント・README・commit message）

---

## 2. KIMOTO Principles

### 2.1 勾配駆動（Gradient-Driven）
> 「次の一手は、成約率の微分が最大になる方向」

**実装への影響:**
- A/Bテスト可能な設計
- 統計ログを標準装備
- 数値で改善を測定できる仕組み

**例:**
- lead-finder: 反応率・成約率を都道府県×業種で計測
- demo-generator: デモ閲覧→商談率を記録

### 2.2 統計ファースト（Stats-First）
> 「感覚ではなく数字で判断」

**実装への影響:**
- CSV出力には統計カラム標準装備
- パイプラインの各段階で件数ログ
- UIには必ず集計サマリーを表示

```python
stats = {
    'total': 500,
    'filtered': 200,
    'final': 180,
    'conversion_rate': 0.36,
}
```

### 2.3 デモ先行（Demo-First）
> 「見積もり前に完成イメージを見せる」

**実装への影響:**
- 見積もり段階でデモサイト生成
- テンプレートは即座にプレビュー可能
- 出力は必ずクライアント共有可能な形式

### 2.4 型化（Template-Based）
> 「繰り返すものは全て型にする」

**実装への影響:**
- コードは関数/クラスで再利用
- 営業トークもテンプレート化
- UIパターンも統一（Bootstrap 5 + ProSaaS style）

---

## 3. Priorities

判断に迷ったら、この順で考える:

1. **Reliability（信頼性）** - 動くこと > 速いこと
2. **Speed（速度）** - 1時間でできる > 1日かける
3. **Maintainability（保守性）** - 3ヶ月後の自分が理解できる
4. **Elegance（美しさ）** - 最後に考える

**例:**
- 「完璧なアーキテクチャ」より「動くスクリプト」
- 「汎用的な設計」より「今の問題を解決」
- リファクタは"具体的な利益"がある時だけ

---

## 4. What NOT to Do

### 4.1 技術負債の先送り禁止
- 動いたら即座に整理（コメント・関数化・テスト）

### 4.2 過度な抽象化禁止
- 3回繰り返したら型にする（それまでは直書きOK）

### 4.3 依存関係の無計画な追加禁止
- 標準ライブラリ or 必須の場合のみ

### 4.4 出力形式の破壊的変更禁止
- CSVカラム名・JSONキー名の変更禁止
- 新カラム追加 or バージョニングで対応

---

## 5. Output Contracts

### 5.1 CSV出力
- UTF-8 with BOM（Excel互換）
- 日本語ヘッダー
- 必須カラム: `店舗名`, `URL`, `スコア`, `弱さスコア`, `事業規模`

### 5.2 ログ出力
```python
logger.info(f"[STAGE] Description: {count} items")
logger.error(f"[ERROR] {url}: {error_message}")
```

### 5.3 統計出力
全ツールは実行後に統計を返す:
```python
stats = {
    'total': int,
    'success': int,
    'failed': int,
    'success_rate': float,
}
```

---

## 6. Development Workflow

### 6.1 Before You Code
1. Read CONSTITUTION.md (this file)
2. Read AI_GUIDE.md (AI-specific rules)
3. Identify minimal change
4. Plan verification

### 6.2 Commit Rules
Format: `[TYPE] Brief description`

Types:
- `[ADD]` - 新機能
- `[FIX]` - バグ修正
- `[UPDATE]` - 既存機能改善
- `[REFACTOR]` - リファクタ
- `[DOCS]` - ドキュメント
- `[CONFIG]` - 設定変更

### 6.3 Branch Strategy
- `main` - 本番相当（常に動く状態）
- `feature/xxx` - 機能追加
- `fix/xxx` - バグ修正

### 6.4 Testing
```bash
python -m pytest tests/ -v
```
全テスト通過が必須。手動テストも実施。

---

## 7. File Structure Standards

```
project_root/
├── CONSTITUTION.md          # このファイル
├── AI_GUIDE.md             # AI向けガイド
├── CLAUDE.md               # Claude Code向けガイド
├── README.md               # プロジェクト概要
├── .env.example            # 環境変数テンプレート
├── .gitignore
├── requirements.txt
├── src/                    # コアロジック
├── config/                 # 設定ファイル
├── web_app/                # Webアプリ
│   ├── app.py
│   ├── templates/
│   └── static/
├── tests/                  # テスト
├── output/                 # 出力先（.gitignore）
└── logs/                   # ログ（.gitignore）
```

### Naming Conventions

- Files: `snake_case.py`
- Functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`

---

## 8. Quality Checklist

変更をコミットする前に確認:

- [ ] 動作確認済み（テスト通過）
- [ ] README/docs更新済み（必要な場合）
- [ ] secrets漏れチェック
- [ ] commit messageが説明的
- [ ] 破壊的変更の場合、マイグレーション手順記載

---

## 9. Decision Framework

迷ったら:

1. **これは営業成約率を上げるか？** → YES: 優先度高
2. **これはオペレーション時間を減らすか？** → YES: 優先度中
3. **これは3ヶ月後の自分を助けるか？** → YES: 優先度低
4. 上記全てNO → やらない

---

## 10. Revision History

- v1.0 (2026-02-03): Initial constitution
