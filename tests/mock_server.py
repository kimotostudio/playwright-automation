"""Deterministic local mock contact form server for SEMI_AUTO testing."""

from __future__ import annotations

import argparse
from html import escape

from flask import Flask, request

app = Flask(__name__)


def _layout(title: str, body: str) -> str:
    return f"""
    <html lang="ja">
      <head><meta charset="utf-8"><title>{title}</title></head>
      <body>
        {body}
      </body>
    </html>
    """


@app.get("/")
def home() -> str:
    body = """
    <h1>Mock Salon Site</h1>
    <ul>
      <li><a href="/contact_single">お問い合わせ（単一名前フィールド）</a></li>
      <li><a href="/contact_split">お問い合わせ（姓/名分割フィールド）</a></li>
    </ul>
    """
    return _layout("Mock Home", body)


@app.get("/contact")
def contact_alias() -> str:
    return contact_single()


@app.get("/contact_single")
def contact_single() -> str:
    body = """
    <h1>お問い合わせフォーム（単一名前）</h1>
    <form action="/confirm/single" method="post">
      <label for="name">お名前 <span>*</span> <span>必須</span></label>
      <input id="name" name="name" type="text" required aria-required="true" placeholder="お名前" />

      <label for="company">会社名 / 屋号</label>
      <input id="company" name="company" type="text" placeholder="会社名" />

      <label for="email">メールアドレス <span>*</span> <span>必須</span></label>
      <input id="email" name="email" type="email" required aria-required="true" placeholder="example@example.com" />

      <label for="phone">電話番号 <span>*</span> <span>必須</span></label>
      <input id="phone" name="phone" type="tel" required aria-required="true" placeholder="090-1234-5678" />

      <label for="message">お問い合わせ内容 <span>*</span> <span>必須</span></label>
      <textarea id="message" name="message" required aria-required="true" placeholder="ご相談内容をご記入ください"></textarea>

      <label for="category">お問い合わせ種別 <span>*</span> <span>必須</span></label>
      <select id="category" name="category" required aria-required="true">
        <option value="">選択してください</option>
        <option value="general">一般お問い合わせ</option>
        <option value="other">その他</option>
      </select>

      <label for="agree">個人情報保護方針に同意 <span>*</span> <span>必須</span></label>
      <input id="agree" name="agree" type="checkbox" required aria-required="true" />

      <button type="submit">確認画面へ</button>
    </form>
    """
    return _layout("Mock Contact Single", body)


@app.get("/contact_split")
def contact_split() -> str:
    body = """
    <h1>お問い合わせフォーム（姓/名分割）</h1>
    <form action="/confirm/split" method="post">
      <label for="sei">姓 <span>*</span> <span>必須</span></label>
      <input id="sei" name="sei" type="text" required aria-required="true" placeholder="姓" />

      <label for="mei">名 <span>*</span> <span>必須</span></label>
      <input id="mei" name="mei" type="text" required aria-required="true" placeholder="名" />

      <label for="sei_kana">姓フリガナ <span>*</span> <span>必須</span></label>
      <input id="sei_kana" name="sei-kana" type="text" required aria-required="true" placeholder="セイ" />

      <label for="mei_kana">名フリガナ <span>*</span> <span>必須</span></label>
      <input id="mei_kana" name="mei-kana" type="text" required aria-required="true" placeholder="メイ" />

      <label for="company2">会社名 / 屋号</label>
      <input id="company2" name="company" type="text" placeholder="会社名" />

      <label for="email2">メールアドレス <span>*</span> <span>必須</span></label>
      <input id="email2" name="email" type="email" required aria-required="true" placeholder="example@example.com" />

      <label for="phone2">電話番号</label>
      <input id="phone2" name="phone" type="tel" placeholder="090-1234-5678" />

      <label for="message2">ご相談内容 <span>*</span> <span>必須</span></label>
      <textarea id="message2" name="message" required aria-required="true" placeholder="ご相談内容をご記入ください"></textarea>

      <label for="agree2">個人情報保護方針に同意 <span>*</span> <span>必須</span></label>
      <input id="agree2" name="agree" type="checkbox" required aria-required="true" />

      <button type="submit">内容確認</button>
    </form>
    """
    return _layout("Mock Contact Split", body)


@app.post("/confirm/<variant>")
def confirm(variant: str) -> str:
    items = []
    for key in ["name", "sei", "mei", "sei-kana", "mei-kana", "company", "email", "phone", "message", "category"]:
        value = escape(request.form.get(key, ""))
        items.append(f"<li>{escape(key)}: {value}</li>")

    hidden_inputs = []
    for key, value in request.form.items():
        hidden_inputs.append(f"<input type='hidden' name='{escape(key)}' value='{escape(value)}' />")

    body = f"""
    <h1>入力内容の確認</h1>
    <div>確認画面です。最終送信ボタンは次です。</div>
    <ul>{''.join(items)}</ul>
    <form action="/submit/{escape(variant)}" method="post">
      {''.join(hidden_inputs)}
      <button type="submit">この内容で送信</button>
    </form>
    """
    return _layout("Mock Confirm", body)


@app.post("/submit/<variant>")
def submit(variant: str) -> str:
    body = f"""
    <h1>送信しました</h1>
    <p>variant: {escape(variant)}</p>
    <p>これはモックサーバーの成功ページです。</p>
    """
    return _layout("Mock Submit", body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local mock contact form server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
