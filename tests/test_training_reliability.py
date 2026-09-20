from __future__ import annotations

import json
import threading
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.atomic_json as atomic_json
import frontend.app as frontend_app
from backend.training import TrainingError, TrainingManager
from backend.model_catalog import InstalledModel, save_installed_models


def _manager(root: Path) -> TrainingManager:
    manager = object.__new__(TrainingManager)
    manager.project_dir = root.resolve()
    manager.gsv_root = root / "GPT-SoVITS"
    manager.jobs_root = root / "data" / "training" / "jobs"
    manager.jobs_root.mkdir(parents=True)
    manager._lock = threading.RLock()
    return manager


def test_job_save_is_atomic_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    path = manager.jobs_root / "job-1" / "job.json"
    path.parent.mkdir()
    original = b'{"id":"job-1","status":"draft","files":[]}\n'
    path.write_bytes(original)

    monkeypatch.setattr(
        atomic_json.os,
        "replace",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("模拟替换失败")),
    )

    with pytest.raises(OSError, match="模拟替换失败"):
        manager._save({"id": "job-1", "status": "draft", "files": []})

    assert path.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []


def test_job_save_rejects_invalid_identity_without_overwrite(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(TrainingError, match="任务 ID"):
        manager._save({"id": "../outside", "status": "draft", "files": []})

    assert not (tmp_path / "data" / "training" / "outside" / "job.json").exists()


def test_corrupt_job_does_not_hide_valid_recoverable_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    corrupt = manager.jobs_root / "bad" / "job.json"
    corrupt.parent.mkdir()
    corrupt.write_text("{broken", encoding="utf-8")
    valid = manager.jobs_root / "good" / "job.json"
    valid.parent.mkdir()
    valid.write_text(
        json.dumps(
            {
                "id": "good",
                "status": "draft",
                "files": [],
                "updated_at": "2026-09-15T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    result = manager.latest_recoverable_job()

    assert result is not None
    assert result["id"] == "good"
    assert corrupt.read_text(encoding="utf-8") == "{broken"


def test_latest_saved_job_does_not_restore_an_older_unfinished_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    jobs = (
        {
            "id": "older-prepared",
            "status": "prepared",
            "files": [{"name": "old.wav", "text": "旧任务"}],
            "updated_at": "2026-09-04T00:00:00+00:00",
        },
        {
            "id": "latest-saved",
            "status": "registered",
            "files": [{"name": "new.wav", "text": "最新任务"}],
            "updated_at": "2026-09-16T00:00:00+00:00",
        },
    )
    for job in jobs:
        path = manager.jobs_root / job["id"] / "job.json"
        path.parent.mkdir()
        path.write_text(json.dumps(job), encoding="utf-8")

    assert manager.latest_recoverable_job() is None


def test_latest_unfinished_job_is_restored_even_with_older_saved_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    jobs = (
        {
            "id": "older-saved",
            "status": "registered",
            "files": [],
            "updated_at": "2026-09-04T00:00:00+00:00",
        },
        {
            "id": "latest-draft",
            "status": "draft",
            "files": [],
            "updated_at": "2026-09-16T00:00:00+00:00",
        },
    )
    for job in jobs:
        path = manager.jobs_root / job["id"] / "job.json"
        path.parent.mkdir()
        path.write_text(json.dumps(job), encoding="utf-8")

    result = manager.latest_recoverable_job()

    assert result is not None
    assert result["id"] == "latest-draft"


def test_latest_saved_job_does_not_restore_an_older_unfinished_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    jobs = (
        {
            "id": "older-prepared",
            "status": "prepared",
            "files": [{"name": "old.wav", "text": "旧任务"}],
            "updated_at": "2026-09-04T00:00:00+00:00",
        },
        {
            "id": "latest-saved",
            "status": "registered",
            "files": [{"name": "new.wav", "text": "最新任务"}],
            "updated_at": "2026-09-16T00:00:00+00:00",
        },
    )
    for job in jobs:
        path = manager.jobs_root / job["id"] / "job.json"
        path.parent.mkdir()
        path.write_text(json.dumps(job), encoding="utf-8")

    assert manager.latest_recoverable_job() is None


def test_latest_unfinished_job_is_restored_even_with_older_saved_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    jobs = (
        {
            "id": "older-saved",
            "status": "registered",
            "files": [],
            "updated_at": "2026-09-04T00:00:00+00:00",
        },
        {
            "id": "latest-draft",
            "status": "draft",
            "files": [],
            "updated_at": "2026-09-16T00:00:00+00:00",
        },
    )
    for job in jobs:
        path = manager.jobs_root / job["id"] / "job.json"
        path.parent.mkdir()
        path.write_text(json.dumps(job), encoding="utf-8")

    result = manager.latest_recoverable_job()

    assert result is not None
    assert result["id"] == "latest-draft"


@pytest.mark.parametrize("stage", ["导入素材", "数据准备", "模型训练", "保存模型"])
def test_disk_gate_stops_stage_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    manager = _manager(tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("backend.training.shutil.disk_usage", lambda _path: usage(100, 99, 1))

    with pytest.raises(TrainingError, match=stage):
        manager._ensure_disk_space(tmp_path, required_bytes=2, stage=stage)


def test_training_artifact_diff_requires_one_current_output(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    output = tmp_path / "weights"
    output.mkdir()
    old = output / "old.pth"
    old.write_bytes(b"old")
    before = manager._artifact_snapshot(output, ".pth")
    current = output / "current.pth"
    current.write_bytes(b"current")

    selected = manager._select_current_artifact(output, ".pth", before, "SoVITS")

    assert selected == current.resolve()
    another = output / "another.pth"
    another.write_bytes(b"another")
    with pytest.raises(TrainingError, match="产生了多个"):
        manager._select_current_artifact(output, ".pth", before, "SoVITS")


def test_reference_audio_and_prompt_text_come_from_same_item(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    missing = tmp_path / "missing.wav"
    valid = tmp_path / "valid.wav"
    valid.write_bytes(b"RIFF")
    files = [
        {"wav_path": str(missing), "text": "错误的第一条文本"},
        {"wav_path": str(valid), "text": "正确配对文本"},
    ]

    reference, prompt = manager._select_reference(files)

    assert reference == valid.resolve()
    assert prompt == "正确配对文本"


def test_finalize_artifact_must_be_inside_allowed_output(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.gsv_root = tmp_path / "GPT-SoVITS"
    allowed = manager.gsv_root / "GPT_weights_v2Pro"
    allowed.mkdir(parents=True)
    valid = allowed / "current.ckpt"
    valid.write_bytes(b"weight")

    assert manager._validate_recorded_artifact(valid, allowed, ".ckpt", "GPT") == valid.resolve()
    outside = tmp_path / "old.ckpt"
    outside.write_bytes(b"old")
    with pytest.raises(TrainingError, match="路径无效"):
        manager._validate_recorded_artifact(outside, allowed, ".ckpt", "GPT")


def test_finalize_recovers_when_model_was_imported_before_job_state_save(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    gpt = manager.gsv_root / "GPT_weights_v2Pro" / "current.ckpt"
    sovits = manager.gsv_root / "SoVITS_weights_v2Pro" / "current.pth"
    gpt.parent.mkdir(parents=True)
    sovits.parent.mkdir(parents=True)
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    job = {
        "id": "job-1",
        "name": "训练模型",
        "model_id": "trained",
        "language": "zh",
        "status": "completed",
        "files": [],
        "artifacts": {"gpt": str(gpt), "sovits": str(sovits)},
    }
    manager._save(job)
    model_root = tmp_path / "data" / "models" / "trained"
    model_root.mkdir(parents=True)
    (model_root / "model.ckpt").write_bytes(b"gpt")
    (model_root / "model.pth").write_bytes(b"sovits")
    metadata = {
        "id": "trained",
        "gpt_model": "model.ckpt",
        "sovits_model": "model.pth",
        "training_source": {
            "job_id": "job-1",
            "gpt_sha256": manager._sha256(gpt),
            "sovits_sha256": manager._sha256(sovits),
        },
    }
    (model_root / "model.json").write_text(json.dumps(metadata), encoding="utf-8")
    save_installed_models(
        tmp_path,
        [InstalledModel("trained", "训练模型", "0.1.0", "models/trained", ["model.json"])],
    )

    result = manager.finalize("job-1")

    assert result["status"] == "registered"
    assert "无需重复导入" in result["message"]


def test_finalize_rejects_existing_model_from_another_job(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    gpt = manager.gsv_root / "GPT_weights_v2Pro" / "current.ckpt"
    sovits = manager.gsv_root / "SoVITS_weights_v2Pro" / "current.pth"
    gpt.parent.mkdir(parents=True)
    sovits.parent.mkdir(parents=True)
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    manager._save(
        {
            "id": "job-1",
            "name": "训练模型",
            "model_id": "trained",
            "language": "zh",
            "status": "completed",
            "files": [],
            "artifacts": {"gpt": str(gpt), "sovits": str(sovits)},
        }
    )
    model_root = tmp_path / "data" / "models" / "trained"
    model_root.mkdir(parents=True)
    (model_root / "model.json").write_text(
        '{"id":"trained","training_source":{"job_id":"other"}}', encoding="utf-8"
    )
    save_installed_models(
        tmp_path,
        [InstalledModel("trained", "旧模型", "0.1.0", "models/trained", ["model.json"])],
    )

    with pytest.raises(TrainingError, match="不属于当前训练任务"):
        manager.finalize("job-1")


def test_training_ui_does_not_claim_checkpoint_resume() -> None:
    root = Path(__file__).parents[1]
    backend_source = (root / "backend" / "training.py").read_text(encoding="utf-8-sig")
    frontend_source = (root / "frontend" / "app.py").read_text(encoding="utf-8-sig")

    assert "可以继续训练" not in backend_source
    assert "开始/继续训练" not in frontend_source
    assert "重新开始模型训练阶段" in backend_source
    assert '"save_every_epoch": self.DEFAULT_EPOCHS' in backend_source
    assert '"save_every_n_epoch": self.DEFAULT_EPOCHS' in backend_source


def test_training_ui_explains_engine_release_and_blocks_duplicate_start() -> None:
    root = Path(__file__).parents[1]
    frontend_source = (root / "frontend" / "app.py").read_text(encoding="utf-8-sig")

    assert "训练尚未开始，请勿重复点击" in frontend_source
    assert "_training_start_pending" in frontend_source
    assert 'QPushButton("开始训练")' in frontend_source


def test_engine_release_submits_training_only_after_request_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted_with_request_thread: list[object] = []
    page = SimpleNamespace(
        request_thread=SimpleNamespace(deleteLater=lambda: None),
        request_worker=object(),
        _request_path="/api/engine/unload",
        _training_start_pending=True,
        _training_start_after_engine_release=True,
        job_status="interrupted",
        _ow_training_set_buttons=lambda: None,
        _ow_training_set_status=lambda _message: None,
        _ow_training_frontend_error=lambda _context, _error: None,
    )
    monkeypatch.setattr(
        frontend_app,
        "_ow_training_original_start_training",
        lambda current: submitted_with_request_thread.append(current.request_thread),
    )
    monkeypatch.setattr(frontend_app.QTimer, "singleShot", lambda _delay, callback: callback())

    frontend_app._ow_training_request_finished(page)

    assert submitted_with_request_thread == [None]
    assert page._training_start_after_engine_release is False


def test_training_heartbeat_is_not_blocked_by_normal_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    page = SimpleNamespace(
        job_status="running",
        job_id="job-1",
        request_thread=object(),
        heartbeat_thread=None,
        heartbeat_state=SimpleNamespace(take_report=lambda: ""),
        training_heartbeat_timer=SimpleNamespace(stop=lambda: None),
    )
    monkeypatch.setattr(
        frontend_app,
        "_ow_training_start_heartbeat",
        lambda current: started.append(current.job_id),
    )

    frontend_app._ow_training_send_heartbeat(page)

    assert started == ["job-1"]


def test_heartbeat_timeout_reason_is_not_overwritten_by_worker_cleanup(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "interrupted",
            "stage": "连接中断，训练已停止",
            "message": "OwVoice 前端超过 90 秒没有响应，后端已停止训练。",
            "error": "前端心跳超时。",
            "stop_reason": "heartbeat_timeout",
            "files": [],
        }
    )

    manager._set_cancelled_status(
        "job-1",
        stage="训练已停止",
        message="训练已停止；素材仍保留。",
    )

    job = manager._load("job-1")
    assert job["stop_reason"] == "heartbeat_timeout"
    assert job["stage"] == "连接中断，训练已停止"
    assert "90 秒" in job["message"]


@pytest.mark.parametrize(
    ("stage", "epoch", "line", "expected"),
    [
        ("正在训练 SoVITS", 4, " 50%|#####     | 4/8", 77),
        ("正在训练 GPT", 7, " 80%|########  | 8/10", 97),
    ],
)
def test_training_output_updates_visible_progress(
    tmp_path: Path, stage: str, epoch: int, line: str, expected: int
) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "running",
            "stage": stage,
            "progress": 72 if "SoVITS" in stage else 84,
            "message": "正在训练",
            "stage_epoch": epoch,
            "stage_epochs": 8,
            "files": [],
        }
    )

    manager._update_training_progress_from_output("job-1", line)

    job = manager._load("job-1")
    assert job["progress"] == expected
    assert f"第 {epoch}/8 轮" in job["message"]


def test_sovits_preparation_progress_is_not_treated_as_training_progress(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "running",
            "stage": "正在训练 SoVITS",
            "progress": 72,
            "message": "正在启动 SoVITS 训练",
            "stage_progress": 0,
            "stage_epoch": 0,
            "stage_epochs": 8,
            "files": [],
        }
    )

    manager._update_training_progress_from_output(
        "job-1", "100%|##########| 100/100 [00:00<00:00]"
    )

    job = manager._load("job-1")
    assert job["progress"] == 72
    assert job["stage_progress"] == 0
    assert "100%" not in job["message"]


def test_sovits_progress_combines_epoch_and_current_epoch_percent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "running",
            "stage": "正在训练 SoVITS",
            "progress": 72,
            "message": "正在启动 SoVITS 训练",
            "stage_progress": 0,
            "stage_epoch": 0,
            "stage_epochs": 8,
            "files": [],
        }
    )

    manager._update_training_progress_from_output(
        "job-1", "0%| | 0/100 INFO:test:Train Epoch: 6 [0%]"
    )
    manager._update_training_progress_from_output(
        "job-1", "25%|##5| 25/100 [00:18<00:51]"
    )

    job = manager._load("job-1")
    assert job["stage_epoch"] == 6
    assert job["epoch_progress"] == 25
    assert job["stage_progress"] == 66
    assert job["progress"] == 80
    assert "第 6/8 轮" in job["message"]
    assert "本轮 25%" in job["message"]


def test_first_sovits_epoch_context_is_kept_when_overall_percent_is_unchanged(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "running",
            "stage": "正在训练 SoVITS",
            "progress": 72,
            "message": "正在启动 SoVITS 训练",
            "stage_progress": 0,
            "stage_epoch": 0,
            "stage_epochs": 8,
            "files": [],
        }
    )

    manager._update_training_progress_from_output(
        "job-1", "0%| | 0/100 INFO:test:Train Epoch: 1 [0%]"
    )

    job = manager._load("job-1")
    assert job["stage_epoch"] == 1
    assert "第 1/8 轮" in job["message"]


def test_stage_reaching_100_percent_does_not_claim_whole_training_finished(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager._save(
        {
            "id": "job-1",
            "status": "running",
            "stage": "正在训练 SoVITS",
            "progress": 83,
            "message": "正在训练 SoVITS：99%",
            "stage_progress": 99,
            "stage_epoch": 8,
            "stage_epochs": 8,
            "files": [],
        }
    )

    manager._update_training_progress_from_output(
        "job-1", "100%|##########| 100/100"
    )

    job = manager._load("job-1")
    assert job["status"] == "running"
    assert job["progress"] == 83
    assert job["stage_progress"] == 100
    assert "整个训练尚未完成" in job["message"]
    assert "训练完成" not in job["message"].replace("尚未完成", "")


def test_training_page_labels_overall_progress_separately() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )

    assert 'self.overall_progress_label = _ow_training_hint("总体进度：0%")' in source
    assert 'self.overall_progress_label.setText(f"总体进度：{progress}%")' in source
    assert '第 {stage_epoch}/{stage_epochs} 轮' in source
