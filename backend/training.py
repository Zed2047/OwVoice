"""OwVoice 本地训练任务编排。

训练算法仍由 GPT-SoVITS 官方脚本负责，本模块只负责：
素材复制与规范化、任务状态、数据准备、训练进程和模型打包。
"""

from __future__ import annotations

import json
import importlib.util
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
import psutil
from backend.training_errors import TrainingError


class TrainingManager:
    """管理单机上的训练任务；同一时间只允许一个训练任务运行。"""

    AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    LANGUAGE_LOCALE = {"zh": "zh-CN", "yue": "zh-HK", "en": "en-US", "ja": "ja-JP", "ko": "ko-KR"}
    DEFAULT_EPOCHS = 8
    FRONTEND_HEARTBEAT_TIMEOUT = 45.0

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.gsv_root = self.project_dir / "GPT-SoVITS"
        self.jobs_root = self.project_dir / "data" / "training" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="owvoice-training-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        self._recover_interrupted_jobs()

    @property
    def python_exe(self) -> Path:
        candidate = self.project_dir / ".venv" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
        raise TrainingError("找不到项目 Python 环境，请先完成首次配置。")

    @property
    def ffmpeg_exe(self) -> Path:
        candidate = self.gsv_root / "ffmpeg.exe"
        if candidate.is_file():
            return candidate
        found = shutil.which("ffmpeg")
        if found:
            return Path(found)
        raise TrainingError("找不到 FFmpeg，无法处理音频素材。")

    def _job_dir(self, job_id: str) -> Path:
        path = (self.jobs_root / job_id).resolve()
        if self.jobs_root.resolve() not in path.parents:
            raise TrainingError("训练任务路径无效。")
        return path

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _load(self, job_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._job_path(job_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TrainingError(f"训练任务不存在或已损坏：{job_id}") from exc
        if not isinstance(value, dict):
            raise TrainingError(f"训练任务数据无效：{job_id}")
        return value

    def _save(self, job: dict[str, Any]) -> None:
        path = self._job_path(str(job["id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _recover_interrupted_jobs(self) -> None:
        """服务重启后清理失联的运行状态，避免界面永久停在加载中。"""
        for job_path in self.jobs_root.glob("*/job.json"):
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(job, dict) or str(job.get("status", "")) not in {"transcribing", "preparing", "running", "cancelling"}:
                continue
            self._terminate_recorded_process(job.get("process_pid"))
            job["status"] = "interrupted"
            job["stage"] = "任务已中断"
            job["message"] = "上次任务未完成，残留进程已停止，可以重新开始。"
            job["error"] = "服务重启或程序异常退出。"
            job["process_pid"] = None
            job["updated_at"] = self._now()
            try:
                job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                continue

    def _watchdog_loop(self) -> None:
        """前端心跳超时后停止训练，避免训练脱离界面长期运行。"""
        while not self._watchdog_stop.wait(5):
            expired_sessions: set[str] = set()
            now = time.time()
            with self._lock:
                for job_id in list(self._threads):
                    try:
                        job = self._load(job_id)
                    except TrainingError:
                        continue
                    status = str(job.get("status", ""))
                    session_id = str(job.get("session_id", "")).strip()
                    heartbeat = str(job.get("last_heartbeat", "")).strip()
                    if status not in {"transcribing", "preparing", "running", "cancelling"} or not session_id or not heartbeat:
                        continue
                    try:
                        heartbeat_at = datetime.fromisoformat(heartbeat).timestamp()
                    except ValueError:
                        continue
                    if now - heartbeat_at > self.FRONTEND_HEARTBEAT_TIMEOUT:
                        expired_sessions.add(session_id)
            for session_id in expired_sessions:
                self.expire_session(session_id)

    @staticmethod
    def _safe_name(value: str, fallback: str = "voice") -> str:
        value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-_")
        return value[:64] or fallback

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = dict(job)
        result.pop("source_paths", None)
        result.pop("raw_paths", None)
        result.pop("avatar_path", None)
        result.pop("cancelled", None)
        result.pop("session_id", None)
        result.pop("last_heartbeat", None)
        result.pop("process_pid", None)
        result["files"] = [
            {key: value for key, value in item.items() if key not in {"raw_path", "wav_path"}}
            for item in result.get("files", [])
            if isinstance(item, dict)
        ]
        data_ready = self._dataset_ready(job)
        result["data_ready"] = data_ready
        result["next_action"] = self._next_action(job, data_ready)
        result["can_train"] = result["next_action"] == "train"
        return result

    def _dataset_ready(self, job: dict[str, Any]) -> bool:
        """确认正式训练所需的合并数据确实存在，不能只看进度数字。"""
        model_id = str(job.get("model_id", "")).strip()
        if not model_id:
            return False
        exp_dir = self.gsv_root / "logs" / model_id
        text_path = exp_dir / "2-name2text.txt"
        semantic_path = exp_dir / "6-name2semantic.tsv"
        if not text_path.is_file() or not semantic_path.is_file():
            return False
        try:
            text_lines = [line for line in text_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            semantic_lines = [line for line in semantic_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeError):
            return False
        files = [item for item in job.get("files", []) if isinstance(item, dict)]
        return (
            bool(files)
            and len(text_lines) >= len(files)
            and len(semantic_lines) > len(files)
            and all(Path(str(item.get("wav_path", ""))).is_file() for item in files)
        )

    @staticmethod
    def _next_action(job: dict[str, Any], data_ready: bool) -> str:
        status = str(job.get("status", ""))
        stage = str(job.get("stage", ""))
        if status in {"transcribing", "preparing", "running", "cancelling"}:
            return "wait"
        if status in {"completed", "registered"}:
            return "save" if status == "completed" else "done"
        if status == "interrupted":
            if "识别" in stage:
                return "transcribe"
            return "train" if data_ready and int(job.get("progress", 0) or 0) >= 70 else "prepare"
        if (status == "prepared" and data_ready) or (status == "failed" and data_ready and ("训练" in stage or int(job.get("progress", 0) or 0) >= 70)):
            return "train"
        if status == "failed":
            if "识别" in stage:
                return "transcribe"
            return "prepare"
        if status == "draft":
            return "prepare" if any(str(item.get("text", "")).strip() for item in job.get("files", [])) else "transcribe"
        return "prepare"

    def _set_status(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self._load(job_id)
            job.update(changes)
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def create_job(
        self,
        name: str,
        language: str,
        source_paths: list[str],
        avatar_path: str | None = None,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        language = str(language or "zh").strip().lower()
        if not name:
            raise TrainingError("请先填写模型名称。")
        if language not in self.LANGUAGE_LOCALE:
            raise TrainingError("暂不支持该训练语言。")
        if not source_paths:
            raise TrainingError("请至少选择一个音频文件。")
        if len(source_paths) > 200:
            raise TrainingError("一次最多上传 200 个音频文件。")

        job_id = uuid.uuid4().hex[:12]
        job_dir = self._job_dir(job_id)
        raw_dir = job_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=False)
        files: list[dict[str, Any]] = []
        used_names: set[str] = set()
        try:
            for index, source_value in enumerate(source_paths, start=1):
                source = Path(source_value).expanduser().resolve()
                if not source.is_file():
                    raise TrainingError(f"音频文件不存在：{source}")
                if source.suffix.lower() not in self.AUDIO_EXTENSIONS:
                    raise TrainingError(f"不支持的音频格式：{source.name}")
                stem = self._safe_name(source.stem, f"audio-{index:03d}")
                target_name = f"{index:03d}-{stem}{source.suffix.lower()}"
                while target_name in used_names or (raw_dir / target_name).exists():
                    target_name = f"{index:03d}-{stem}-{uuid.uuid4().hex[:4]}{source.suffix.lower()}"
                used_names.add(target_name)
                target = raw_dir / target_name
                shutil.copy2(source, target)
                files.append(
                    {
                        "name": target_name,
                        "source_name": source.name,
                        "raw_path": str(target),
                        "wav_path": "",
                        "text": "",
                        "duration": 0.0,
                    }
                )
            avatar_internal = ""
            avatar_name = ""
            if avatar_path:
                avatar_source = Path(avatar_path).expanduser().resolve()
                if not avatar_source.is_file():
                    raise TrainingError(f"角色头像不存在：{avatar_source}")
                if avatar_source.suffix.lower() not in self.IMAGE_EXTENSIONS:
                    raise TrainingError(f"不支持的头像格式：{avatar_source.name}")
                avatar_target = job_dir / f"avatar{avatar_source.suffix.lower()}"
                shutil.copy2(avatar_source, avatar_target)
                avatar_internal = str(avatar_target)
                avatar_name = avatar_source.name
            job = {
                "id": job_id,
                "name": name,
                "model_id": self._safe_name(name, f"local-{job_id}")[:64],
                "language": language,
                "version": "v2Pro",
                "stage": "素材已上传",
                "status": "draft",
                "progress": 5,
                "message": "素材已复制到本地训练任务。请检查并校对文本。",
                "created_at": self._now(),
                "updated_at": self._now(),
                "source_paths": [str(Path(item).expanduser().resolve()) for item in source_paths],
                "files": files,
                "avatar_path": avatar_internal,
                "avatar_name": avatar_name,
                "artifacts": {},
                "error": "",
            }
            self._save(job)
            return self._public(job)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._public(self._load(job_id))

    def file_path(self, job_id: str, file_name: str) -> Path:
        """返回任务内音频的安全路径，供桌面端试听，不暴露任务目录结构。"""
        with self._lock:
            job = self._load(job_id)
            item = next((entry for entry in job.get("files", []) if str(entry.get("name", "")) == file_name), None)
            if item is None:
                raise TrainingError("找不到要试听的音频。")
            raw_dir = (self._job_dir(job_id) / "raw").resolve()
            path = Path(str(item.get("raw_path", ""))).resolve()
            if raw_dir not in path.parents or not path.is_file():
                raise TrainingError("训练音频文件不存在。")
            return path

    def avatar_file_path(self, job_id: str) -> Path:
        """返回任务头像的安全路径，供训练页恢复头像预览。"""
        with self._lock:
            job = self._load(job_id)
            job_dir = self._job_dir(job_id)
            path = Path(str(job.get("avatar_path", ""))).resolve()
            if not path.is_file() or job_dir not in path.parents:
                raise TrainingError("当前任务没有角色头像。")
            return path

    def latest_recoverable_job(self) -> dict[str, Any] | None:
        """返回最近一次可继续查看的任务，供前端异常退出后恢复。"""
        ignored_statuses = {"registered"}
        candidates: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for job_path in self.jobs_root.glob("*/job.json"):
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(job, dict) or str(job.get("status", "")) in ignored_statuses:
                    continue
                candidates.append((str(job.get("updated_at", "")), job))
        if not candidates:
            return None
        _updated_at, job = max(candidates, key=lambda item: item[0])
        return self._public(job)

    def heartbeat(self, session_id: str, job_id: str | None = None) -> dict[str, Any]:
        """刷新前端租约；训练任务只能由启动它的前端会话保活。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            raise TrainingError("前端会话标识无效。")
        with self._lock:
            if job_id:
                job = self._load(job_id)
                status = str(job.get("status", ""))
                if status in {"transcribing", "preparing", "running", "cancelling"}:
                    if str(job.get("session_id", "")) != session_id:
                        raise TrainingError("当前训练任务属于其他前端会话。")
                    job["last_heartbeat"] = self._now()
                    self._save(job)
                return self._public(job)
        return {"status": "idle"}

    @staticmethod
    def _process_matches_project(process: psutil.Process, project_dir: Path) -> bool:
        try:
            command_line = " ".join(process.cmdline()).lower()
        except (psutil.Error, OSError):
            return False
        project_text = str(project_dir.resolve()).lower()
        return project_text in command_line and "gpt_so\u0076its" in command_line

    def _terminate_recorded_process(self, pid_value: object) -> None:
        """只按任务记录且经命令行校验的 PID 清理残留训练进程。"""
        try:
            pid = int(str(pid_value or "0"))
            process = psutil.Process(pid)
        except (TypeError, ValueError, psutil.Error, OSError):
            return
        if not self._process_matches_project(process, self.project_dir):
            return
        targets = process.children(recursive=True) + [process]
        for target in targets:
            try:
                target.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(targets, timeout=5)
        for target in alive:
            try:
                target.kill()
            except psutil.Error:
                pass

    def expire_session(self, session_id: str) -> bool:
        """前端心跳超时后停止属于该会话的训练任务。"""
        expired = False
        processes: list[subprocess.Popen[str]] = []
        with self._lock:
            for job_id, event in list(self._cancel.items()):
                try:
                    job = self._load(job_id)
                except TrainingError:
                    continue
                if str(job.get("session_id", "")) != str(session_id) or str(job.get("status", "")) not in {"transcribing", "preparing", "running", "cancelling"}:
                    continue
                expired = True
                event.set()
                job.update({
                    "status": "interrupted",
                    "stage": "前端已退出",
                    "message": "前端异常退出，训练已自动停止。",
                    "error": "前端心跳超时。",
                    "process_pid": None,
                    "updated_at": self._now(),
                })
                self._save(job)
                process = self._processes.get(job_id)
                if process is not None:
                    processes.append(process)
        for process in processes:
            self._terminate_process_tree(process)
        return expired

    def update_transcripts(self, job_id: str, transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(transcripts, list):
            raise TrainingError("文本列表格式无效。")
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在运行，完成后才能修改训练文本。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能再修改训练文本。")
            by_name = {str(item.get("name")): str(item.get("text", "")).strip() for item in transcripts if isinstance(item, dict)}
            changed = 0
            for item in job.get("files", []):
                text = by_name.get(str(item.get("name")))
                if text is not None:
                    item["text"] = text
                    changed += 1
            if changed != len(job.get("files", [])):
                raise TrainingError("提交的文本和音频列表不一致，请刷新后重试。")
            if current_status in {"prepared", "completed"}:
                # 文本变化会使已有特征和索引失效，必须重新准备数据后才能训练。
                job["status"] = "draft"
                job["progress"] = 30
                job["artifacts"] = {}
                job["message"] = "文本已修改，请重新准备训练数据。"
                job["stage"] = "等待重新准备"
            else:
                job["message"] = "文本已保存。"
                job["stage"] = "等待处理"
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def add_files(self, job_id: str, source_paths: list[str]) -> dict[str, Any]:
        """向尚未运行的任务追加音频，并使旧的数据准备结果失效。"""
        if not source_paths:
            raise TrainingError("请至少选择一个音频文件。")
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在处理，完成后才能修改训练素材。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能再修改训练素材。")
            existing_files = list(job.get("files", []))
            if len(existing_files) + len(source_paths) > 200:
                raise TrainingError("训练任务最多包含 200 个音频文件。")

            raw_dir = self._job_dir(job_id) / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            used_names = {str(item.get("name", "")) for item in existing_files}
            added: list[dict[str, Any]] = []
            try:
                for offset, source_value in enumerate(source_paths, start=1):
                    source = Path(source_value).expanduser().resolve()
                    if not source.is_file():
                        raise TrainingError(f"音频文件不存在：{source}")
                    if source.suffix.lower() not in self.AUDIO_EXTENSIONS:
                        raise TrainingError(f"不支持的音频格式：{source.name}")
                    stem = self._safe_name(source.stem, f"audio-{len(existing_files) + offset:03d}")
                    target_name = f"{len(existing_files) + offset:03d}-{stem}{source.suffix.lower()}"
                    while target_name in used_names or (raw_dir / target_name).exists():
                        target_name = f"{len(existing_files) + offset:03d}-{stem}-{uuid.uuid4().hex[:4]}{source.suffix.lower()}"
                    used_names.add(target_name)
                    target = raw_dir / target_name
                    shutil.copy2(source, target)
                    added.append(
                        {
                            "name": target_name,
                            "source_name": source.name,
                            "raw_path": str(target),
                            "wav_path": "",
                            "text": "",
                            "duration": 0.0,
                        }
                    )
            except Exception:
                for item in added:
                    Path(str(item["raw_path"])).unlink(missing_ok=True)
                raise

            for item in existing_files:
                item["wav_path"] = ""
            job["files"] = existing_files + added
            job["source_paths"] = list(job.get("source_paths", [])) + [
                str(Path(item).expanduser().resolve()) for item in source_paths
            ]
            job["status"] = "draft"
            job["progress"] = 30 if job["files"] and all(str(item.get("text", "")).strip() for item in job["files"]) else 5
            job["artifacts"] = {}
            job["stage"] = "等待校对"
            job["message"] = f"已追加 {len(added)} 个音频，请检查训练文字。"
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def remove_file(self, job_id: str, file_name: str) -> dict[str, Any]:
        """兼容旧调用：删除任务中的一个音频。"""
        return self.remove_files(job_id, [file_name])

    def remove_files(self, job_id: str, file_names: list[str]) -> dict[str, Any]:
        """批量删除任务中的音频；先完整校验，再统一提交，避免部分删除。"""
        names = [str(name) for name in file_names if str(name).strip()]
        if not names:
            raise TrainingError("请先选择要删除的音频。")
        if len(set(names)) != len(names):
            raise TrainingError("删除列表中存在重复音频，请刷新后重试。")
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在处理，完成后才能修改训练素材。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能再修改训练素材。")
            files = list(job.get("files", []))
            by_name = {str(item.get("name", "")): item for item in files}
            missing = [name for name in names if name not in by_name]
            if missing:
                raise TrainingError("找不到要删除的音频，请刷新后重试。")
            raw_dir = (self._job_dir(job_id) / "raw").resolve()
            for name in names:
                raw_path = Path(str(by_name[name].get("raw_path", ""))).resolve()
                if raw_path.is_file() and raw_dir in raw_path.parents:
                    raw_path.unlink()
            files = [item for item in files if str(item.get("name", "")) not in set(names)]
            job["files"] = files
            job["status"] = "draft"
            job["progress"] = 30 if files and all(str(entry.get("text", "")).strip() for entry in files) else 5
            job["artifacts"] = {}
            job["stage"] = "等待校对" if files else "等待素材"
            job["message"] = f"已删除 {len(names)} 个音频；当前剩余 {len(files)} 个素材。"
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def set_avatar(self, job_id: str, source_path: str) -> dict[str, Any]:
        """设置或替换训练任务头像；不传头像时由前端使用默认头像。"""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise TrainingError(f"角色头像不存在：{source}")
        if source.suffix.lower() not in self.IMAGE_EXTENSIONS:
            raise TrainingError(f"不支持的头像格式：{source.name}")
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在处理，完成后才能修改角色头像。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能再修改角色头像。")
            job_dir = self._job_dir(job_id)
            old_path = Path(str(job.get("avatar_path", ""))).resolve()
            if old_path.is_file() and job_dir in old_path.parents:
                old_path.unlink()
            target = job_dir / f"avatar{source.suffix.lower()}"
            shutil.copy2(source, target)
            job["avatar_path"] = str(target)
            job["avatar_name"] = source.name
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def clear_files(self, job_id: str) -> dict[str, Any]:
        """清空尚未运行任务的全部音频素材。"""
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在处理，完成后才能清空训练素材。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能再修改训练素材。")
            raw_dir = (self._job_dir(job_id) / "raw").resolve()
            for item in job.get("files", []):
                raw_path = Path(str(item.get("raw_path", ""))).resolve()
                if raw_path.is_file() and raw_dir in raw_path.parents:
                    raw_path.unlink()
            job["files"] = []
            job["source_paths"] = []
            job["status"] = "draft"
            job["progress"] = 0
            job["artifacts"] = {}
            job["stage"] = "等待素材"
            job["message"] = "训练素材已清空，请重新添加音频。"
            job["updated_at"] = self._now()
            self._save(job)
            return self._public(job)

    def prepare_with_transcripts(self, job_id: str, transcripts: list[dict[str, Any]], session_id: str = "") -> dict[str, Any]:
        """原子地保存当前文字并启动数据准备，避免前端两个请求产生竞态。"""
        if not isinstance(transcripts, list):
            raise TrainingError("文本列表格式无效。")
        if not str(session_id or "").strip():
            raise TrainingError("缺少 OwVoice 前端会话，不能启动训练任务。")
        with self._lock:
            job = self._load(job_id)
            current_status = str(job.get("status", ""))
            if current_status in {"transcribing", "preparing", "running", "cancelling"}:
                raise TrainingError("当前任务正在运行，请等待当前操作完成。")
            if current_status == "registered":
                raise TrainingError("模型已经保存，不能重新准备数据。")
            by_name = {
                str(item.get("name")): str(item.get("text", "")).strip()
                for item in transcripts
                if isinstance(item, dict)
            }
            files = list(job.get("files", []))
            if not files:
                raise TrainingError("训练任务没有音频文件。")
            if len(by_name) != len(files) or any(str(item.get("name")) not in by_name for item in files):
                raise TrainingError("提交的文本和音频列表不一致，请刷新后重试。")
            if any(not by_name[str(item.get("name"))] for item in files):
                raise TrainingError("还有音频没有训练文本，请补充后再继续。")
            for item in files:
                item["text"] = by_name[str(item.get("name"))]
                item["wav_path"] = ""
            job["files"] = files
            job["status"] = "draft"
            job["progress"] = 30
            job["artifacts"] = {}
            job["stage"] = "等待准备"
            job["message"] = "文字已确认，正在准备训练数据。"
            job["updated_at"] = self._now()
            self._save(job)
            return self._start_thread(
                job_id,
                self._prepare_worker,
                session_id,
                status="preparing",
                stage="正在准备数据",
                progress=32,
                message="文字已确认，正在准备训练数据。",
            )

    def _start_thread(
        self,
        job_id: str,
        target: Any,
        session_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        message: str,
        allowed_statuses: set[str] | None = None,
    ) -> dict[str, Any]:
        if not str(session_id or "").strip():
            raise TrainingError("缺少 OwVoice 前端会话，不能启动训练任务。")
        with self._lock:
            current = self._load(job_id)
            if any(thread.is_alive() for thread in self._threads.values()):
                raise TrainingError("已有训练任务正在运行，请等待完成或先停止它。")
            if current.get("status") in {"running", "preparing", "transcribing"}:
                raise TrainingError("该训练任务正在运行。")
            if allowed_statuses is not None and str(current.get("status", "")) not in allowed_statuses:
                raise TrainingError("当前任务还不能执行此操作，请先完成上一步。")
            current.update({
                "status": status,
                "stage": stage,
                "progress": progress,
                "message": message,
                "error": "",
                "updated_at": self._now(),
                "session_id": str(session_id or ""),
                "last_heartbeat": self._now(),
                "process_pid": None,
            })
            self._save(current)
            event = threading.Event()
            thread = threading.Thread(target=target, args=(job_id, event), daemon=True, name=f"owvoice-training-{job_id}")
            self._threads[job_id] = thread
            self._cancel[job_id] = event
            thread.start()
            return self._public(self._load(job_id))

    def start_transcription(self, job_id: str, session_id: str = "") -> dict[str, Any]:
        return self._start_thread(
            job_id,
            self._transcribe_worker,
            session_id,
            status="transcribing",
            stage="正在识别文本",
            progress=10,
            message="正在启动自动识别，请稍候。",
            allowed_statuses={"draft", "failed", "prepared", "interrupted"},
        )

    def start_prepare(self, job_id: str, session_id: str = "") -> dict[str, Any]:
        return self._start_thread(
            job_id,
            self._prepare_worker,
            session_id,
            status="preparing",
            stage="正在准备数据",
            progress=32,
            message="正在启动数据准备，请稍候。",
            allowed_statuses={"draft", "failed", "interrupted"},
        )

    def start_training(self, job_id: str, session_id: str = "") -> dict[str, Any]:
        with self._lock:
            job = self._load(job_id)
            status = str(job.get("status", ""))
            data_ready = self._dataset_ready(job)
            if status not in {"prepared", "failed", "interrupted"} or not data_ready:
                raise TrainingError("训练数据未准备完成，请先重新准备数据。")
        return self._start_thread(
            job_id,
            self._training_worker,
            session_id,
            status="running",
            stage="正在启动训练",
            progress=72,
            message="正在启动模型训练，请稍候。",
            allowed_statuses={"prepared", "failed", "interrupted"},
        )

    def cancel(self, job_id: str, session_id: str = "") -> dict[str, Any]:
        with self._lock:
            event = self._cancel.get(job_id)
            if event is None:
                return self._public(self._load(job_id))
            job = self._load(job_id)
            if str(job.get("session_id", "")) != str(session_id or ""):
                raise TrainingError("当前训练任务属于其他前端会话，不能停止。")
            event.set()
            process = self._processes.get(job_id)
            job.update({"status": "cancelling", "message": "正在停止当前任务……", "updated_at": self._now()})
            self._save(job)
            result = self._public(job)
        if process and process.poll() is None:
            self._terminate_process_tree(process)
        return result

    def _finish_thread(self, job_id: str) -> None:
        with self._lock:
            self._threads.pop(job_id, None)
            self._cancel.pop(job_id, None)
            self._processes.pop(job_id, None)

    def _log(self, job_id: str, line: str) -> None:
        path = self._job_dir(job_id) / "training.log"
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line.rstrip() + "\n")

    def log_text(self, job_id: str) -> str:
        """返回当前任务日志；限制体积，避免超长日志拖慢界面。"""
        with self._lock:
            self._load(job_id)
            path = self._job_dir(job_id) / "training.log"
            if not path.is_file():
                return "当前任务还没有训练日志。"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise TrainingError(f"读取训练日志失败：{exc}") from exc
        limit = 1_000_000
        if len(text) > limit:
            text = "……日志过长，仅显示最后一部分……\n" + text[-limit:]
        return text or "当前任务还没有训练日志。"

    def environment_status(self) -> dict[str, Any]:
        """返回训练页使用的可执行环境检查结果，不启动任何重模型。"""
        root = self.gsv_root / "GPT_SoVITS"
        required_files = {
            "Python 虚拟环境": self.project_dir / ".venv" / "Scripts" / "python.exe",
            "FFmpeg": self.gsv_root / "ffmpeg.exe",
            "ffprobe": self.gsv_root / "ffprobe.exe",
            "G2PW": root / "text" / "G2PWModel" / "g2pW.onnx",
            "文本准备脚本": root / "prepare_datasets" / "1-get-text.py",
            "HuBERT 数据脚本": root / "prepare_datasets" / "2-get-hubert-wav32k.py",
            "说话人特征脚本": root / "prepare_datasets" / "2-get-sv.py",
            "语义数据脚本": root / "prepare_datasets" / "3-get-semantic.py",
            "训练配置": root / "configs" / "s2v2Pro.json",
            "HuBERT 预训练模型": root / "pretrained_models" / "chinese-hubert-base" / "config.json",
            "RoBERTa 预训练模型": root / "pretrained_models" / "chinese-roberta-wwm-ext-large" / "config.json",
            "说话人特征模型": root / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
            "SoVITS 预训练生成器": root / "pretrained_models" / "v2Pro" / "s2Gv2Pro.pth",
            "SoVITS 预训练判别器": root / "pretrained_models" / "v2Pro" / "s2Dv2Pro.pth",
            "GPT 预训练模型": root / "pretrained_models" / "s1v3.ckpt",
        }
        missing_files = [f"{label}：{path}" for label, path in required_files.items() if not path.is_file()]
        required_imports = {
            "PyYAML": "yaml",
            "psutil": "psutil",
            "TensorBoard": "tensorboard",
            "Gradio": "gradio",
            "FunASR": "funasr",
            "ModelScope": "modelscope",
            "音频容器支持": "av",
        }
        missing_dependencies = [
            f"{label}（import {module}）"
            for label, module in required_imports.items()
            if importlib.util.find_spec(module) is None
        ]
        ready = not missing_files and not missing_dependencies
        return {
            "ready": ready,
            "trainingResourcesReady": not missing_files,
            "missingFiles": missing_files,
            "missingDependencies": missing_dependencies,
            "message": "本地训练环境已就绪。" if ready else "本地训练需要先安装依赖并下载训练预训练模型。",
        }

    def _check_training_runtime(self, *, include_training: bool = False) -> None:
        """在真正启动重任务前给出明确的环境缺失提示。"""
        required: list[tuple[Path, str]] = [
            (self.ffmpeg_exe, "FFmpeg"),
            (self.gsv_root / "GPT_SoVITS" / "prepare_datasets" / "1-get-text.py", "数据准备脚本"),
            (self.gsv_root / "GPT_SoVITS" / "prepare_datasets" / "2-get-hubert-wav32k.py", "数据准备脚本"),
            (self.gsv_root / "GPT_SoVITS" / "prepare_datasets" / "2-get-sv.py", "数据准备脚本"),
            (self.gsv_root / "GPT_SoVITS" / "prepare_datasets" / "3-get-semantic.py", "数据准备脚本"),
            (self.gsv_root / "GPT_SoVITS" / "configs" / "s2v2Pro.json", "训练配置"),
            (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large", "预训练模型"),
            (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base", "预训练模型"),
            (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt", "说话人特征模型"),
        ]
        if include_training:
            required.extend(
                [
                    (self.gsv_root / "GPT_SoVITS" / "s1_train.py", "训练脚本"),
                    (self.gsv_root / "GPT_SoVITS" / "s2_train.py", "训练脚本"),
                    (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Gv2Pro.pth", "预训练模型"),
                    (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Dv2Pro.pth", "预训练模型"),
                    (self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "s1v3.ckpt", "预训练模型"),
                ]
            )
        missing = [f"{label}：{path}" for path, label in required if not path.exists()]
        if missing:
            raise TrainingError("训练环境不完整，缺少以下文件：\n" + "\n".join(missing))

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """终止当前训练命令及其多进程子任务，避免 Windows 下只停父进程。"""
        if process.poll() is not None:
            return
        try:
            parent = psutil.Process(process.pid)
            targets = parent.children(recursive=True) + [parent]
            for target in targets:
                try:
                    target.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=5)
            for target in alive:
                try:
                    target.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(alive, timeout=5)
            return
        except (OSError, psutil.Error):
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _run_process(self, job_id: str, command: list[str], env: dict[str, str], cancel: threading.Event) -> None:
        self._log(job_id, "$ " + " ".join(command))
        process = subprocess.Popen(
            command,
            cwd=str(self.gsv_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._processes[job_id] = process
            try:
                job = self._load(job_id)
                job["process_pid"] = process.pid
                job["updated_at"] = self._now()
                self._save(job)
            except TrainingError:
                self._terminate_process_tree(process)
                raise
        assert process.stdout is not None
        output: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            for line in process.stdout:
                output.put(line)

        reader = threading.Thread(target=read_output, daemon=True, name=f"owvoice-output-{job_id}")
        reader.start()
        while process.poll() is None:
            try:
                while True:
                    self._log(job_id, output.get_nowait())
            except queue.Empty:
                pass
            if cancel.is_set():
                self._terminate_process_tree(process)
                break
            time.sleep(0.15)
        reader.join(timeout=3)
        try:
            while True:
                self._log(job_id, output.get_nowait())
        except queue.Empty:
            pass
        code = process.poll()
        if code is None:
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                code = process.poll()
        with self._lock:
            self._processes.pop(job_id, None)
            try:
                job = self._load(job_id)
                job["process_pid"] = None
                self._save(job)
            except TrainingError:
                pass
        if cancel.is_set():
            raise TrainingError("任务已停止。")
        if code != 0:
            raise TrainingError(f"训练步骤失败，退出码：{code}。请点击“训练日志”查看详情。")

    def _merge_dataset_parts(self, exp_dir: Path) -> None:
        """合并 GPT-SoVITS 分片输出，生成训练脚本实际读取的固定文件名。"""
        text_parts = sorted(exp_dir.glob("2-name2text-*.txt"))
        if not text_parts:
            raise TrainingError("文字特征没有生成分片文件。")
        text_lines: list[str] = []
        for part in text_parts:
            text_lines.extend(line for line in part.read_text(encoding="utf-8").splitlines() if line.strip())
        if not text_lines:
            raise TrainingError("文字特征为空，无法继续训练。")
        (exp_dir / "2-name2text.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")
        for part in text_parts:
            part.unlink(missing_ok=True)

    def _transcribe_worker(self, job_id: str, cancel: threading.Event) -> None:
        try:
            self._set_status(job_id, status="transcribing", stage="正在识别文本", progress=10, message="正在加载语音识别模型，首次使用可能需要下载模型。", error="")
            job = self._load(job_id)
            if job.get("language") != "zh":
                raise TrainingError("当前自动识别先支持中文；其他语言可以在表格中手动填写文本。")
            # FunASR 是可选训练依赖，必须运行时导入，避免被 PyInstaller 打进基础 EXE。
            funasr_module = importlib.import_module("funasr")
            AutoModel = funasr_module.AutoModel

            model = AutoModel(
                model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                model_revision="v2.0.4",
                vad_model_revision="v2.0.4",
                punc_model_revision="v2.0.4",
            )
            for index, item in enumerate(job.get("files", []), start=1):
                if cancel.is_set():
                    raise TrainingError("任务已停止。")
                source = item.get("raw_path")
                result = model.generate(input=source)
                item["text"] = str(result[0].get("text", "")).strip() if result else ""
                self._save(job)
                self._set_status(job_id, progress=10 + int(index / max(1, len(job["files"])) * 20), message=f"已识别 {index}/{len(job['files'])} 个音频。")
            self._set_status(job_id, status="draft", stage="等待校对", progress=30, message="自动识别完成，请检查文本并修改错误。")
        except Exception as exc:
            if cancel.is_set():
                self._set_status(job_id, status="interrupted", stage="识别已停止", message="识别已停止，可以继续识别。", error="")
            else:
                self._set_status(job_id, status="failed", stage="文本识别失败", message=str(exc), error=str(exc))
        finally:
            self._finish_thread(job_id)

    def _prepare_worker(self, job_id: str, cancel: threading.Event) -> None:
        try:
            job = self._load(job_id)
            files = job.get("files", [])
            if not files:
                raise TrainingError("训练任务没有音频文件。")
            if any(not str(item.get("text", "")).strip() for item in files):
                raise TrainingError("还有音频没有文本，请先自动识别或手动填写并保存。")
            self._check_training_runtime()
            self._set_status(job_id, status="preparing", stage="正在整理音频", progress=32, message="正在统一音频格式并检查素材。", error="")
            wav_dir = self._job_dir(job_id) / "wav32k"
            wav_dir.mkdir(exist_ok=True)
            for index, item in enumerate(files, start=1):
                if cancel.is_set():
                    raise TrainingError("任务已停止。")
                target = wav_dir / (Path(str(item["name"])).stem + ".wav")
                command = [str(self.ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-y", "-i", str(item["raw_path"]), "-ac", "1", "-ar", "32000", "-c:a", "pcm_s16le", str(target)]
                subprocess.run(command, cwd=str(self.gsv_root), check=True, capture_output=True, text=True)
                item["wav_path"] = str(target)
                try:
                    with wave.open(str(target), "rb") as wav:
                        item["duration"] = round(wav.getnframes() / max(1, wav.getframerate()), 2)
                except (OSError, wave.Error):
                    item["duration"] = 0.0
                self._save(job)
                self._set_status(job_id, progress=32 + int(index / len(files) * 12), message=f"已处理 {index}/{len(files)} 个音频。")
            job = self._load(job_id)
            list_path = self._job_dir(job_id) / "dataset.list"
            with list_path.open("w", encoding="utf-8") as handle:
                for item in job["files"]:
                    handle.write(f"{item['wav_path']}|{job['model_id']}|{job['language']}|{str(item['text']).replace(chr(10), ' ').strip()}\n")
            exp_dir = self.gsv_root / "logs" / job["model_id"]
            exp_dir.mkdir(parents=True, exist_ok=True)
            # 本次准备从当前素材快照重新生成，先清理旧分片，避免重复合并旧任务结果。
            for pattern in ("2-name2text.txt", "2-name2text-*.txt", "6-name2semantic.tsv", "6-name2semantic-*.tsv"):
                for stale_path in exp_dir.glob(pattern):
                    stale_path.unlink(missing_ok=True)
            python = str(self.python_exe)
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                str(path)
                for path in (self.gsv_root, self.gsv_root / "GPT_SoVITS", self.gsv_root / "tools")
            )
            env.update({
                "inp_text": str(list_path),
                "inp_wav_dir": "",
                "exp_name": job["model_id"],
                "i_part": "0",
                "all_parts": "1",
                "opt_dir": str(exp_dir),
                "bert_pretrained_dir": str(self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large"),
                "cnhubert_base_dir": str(self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base"),
                "sv_path": str(self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"),
                "pretrained_s2G": str(self.gsv_root / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Gv2Pro.pth"),
                "s2config_path": str(self.gsv_root / "GPT_SoVITS" / "configs" / "s2v2Pro.json"),
                "version": "v2Pro",
                "is_half": "True",
            })
            scripts = [
                ("正在提取文字特征", "GPT_SoVITS/prepare_datasets/1-get-text.py", 46, 51),
                ("正在提取语音特征", "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py", 51, 58),
                ("正在提取语义特征", "GPT_SoVITS/prepare_datasets/2-get-sv.py", 58, 63),
                ("正在生成训练索引", "GPT_SoVITS/prepare_datasets/3-get-semantic.py", 63, 68),
            ]
            for message, script, start, end in scripts:
                self._set_status(job_id, stage="正在准备数据", progress=start, message=message)
                self._run_process(job_id, [python, "-s", script], env, cancel)
                if script.endswith("1-get-text.py"):
                    self._merge_dataset_parts(exp_dir)
                elif script.endswith("3-get-semantic.py"):
                    # 语义脚本会输出分片，复用合并函数的语义部分。
                    semantic_parts = sorted(exp_dir.glob("6-name2semantic-*.tsv"))
                    if not semantic_parts:
                        raise TrainingError("语义特征没有生成分片文件。")
                    semantic_lines = []
                    for part in semantic_parts:
                        semantic_lines.extend(line for line in part.read_text(encoding="utf-8").splitlines() if line.strip())
                    if not semantic_lines:
                        raise TrainingError("语义特征为空，无法继续训练。")
                    (exp_dir / "6-name2semantic.tsv").write_text(
                        "item_name\tsemantic_audio\n" + "\n".join(semantic_lines) + "\n",
                        encoding="utf-8",
                    )
                    for part in semantic_parts:
                        part.unlink(missing_ok=True)
                self._set_status(job_id, progress=end, message=message + "完成。")
            if not self._dataset_ready(self._load(job_id)):
                raise TrainingError("训练数据生成不完整，请重新准备数据。")
            self._set_status(job_id, status="prepared", stage="数据准备完成", progress=70, message="数据准备完成，可以开始训练。")
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or str(exc)).strip()
            if cancel.is_set():
                self._set_status(job_id, status="interrupted", stage="准备已停止", message="数据准备已停止，可以继续准备。", error="")
            else:
                self._set_status(job_id, status="failed", stage="音频处理失败", message=message, error=message)
        except Exception as exc:
            if cancel.is_set():
                self._set_status(job_id, status="interrupted", stage="准备已停止", message="数据准备已停止，可以继续准备。", error="")
            else:
                self._set_status(job_id, status="failed", stage="数据准备失败", message=str(exc), error=str(exc))
        finally:
            self._finish_thread(job_id)

    def _training_worker(self, job_id: str, cancel: threading.Event) -> None:
        try:
            job = self._load(job_id)
            if job.get("status") != "running" or not self._dataset_ready(job):
                raise TrainingError("训练数据未准备完成，请先重新准备数据。")
            self._check_training_runtime(include_training=True)
            try:
                import torch

                use_cuda = bool(torch.cuda.is_available())
            except Exception as exc:  # noqa: BLE001 - 在任务日志中给出明确环境错误
                raise TrainingError(f"无法加载 PyTorch，不能开始训练：{exc}") from exc
            version = "v2Pro"
            pretrained_root = self.gsv_root / "GPT_SoVITS" / "pretrained_models"
            s2_config_path = self.gsv_root / "GPT_SoVITS" / "configs" / "s2v2Pro.json"
            s2_data = json.loads(s2_config_path.read_text(encoding="utf-8"))
            s2_data["train"].update({
                "epochs": self.DEFAULT_EPOCHS,
                "batch_size": 1,
                "text_low_lr_rate": 0.4,
                "pretrained_s2G": str(pretrained_root / "v2Pro" / "s2Gv2Pro.pth"),
                "pretrained_s2D": str(pretrained_root / "v2Pro" / "s2Dv2Pro.pth"),
                "if_save_latest": False,
                "if_save_every_weights": True,
                "save_every_epoch": 1,
                "gpu_numbers": "0",
                "fp16_run": use_cuda,
                "grad_ckpt": False,
                "lora_rank": 32,
            })
            exp_dir = self.gsv_root / "logs" / job["model_id"]
            # GPT-SoVITS 的权重保存函数不会自动创建这两个输出目录。
            # 公开版首次训练时目录通常不存在，必须在启动子进程前创建。
            sovits_weight_dir = self.gsv_root / "SoVITS_weights_v2Pro"
            gpt_weight_dir = self.gsv_root / "GPT_weights_v2Pro"
            sovits_weight_dir.mkdir(parents=True, exist_ok=True)
            gpt_weight_dir.mkdir(parents=True, exist_ok=True)
            s2_data["data"]["exp_dir"] = str(exp_dir)
            s2_data["model"]["version"] = version
            s2_data["s2_ckpt_dir"] = str(exp_dir)
            s2_data["save_weight_dir"] = str(sovits_weight_dir)
            s2_data["name"] = job["model_id"]
            s2_data["version"] = version
            (exp_dir / f"logs_s2_{version}").mkdir(parents=True, exist_ok=True)
            s2_tmp = self._job_dir(job_id) / "tmp_s2.json"
            s2_tmp.write_text(json.dumps(s2_data, ensure_ascii=False, indent=2), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                str(path)
                for path in (self.gsv_root, self.gsv_root / "GPT_SoVITS", self.gsv_root / "tools")
            )
            env["CUDA_VISIBLE_DEVICES"] = "0"
            env["_CUDA_VISIBLE_DEVICES"] = "0"
            env["version"] = version
            env["is_half"] = str(use_cuda)
            # 统一训练子进程输出编码，避免 Windows 控制台字符在训练日志中乱码。
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            self._set_status(job_id, status="running", stage="正在训练 SoVITS", progress=72, message="正在训练声音生成模型，期间界面可以最小化。")
            self._run_process(job_id, [str(self.python_exe), "-s", "GPT_SoVITS/s2_train.py", "--config", str(s2_tmp)], env, cancel)

            s1_data = yaml.safe_load((self.gsv_root / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml").read_text(encoding="utf-8"))
            s1_data["train"].update({"epochs": self.DEFAULT_EPOCHS, "batch_size": 1, "save_every_n_epoch": 1, "precision": "16-mixed" if use_cuda else "32-true", "if_save_latest": False, "if_save_every_weights": True, "if_dpo": False})
            # Windows 下多进程 DataLoader 容易在语义训练阶段卡在 worker 启动或预取。
            # 语义训练数据量通常很小，单进程加载更稳定，且不会阻塞 OwVoice 界面。
            s1_data.setdefault("data", {})["num_workers"] = 0
            s1_data["train_semantic_path"] = str(exp_dir / "6-name2semantic.tsv")
            s1_data["train_phoneme_path"] = str(exp_dir / "2-name2text.txt")
            s1_data["pretrained_s1"] = str(pretrained_root / "s1v3.ckpt")
            s1_data["output_dir"] = str(exp_dir / "logs_s1_v2Pro")
            s1_data["train"]["half_weights_save_dir"] = str(gpt_weight_dir)
            s1_data["train"]["exp_name"] = job["model_id"]
            s1_tmp = self._job_dir(job_id) / "tmp_s1.yaml"
            s1_tmp.write_text(yaml.safe_dump(s1_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self._set_status(job_id, stage="正在训练 GPT", progress=84, message="正在训练文字到语音的语义模型。")
            self._run_process(job_id, [str(self.python_exe), "-s", "GPT_SoVITS/s1_train.py", "--config_file", str(s1_tmp)], env, cancel)
            self._set_status(job_id, status="completed", stage="训练完成", progress=100, message="训练完成，请点击保存到本地模型库。")
        except Exception as exc:
            if cancel.is_set():
                self._set_status(job_id, status="interrupted", stage="训练已停止", message="训练已停止，可以继续训练。", error="")
            else:
                self._set_status(job_id, status="failed", stage="训练失败", message=str(exc), error=str(exc))
        finally:
            self._finish_thread(job_id)

    def finalize(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._load(job_id)
            if job.get("status") != "completed":
                raise TrainingError("训练尚未完成，不能保存模型。")
            job_dir = self._job_dir(job_id)
            exp_name = job["model_id"]
            gpt_candidates = sorted((self.gsv_root / "GPT_weights_v2Pro").glob(f"*{exp_name}*.ckpt"), key=lambda p: p.stat().st_mtime, reverse=True)
            sovits_candidates = sorted((self.gsv_root / "SoVITS_weights_v2Pro").glob(f"*{exp_name}*.pth"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not gpt_candidates or not sovits_candidates:
                raise TrainingError("找不到当前任务对应的训练权重，未保存其他任务的权重。请点击“训练日志”查看详情。")
            package = job_dir / "model_package"
            if package.exists():
                shutil.rmtree(package)
            package.mkdir()
            gpt_target = package / "GPT_weights" / "model.ckpt"
            sovits_target = package / "SoVITS_weights" / "model.pth"
            gpt_target.parent.mkdir()
            sovits_target.parent.mkdir()
            shutil.copy2(gpt_candidates[0], gpt_target)
            shutil.copy2(sovits_candidates[0], sovits_target)
            reference = next((Path(str(item.get("wav_path"))) for item in job.get("files", []) if Path(str(item.get("wav_path", ""))).is_file()), None)
            if reference is None:
                raise TrainingError("找不到训练参考音频。")
            reference_target = package / "reference.wav"
            shutil.copy2(reference, reference_target)
            avatar = Path(str(job.get("avatar_path", ""))).resolve()
            avatar_filename = ""
            if avatar.is_file() and job_dir in avatar.parents:
                avatar_filename = f"avatar{avatar.suffix.lower()}"
                shutil.copy2(avatar, package / avatar_filename)
            first_text = str(job.get("files", [{}])[0].get("text", ""))
            metadata = {
                "id": exp_name,
                "name": job["name"],
                "display_name": job["name"],
                "version": "0.1.0",
                "locale": self.LANGUAGE_LOCALE[job["language"]],
                "mode": "finetuned",
                "gpt_model": "GPT_weights/model.ckpt",
                "sovits_model": "SoVITS_weights/model.pth",
                "reference_audio": "reference.wav",
                "avatar": avatar_filename,
                "prompt_text": first_text,
                "prompt_language": job["language"],
                "enabled": True,
                "license_note": "用户本地训练模型；请确认训练音频和声音素材具有合法授权。",
            }
            (package / "model.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            from backend.model_manager import ModelManager

            model = ModelManager(self.project_dir).import_model(package)
            job["artifacts"] = {"package": str(package), "gpt": str(gpt_candidates[0]), "sovits": str(sovits_candidates[0])}
            job.update({"status": "registered", "stage": "已保存", "progress": 100, "message": "模型已保存。", "model": model, "updated_at": self._now()})
            self._save(job)
            return self._public(job)


TRAINING_MANAGER = TrainingManager(Path(os.environ.get("OWVOICE_PROJECT_DIR", Path(__file__).resolve().parents[1])))
