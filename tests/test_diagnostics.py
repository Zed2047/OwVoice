from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import backend.diagnostics as diagnostics
from backend.diagnostics import create_diagnostic_report, redact_text
from backend.logging_support import new_error_reference, rotate_log
from backend.error_reporting import report_user_error


def test_rotate_log_keeps_only_configured_history(tmp_path: Path) -> None:
    log = tmp_path / "backend.log"
    unrelated = tmp_path / "user.wav"
    unrelated.write_bytes(b"audio")
    for index in range(4):
        log.write_bytes((str(index) * 20).encode())
        rotate_log(log, max_bytes=10, backup_count=2)

    assert not log.exists()
    assert (tmp_path / "backend.log.1").is_file()
    assert (tmp_path / "backend.log.2").is_file()
    assert not (tmp_path / "backend.log.3").exists()
    assert unrelated.read_bytes() == b"audio"


def test_error_reference_contains_stable_category_and_unique_id() -> None:
    reference = new_error_reference("MODEL-201")

    assert reference.startswith("MODEL-201 · ")
    assert len(reference.rsplit(" ", 1)[-1]) == 8


def test_user_error_has_reason_impact_advice_and_matching_log(tmp_path: Path) -> None:
    message = report_user_error(
        tmp_path,
        code="TRAIN-301",
        module="training",
        reason="磁盘空间不足",
        impact="训练尚未开始，素材仍保留。",
        advice="释放空间后重新开始当前阶段。",
        exception=RuntimeError("technical detail"),
    )

    assert all(label in message for label in ("原因：", "影响：", "建议：", "错误编号：TRAIN-301 ·"))
    error_id = message.rsplit("错误编号：", 1)[-1]
    log = (tmp_path / "logs" / "training.error.log").read_text(encoding="utf-8")
    assert error_id in log
    assert "technical detail" in log


def test_redaction_removes_paths_url_credentials_and_tokens(tmp_path: Path) -> None:
    text = (
        f"root={tmp_path} user=C:\\Users\\Alice\\secret "
        "url=https://name:pass@example.com/file?token=abc ACCESS_TOKEN=very-secret"
    )

    redacted = redact_text(text, project_root=tmp_path, user_dir=Path("C:/Users/Alice"))

    assert str(tmp_path) not in redacted
    assert "Alice" not in redacted
    assert "pass" not in redacted
    assert "very-secret" not in redacted
    assert "<OWVOICE_DIR>" in redacted


def test_diagnostic_zip_excludes_user_content_and_keeps_bounded_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "backend.error.log").write_text(
        "x" * 300_000 + "\nmodel=private-name text=private transcript ACCESS_TOKEN=secret",
        encoding="utf-8",
    )
    (logs / "private.wav").write_bytes(b"audio-secret")
    model = tmp_path / "data" / "models" / "私有角色"
    model.mkdir(parents=True)
    (model / "model.json").write_text('{"id":"private-name"}', encoding="utf-8")
    job = tmp_path / "data" / "training" / "jobs" / "job-1"
    job.mkdir(parents=True)
    (job / "job.json").write_text('{"status":"failed","text":"private transcript"}', encoding="utf-8")

    report = create_diagnostic_report(tmp_path, version="0.2.0")

    with zipfile.ZipFile(report) as archive:
        names = archive.namelist()
        content = b"\n".join(archive.read(name) for name in names)
        summary = json.loads(archive.read("diagnostic-summary.json"))
    assert names == ["README.txt", "diagnostic-summary.json", "logs/backend.error.log"]
    assert b"audio-secret" not in content
    assert b"private transcript" not in content
    assert b"private-name" not in content
    assert b"ACCESS_TOKEN=secret" not in content
    bytes_included = summary["logs"][0]["bytes_included"]
    assert bytes_included > 0
    assert bytes_included <= 200_000


def test_diagnostic_ui_is_explicit_and_log_copy_has_both_actions() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(encoding="utf-8-sig")

    assert 'QPushButton("生成诊断报告")' in source
    assert 'requests.post(f"{self.api_url()}/api/diagnostics"' in source
    assert "报告不会自动上传" in source
    assert 'QPushButton("复制所选")' in source
    assert 'QPushButton("复制全部")' in source
    assert 'selectedText().replace("\\u2029", "\\n")' in source


def test_diagnostic_replace_failure_leaves_no_formal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diagnostics.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("模拟失败")),
    )

    with pytest.raises(OSError, match="模拟失败"):
        create_diagnostic_report(tmp_path, version="0.2.0")

    output = tmp_path / "logs" / "diagnostics"
    assert list(output.glob("OwVoice-diagnostics-*.zip")) == []
    assert list(output.glob(".diagnostics-*.tmp")) == []
    assert "requests" not in Path(diagnostics.__file__).read_text(encoding="utf-8")
