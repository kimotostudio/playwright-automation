from __future__ import annotations

from pathlib import Path

import src.ui_utils as ui_utils


class _DummyResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_prefill_subprocess_skips_non_file_queue(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        return _DummyResult(returncode=2, stdout='{"status":"skipped"}\n', stderr="")

    monkeypatch.setattr(ui_utils.subprocess, "run", _fake_run)

    code, _out, _err, payload = ui_utils.run_prefill_subprocess(
        salon_id="aidnet-0001",
        review_queue_path=Path(""),
        final_url="https://example.com/contact",
        keep_open=False,
    )

    cmd = captured["cmd"]
    assert "--queue" not in cmd
    assert "--final-url" in cmd
    assert "--no-keep-open" in cmd
    assert code == 2
    assert payload.get("status") == "skipped"


def test_run_prefill_subprocess_includes_queue_when_file(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}
    queue_path = tmp_path / "review_queue_20260221.csv"
    queue_path.write_text("salon_id\n1\n", encoding="utf-8")

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        return _DummyResult(returncode=0, stdout='{"status":"prepared_full"}\n', stderr="")

    monkeypatch.setattr(ui_utils.subprocess, "run", _fake_run)

    code, _out, _err, payload = ui_utils.run_prefill_subprocess(
        salon_id="1",
        review_queue_path=queue_path,
        final_url="",
        keep_open=True,
    )

    cmd = captured["cmd"]
    assert "--queue" in cmd
    idx = cmd.index("--queue")
    assert cmd[idx + 1] == str(queue_path)
    assert "--keep-open" in cmd
    assert code == 0
    assert payload.get("status") == "prepared_full"


def test_run_detection_subprocess_skips_non_file_queue(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        return _DummyResult(returncode=0, stdout='{"status":"prepared"}\n', stderr="")

    monkeypatch.setattr(ui_utils.subprocess, "run", _fake_run)

    _code, _out, _err, _payload = ui_utils.run_detection_subprocess(
        salon_id="aidnet-0002",
        review_queue_path=Path(""),
        base_url="https://example.com",
        final_url="https://example.com/contact",
    )

    cmd = captured["cmd"]
    assert "--queue" not in cmd
    assert "--detect-only" in cmd
    assert "--no-keep-open" in cmd
    assert "--no-wait" in cmd
