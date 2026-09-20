"""验证候选 OwVoice 环境；只输出结构化结果，不启动正式服务。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import struct
import subprocess
import sys
from pathlib import Path


def python_version_compatible(actual: dict, expected: dict) -> bool:
    """补丁版本可不同，但实现、主次版本和架构必须与规范一致。"""

    version = str(actual.get("version", ""))
    major_minor = ".".join(version.split(".")[:2])
    return (
        str(actual.get("implementation", "")) == str(expected.get("implementation", ""))
        and str(actual.get("architecture", "")) == str(expected.get("architecture", ""))
        and major_minor == str(expected.get("compatible_major_minor", ""))
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{__import__('uuid').uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def environment_fingerprint(spec: dict, mode: str, with_training: bool) -> str:
    dependencies = spec["dependencies"]
    value = "|".join(
        (
            str(spec["python"].get("version_range", spec["python"]["version"])),
            str(dependencies["pyproject_sha256"]),
            str(dependencies["lock_sha256"]),
            mode.upper(),
            str(bool(with_training)).lower(),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify(project_root: Path, mode: str, with_training: bool) -> dict:
    project_root = project_root.resolve()
    failures: list[str] = []
    spec_path = project_root / "environment-spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "code": "ENV_SPEC_INVALID", "failures": [str(exc)]}

    python = {
        "implementation": platform.python_implementation(),
        "version": ".".join(str(value) for value in sys.version_info[:3]),
        "architecture": "x64" if struct.calcsize("P") * 8 == 64 else "x86",
        "source": str(spec.get("python", {}).get("source", "")),
    }
    expected_python = spec.get("python", {})
    if not python_version_compatible(python, expected_python):
        failures.append(f"Python 不匹配：期望 {expected_python}，实际 {python}")

    # uv 创建的 venv 可以不携带 pip；锁定依赖已经由 uv sync --frozen 校验。
    if _module_available("pip"):
        pip_check = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if pip_check.returncode != 0:
            failures.append(f"pip check 失败：{(pip_check.stdout + pip_check.stderr).strip()}")

    required_modules = ["requests", "pydantic", "PySide6", "torch", "torchaudio", "transformers", "jieba", "nltk"]
    missing = [name for name in required_modules if not _module_available(name)]
    if missing:
        failures.append(f"缺少核心模块：{', '.join(missing)}")

    actual_mode = None
    try:
        import torch  # type: ignore

        actual_mode = "GPU" if "+cu" in str(torch.__version__) else "CPU"
        if mode.upper() != actual_mode:
            failures.append(f"Torch 模式不匹配：期望 {mode.upper()}，实际 {actual_mode}")
        if mode.upper() == "GPU" and not torch.cuda.is_available():
            failures.append("GPU 模式下 CUDA 不可用")
    except Exception as exc:  # noqa: BLE001 - 将验证异常转成结构化结果
        failures.append(f"Torch 验证失败：{exc}")

    training_modules = ["tensorboard", "gradio", "funasr", "modelscope", "av"]
    training_ready = all(_module_available(name) for name in training_modules)
    if with_training and not training_ready:
        failures.append("训练依赖不完整")

    resource_lock = project_root / str(spec.get("resources", {}).get("lock_file", "resource-lock.json"))
    if not resource_lock.is_file():
        failures.append(f"资源锁不存在：{resource_lock}")

    fingerprint = environment_fingerprint(spec, mode, with_training)
    environment_identity = {
        "python_version": str(python.get("version", "")),
        "python_version_range": str(expected_python.get("version_range", "")),
        "python_implementation": str(expected_python.get("implementation", "")),
        "architecture": str(expected_python.get("architecture", "")),
        "pyproject_sha256": str(spec.get("dependencies", {}).get("pyproject_sha256", "")).lower(),
        "lock_sha256": str(spec.get("dependencies", {}).get("lock_sha256", "")).lower(),
        "resource_schema": int(spec.get("resources", {}).get("schema", 0)),
    }

    return {
        "ok": not failures,
        "code": "OK" if not failures else "ENV_CANDIDATE_VERIFY_FAILED",
        "python": python,
        "mode": actual_mode or mode.upper(),
        "with_training": with_training,
        "training_ready": training_ready,
        "environment_fingerprint": fingerprint,
        "environment_identity": environment_identity,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("cpu", "gpu"), required=True)
    parser.add_argument("--with-training", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = verify(args.project_root, args.mode, args.with_training)
    _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
