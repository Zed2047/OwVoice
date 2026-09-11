# 第三方组件说明

## GPT-SoVITS

OwVoice 携带并修改了 GPT-SoVITS 的部分源码，将其作为本地语音推理和可选训练引擎使用。GPT-SoVITS 由 RVC-Boss 及贡献者开发，采用 MIT License；上游项目地址：<https://github.com/RVC-Boss/GPT-SoVITS>。

OwVoice 当前维护的主要修改文件包括：

- `GPT-SoVITS\api.py`、`webui.py`
- `GPT-SoVITS\GPT_SoVITS\AR\data\bucket_sampler.py`
- `GPT-SoVITS\GPT_SoVITS\AR\data\data_module.py`
- `GPT-SoVITS\GPT_SoVITS\s1_train.py`

修改主要用于 OwVoice 的本地服务接入、文本/标点处理和训练流程适配。发布包中保留 `GPT-SoVITS\LICENSE`。

## Python 依赖

Python 依赖和 PyInstaller 运行库分别由 `requirements-*.txt` 与发布包中的 `_internal` 提供。PyTorch、PySide6、FastAPI、NumPy 等组件各自适用其上游许可证；重新分发和使用时应遵守对应项目的许可证及其附带声明。

## uv

OwVoice 发布包内置 uv 0.12.12 的 Windows x64 官方签名可执行文件，用于下载项目私有 Python 和安装锁定依赖。uv 由 Astral 开发，采用 MIT 或 Apache-2.0 双重许可；上游项目地址：<https://github.com/astral-sh/uv>。

## 外部素材

OwVoice 的角色模型、参考音频和头像属于独立的外部素材，不自动受到 OwVoice 或 GPT-SoVITS 软件许可证覆盖。当前源码和发布包不包含角色模型文件。发布者和使用者应自行确认外部素材的来源、授权范围和传播合规性。
