# OwVoice

OwVoice 是一个守望先锋角色本地语音合成工具，使用 GPT-SoVITS 进行本地推理。
OwVoice学习交流q群:1121380498，欢迎大家学习讨论项目bug、可优化点、模型等等。

## 普通用户使用

### 1. 下载

请从 GitHub 的 **Releases** 下载 `OwVoice-v*.zip`，不要下载页面上的 `Source code` 压缩包。

将 ZIP 解压到一个有足够空间、并且有读写权限的目录，例如 `D:\OwVoice`。不建议解压到 `C:\Program Files` 等受保护目录。

### 2. 使用前准备

- Windows 10/11 64 位
- 64 位 Python 3.10（从 python.org 安装）
- 首次配置需要联网
- CPU 模式不需要 NVIDIA 显卡；GPU 模式需要 NVIDIA 显卡和兼容的 CUDA 驱动/运行组件
- 发布包解压、Python 虚拟环境和推理资源合计需要较大空间；建议至少预留 20GB，可选训练还会额外下载训练底模

首次配置会在项目目录创建 `.venv`，按选择安装固定版本的 CPU 或 GPU 依赖，再下载 GPT-SoVITS 推理资源和 NLTK 资源。

### 3. 首次配置

1. 双击 `setup.bat`。
2. 选择 CPU 或 GPU：CPU 下载少但推理慢；GPU 下载多但推理快。
3. 等待窗口显示配置完成。
4. 配置完成后关闭窗口。

首次配置因为要配置环境、下载模型，可能需要较长时间，请不要中途关闭窗口。
之后使用时直接双击 `OwVoice.exe`，不需要再次运行配置脚本。

### 4. 生成语音

1. 选择角色。
2. 输入配音文案。
3. 拖动语速滑块，或点击倍率数字精确输入。语速范围为 `0.10x` 到 `3.00x`。
4. 设置 WAV 文件名和输出目录。
5. 点击“合成语音”。
6. 点击“本地模型库”可导入、查看或删除本地模型；模型不会联网下载。
7. 点击“检查更新”可更新程序和 GPT-SoVITS 代码；配置、模型、输出文件和缓存会保留。
8. 合成完成后可以播放、打开文件或删除 WAV 文件。

> 提示：首次启动通常需要约 15 秒加载语音引擎，请耐心等待。

## 常见问题

### 首次配置提示找不到运行环境

确认已安装 64 位 Python 3.10，并重新打开 `setup.bat`。若配置失败，可先运行 `powershell -ExecutionPolicy Bypass -File .\scripts\check_env.ps1`，再把 `environment-report.txt` 和完整报错发送给 AI。

排查时应同时说明选择的是 CPU 还是 GPU。AI 只应根据环境报告和完整报错给出检查建议；不要发送密码、令牌等敏感信息，也不要直接执行来源不明的命令。

### PyTorch、CUDA 或显卡错误

GPU 模式请确认 NVIDIA 驱动和 `nvidia-smi` 正常；没有 NVIDIA 显卡时请选择 CPU。详细错误可查看 `logs` 目录中的日志文件。

### 合成失败

请先确认首次配置已经完成，然后查看：

- `logs\gpt_sovits.error.log`
- `logs\backend.error.log`
- `logs\warmup.error.log`

### 启动很慢

首次启动需要加载模型和启动本地服务，等待时间较长属于正常现象。启动页会显示当前阶段和进度。

### 可选：本地训练

点击侧边栏“本地训练”后，程序会先检查训练环境。只有确认安装时，才会安装训练依赖并下载训练预训练模型；基础推理用户不需要承担这部分下载。

本地训练推荐使用 GPU。训练运行时会根据 `torch.cuda.is_available()` 自动选择设备：当前环境有可用 CUDA 时使用 GPU，否则使用 CPU。CPU 可以运行，但速度会明显变慢。训练需要用户自行准备并确认拥有授权的音频、参考音频和头像素材。

训练向导流程：

1. 填写模型名称并选择训练语言。
2. 上传自己的语音素材（支持 WAV、MP3、FLAC、M4A 等）。
3. 点击“自动识别文本”，检查、修改并保存文本。
4. 点击“准备训练数据”后开始训练；训练期间可以最小化窗口。
5. 训练完成后点击“保存到本地模型库”，返回工作台即可使用。

任务文件保存在 `data\training\jobs`，每个任务的日志在对应目录的 `training.log`。当前自动识别以中文为主；其他语言可以手动填写文本，但仍需要相应训练资源。

默认安装只准备推理、桌面界面和后端环境，不下载或内置角色模型权重。首次配置遇到 NLTK 报错时，可以重新运行 `setup.bat`；也可以在项目目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\download_nltk_data.py
```

`scripts\start_training.ps1` 仅供开发者排查 GPT-SoVITS 原始 WebUI，不是普通用户入口。训练失败时，应优先查看 OwVoice 向导中的训练日志。

## 发布包内容

发布 ZIP 中包含：

- `OwVoice.exe` 和运行文件
- `setup.bat` 首次配置脚本
- 固定版本 CPU/GPU 依赖清单；用户环境安装在项目目录的 `.venv`
- 本地模型库目录（初始为空，模型和参考音频需由用户自行准备或导入）
- 本地模型库、本地训练入口和程序更新功能（训练依赖与训练底模按需安装）
- GPT-SoVITS 推理代码
- 配置模板和许可证说明

普通用户不需要修改配置文件，也不需要直接运行 Python 文件。

## 开发者构建

```powershell
.\scripts\setup.ps1
.\scripts\run.ps1
uv sync --extra cpu  # 或 uv sync --extra gpu，用于开发环境
.\scripts\build_exe.ps1
.\scripts\build_release.ps1
```

开发环境依赖由 `requirements-cpu.txt`、`requirements-gpu.txt`、`requirements-training.txt`、`pyproject.toml` 和 `uv.lock` 固定。CPU/GPU 清单是实际安装入口，`pyproject.toml` 与 `uv.lock` 供 uv 开发环境使用；先构建 EXE，再按需生成发布包。暂不生成 ZIP 进行日常测试。

模型包格式和导入规则见 `MODEL_PACKAGE_SPEC.md`；第三方许可证见 `THIRD_PARTY_NOTICES.md`。

## 版权与使用说明

OwVoice 与暴雪娱乐没有官方关系。

OwVoice 项目源代码按根目录 `LICENSE` 中的 MIT License 发布。当前 GitHub 源码和发布包不包含角色模型权重、角色参考音频或角色头像。

用户自行导入或训练的模型、音频、声音素材和头像不属于 OwVoice 源代码许可的范围。使用者必须自行确认这些外部素材的来源、授权范围和传播合规性，不得未经授权公开传播或使用。

GPT-SoVITS 的许可证见 `GPT-SoVITS\LICENSE`，其他直接携带的第三方组件说明见 `THIRD_PARTY_NOTICES.md`。
