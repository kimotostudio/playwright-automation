from __future__ import annotations

import unittest

try:
    from playwright.async_api import async_playwright
except (ImportError, ModuleNotFoundError):  # pragma: no cover - exercised only without optional dependency.
    async_playwright = None

from src.form_detector import FormDetector
from src.main import collect_no_field_form_context


@unittest.skipIf(async_playwright is None, "playwright is not installed")
class DynamicFormDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on local browser install.
            await self.playwright.stop()
            self.skipTest(f"chromium is unavailable: {exc}")
        self.page = await self.browser.new_page()

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.playwright.stop()

    async def test_nearby_labels_do_not_map_all_fields_to_first_select(self) -> None:
        await self.page.set_content(
            """
            <html><body>
              <form>
                <div class="row">
                  <p>お問い合わせの種類 <span>必須</span></p>
                  <select id="kind"><option>選択してください</option><option>お問い合わせ</option></select>
                </div>
                <div class="row">
                  <p>お名前 <span>必須</span></p>
                  <input id="name" type="text">
                </div>
                <div class="row">
                  <p>お電話番号 <span>必須</span></p>
                  <input id="phone" type="text">
                </div>
                <div class="row">
                  <p>メールアドレス <span>必須</span></p>
                  <input id="email" type="email">
                </div>
                <div class="row">
                  <p>お問い合わせ内容</p>
                  <textarea id="body"></textarea>
                </div>
              </form>
            </body></html>
            """
        )
        detector = FormDetector(
            self.page,
            {
                "display_name": "Test Sender",
                "email": "sender@example.com",
                "phone": "090-0000-0000",
            },
        )

        fields, form_map = await detector.detect_form_fields()

        self.assertIn("name", fields)
        self.assertIn("email", fields)
        self.assertIn("phone", fields)
        self.assertIn("message", fields)
        self.assertNotIn("name_mei", fields)
        for field_type in ("name", "email", "phone", "message"):
            tag_name = await fields[field_type].evaluate("el => el.tagName.toLowerCase()")
            self.assertNotEqual(tag_name, "select", form_map[field_type])

        fill_ok, stats = await detector.fill_form(fields, "Hello from a local mock test", "Subject")

        self.assertTrue(fill_ok, stats)
        self.assertGreaterEqual(stats["filled"], 4)
        self.assertEqual(await self.page.locator("#name").input_value(), "Test Sender")
        self.assertEqual(await self.page.locator("#email").input_value(), "sender@example.com")
        self.assertEqual(await self.page.locator("#phone").input_value(), "090-0000-0000")
        self.assertIn("local mock test", await self.page.locator("#body").input_value())

    async def test_embedded_reservation_widget_context_is_reportable(self) -> None:
        await self.page.set_content(
            """
            <html><body>
              <h1>reservation</h1>
              <iframe src="about:blank" title="RESERVA reservation widget"></iframe>
              <a href="/contact">お問い合わせ</a>
            </body></html>
            """
        )

        context = await collect_no_field_form_context(self.page)

        self.assertEqual(context["iframe_count"], 1)
        self.assertIn("reserva", context["providers"])
        self.assertTrue(context["contact_or_reservation_links"])
        self.assertIn("iframes=1", context["evidence"])


if __name__ == "__main__":
    unittest.main()
