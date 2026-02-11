"""Deterministic local mock contact form server for SEMI_AUTO testing."""

from __future__ import annotations

import argparse
from html import escape

from flask import Flask, request

app = Flask(__name__)


@app.get("/")
def home() -> str:
    return """
    <html lang="ja">
      <head><meta charset="utf-8"><title>Mock Home</title></head>
      <body>
        <h1>Mock Salon Site</h1>
        <a href="/contact">お問い合わせ</a>
      </body>
    </html>
    """


@app.get("/contact")
def contact() -> str:
    return """
    <html lang="ja">
      <head><meta charset="utf-8"><title>Mock Contact</title></head>
      <body>
        <h1>お問い合わせフォーム</h1>
        <form action="/confirm" method="post">
          <label for="name">お名前 <span>*</span> <span>必須</span></label>
          <input id="name" name="name" type="text" required aria-required="true" placeholder="お名前" />

          <label for="email">メールアドレス <span>*</span> <span>必須</span></label>
          <input id="email" name="email" type="email" required aria-required="true" placeholder="example@example.com" />

          <label for="phone">電話番号 <span>*</span> <span>必須</span></label>
          <input id="phone" name="phone" type="tel" required aria-required="true" placeholder="090-1234-5678" />

          <label for="message">お問い合わせ内容 <span>*</span> <span>必須</span></label>
          <textarea id="message" name="message" required aria-required="true" placeholder="ご相談内容をご記入ください"></textarea>

          <label for="agree">個人情報保護方針に同意 <span>必須</span></label>
          <input id="agree" name="agree" type="checkbox" required aria-required="true" />

          <button type="submit">確認画面へ</button>
        </form>
      </body>
    </html>
    """


@app.post("/confirm")
def confirm() -> str:
    name = escape(request.form.get("name", ""))
    email = escape(request.form.get("email", ""))
    phone = escape(request.form.get("phone", ""))
    message = escape(request.form.get("message", ""))
    return f"""
    <html lang="ja">
      <head><meta charset="utf-8"><title>Mock Confirm</title></head>
      <body>
        <h1>入力内容の確認</h1>
        <div>確認画面です。最終送信ボタンは次です。</div>
        <ul>
          <li>name: {name}</li>
          <li>email: {email}</li>
          <li>phone: {phone}</li>
          <li>message: {message}</li>
        </ul>
        <form action="/submit" method="post">
          <input type="hidden" name="name" value="{name}" />
          <input type="hidden" name="email" value="{email}" />
          <input type="hidden" name="phone" value="{phone}" />
          <input type="hidden" name="message" value="{message}" />
          <button type="submit">この内容で送信</button>
        </form>
      </body>
    </html>
    """


@app.post("/submit")
def submit() -> str:
    return """
    <html lang="ja">
      <head><meta charset="utf-8"><title>Mock Submit</title></head>
      <body>
        <h1>送信しました</h1>
        <p>これはモックサーバーの成功画面です。</p>
      </body>
    </html>
    """


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local mock contact form server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
