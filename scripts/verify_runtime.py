"""验证 OwVoice 运行环境是否满足推理或本地训练所需条件。"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import sys
from pathlib import Path


CORE_IMPORTS = (
    "torch",
    "torchaudio",
    "numpy",
    "scipy",
    "librosa",
    "soundfile",
    "fastapi",
    "uvicorn",
    "requests",
    "pydantic",
    "transformers",
    "huggingface_hub",
    "matplotlib",
    "PySide6",
    "pytorch_lightning",
    "torchmetrics",
    "peft",
    "onnxruntime",
    "nltk",
)

TEXT_IMPORTS = (
    "cn2an",
    "pypinyin",
    "jieba_fast",
    "split_lang",
    "fast_langdetect",
    "wordsegment",
    "g2p_en",
    "pyopenjtalk",
    "opencc",
    "ToJyutping",
    "g2pk2",
    "ko_pron",
)

ENGINE_IMPORTS = (
    "feature_extractor.cnhubert",
    "module.models",
    "AR.models.t2s_lightning_module",
    "text.cleaner",
    "BigVGAN.bigvgan",
)

TRAINING_IMPORTS = (
    "yaml",
    "psutil",
    "tensorboard",
    "gradio",
    "funasr",
    "modelscope",
    "av",
)


def import_group(names: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 展示具体失败模块
            result[name] = f"失败：{type(exc).__name__}: {exc}"
        else:
            result[name] = "通过"
    return result


def add_engine_paths(project_dir: Path) -> None:
    gsv_root = project_dir / "GPT-SoVITS"
    gsv_package = gsv_root / "GPT_SoVITS"
    for path in (gsv_root, gsv_package):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def check_files(project_dir: Path, *, training: bool = False) -> dict[str, str]:
    required = {
        "engine_api": project_dir / "GPT-SoVITS" / "api.py",
        "ffmpeg": project_dir / "GPT-SoVITS" / "ffmpeg.exe",
        "ffprobe": project_dir / "GPT-SoVITS" / "ffprobe.exe",
        "g2pw": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "text" / "G2PWModel" / "g2pW.onnx",
    }
    if training:
        required.update(
            {
                "training_script_s1": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "s1_train.py",
                "training_script_s2": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "s2_train.py",
                "dataset_script_text": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "prepare_datasets" / "1-get-text.py",
                "dataset_script_hubert": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "prepare_datasets" / "2-get-hubert-wav32k.py",
                "dataset_script_sv": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "prepare_datasets" / "2-get-sv.py",
                "dataset_script_semantic": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "prepare_datasets" / "3-get-semantic.py",
                "training_config": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "configs" / "s2v2Pro.json",
                "hubert": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "chinese-hubert-base" / "config.json",
                "roberta": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large" / "config.json",
                "speaker_model": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
                "sovits_base": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Gv2Pro.pth",
                "sovits_discriminator": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Dv2Pro.pth",
                "gpt_base": project_dir / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models" / "s1v3.ckpt",
            }
        )
    return {name: "通过" if path.is_file() else f"缺失：{path}" for name, path in required.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--skip-files", action="store_true", help="只检查 Python 导入，不检查项目资源文件")
    parser.add_argument("--training", action="store_true", help="额外检查本地训练依赖、脚本和预训练模型")
    args = parser.parse_args()
    project_dir = args.project_dir.resolve()
    add_engine_paths(project_dir)

    result = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cuda_available": False,
        "core_imports": import_group(CORE_IMPORTS),
        "text_imports": import_group(TEXT_IMPORTS),
        "engine_imports": import_group(ENGINE_IMPORTS),
    }
    if args.training:
        result["training_imports"] = import_group(TRAINING_IMPORTS)
    try:
        import torch

        result["cuda_available"] = bool(torch.cuda.is_available())
        result["torch"] = torch.__version__
        result["cuda"] = torch.version.cuda
    except Exception as exc:  # noqa: BLE001
        result["torch_error"] = f"{type(exc).__name__}: {exc}"

    if not args.skip_files:
        result["project_files"] = check_files(project_dir, training=args.training)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    statuses = []
    for group in ("core_imports", "text_imports", "engine_imports", "training_imports"):
        if group not in result:
            continue
        statuses.extend(result[group].values())
    if not args.skip_files:
        statuses.extend(result["project_files"].values())
    return 0 if all(status == "通过" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
