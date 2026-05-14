from __future__ import annotations

import sys
import types
import unittest


try:
    import playwright.async_api  # noqa: F401
except ModuleNotFoundError:
    playwright_mod = types.ModuleType("playwright")
    async_api_mod = types.ModuleType("playwright.async_api")

    class _DummyPlaywrightType:
        pass

    async_api_mod.Locator = _DummyPlaywrightType
    async_api_mod.Page = _DummyPlaywrightType
    async_api_mod.TimeoutError = TimeoutError
    sys.modules["playwright"] = playwright_mod
    sys.modules["playwright.async_api"] = async_api_mod


from src.form_detector import (  # noqa: E402
    analyze_static_form_html,
    classify_submit_text,
    detect_sales_prohibited_text,
)


class StaticFormDetectorTests(unittest.TestCase):
    def assert_has_fields(self, html: str, *field_types: str) -> None:
        analysis = analyze_static_form_html(html)
        for field_type in field_types:
            with self.subTest(field_type=field_type):
                self.assertGreaterEqual(analysis["fields"].get(field_type, 0), 1)

    def test_japanese_basic_form_fields(self) -> None:
        html = """
        <form>
          <label for="name">お名前</label><input id="name" name="your-name">
          <label for="email">メールアドレス</label><input id="email" type="email">
          <label for="tel">電話番号</label><input id="tel" type="tel">
          <label for="subject">件名</label><input id="subject">
          <label for="message">お問い合わせ内容</label><textarea id="message"></textarea>
          <button>送信</button>
        </form>
        """
        analysis = analyze_static_form_html(html)
        self.assert_has_fields(html, "name", "email", "phone", "subject", "message")
        self.assertEqual(analysis["final_submit_button_count"], 1)

    def test_english_basic_form_fields(self) -> None:
        html = """
        <form>
          <label for="name">Full Name</label><input id="name">
          <label for="email">Email</label><input id="email">
          <label for="phone">Phone Number</label><input id="phone">
          <label for="subject">Subject</label><input id="subject">
          <label for="message">Message</label><textarea id="message"></textarea>
          <button>Submit</button>
        </form>
        """
        analysis = analyze_static_form_html(html)
        self.assert_has_fields(html, "name", "email", "phone", "subject", "message")
        self.assertEqual(analysis["final_submit_button_count"], 1)

    def test_placeholder_based_form_fields(self) -> None:
        html = """
        <form>
          <input placeholder="お名前">
          <input placeholder="Email">
          <input placeholder="Phone Number">
          <input placeholder="Subject">
          <textarea placeholder="Comments"></textarea>
          <input type="button" value="Confirm">
        </form>
        """
        analysis = analyze_static_form_html(html)
        self.assert_has_fields(html, "name", "email", "phone", "subject", "message")
        self.assertEqual(analysis["confirm_button_count"], 1)

    def test_contenteditable_and_role_textbox_message_fields(self) -> None:
        html = """
        <form>
          <input name="your-name">
          <input name="your-email">
          <div contenteditable="true" aria-label="Message"></div>
          <div role="textbox" aria-label="Comments"></div>
        </form>
        """
        analysis = analyze_static_form_html(html)
        self.assertGreaterEqual(analysis["fields"].get("message", 0), 2)

    def test_confirmation_and_final_submit_text_classification(self) -> None:
        for text in ["確認", "内容確認", "Confirm", "Next"]:
            with self.subTest(text=text):
                self.assertEqual(classify_submit_text(text), "confirm")
        for text in ["送信", "Submit", "Send"]:
            with self.subTest(text=text):
                self.assertEqual(classify_submit_text(text), "submit")

    def test_captcha_and_sales_prohibited_detection(self) -> None:
        html = """
        <form>
          <textarea name="message"></textarea>
          <iframe src="https://captcha.example/widget"></iframe>
          <p>営業お断り・セールス禁止</p>
        </form>
        """
        analysis = analyze_static_form_html(html)
        self.assertTrue(analysis["has_captcha"])
        self.assertTrue(analysis["sales_prohibited"])
        self.assertTrue(detect_sales_prohibited_text("No solicitation please"))


if __name__ == "__main__":
    unittest.main()
