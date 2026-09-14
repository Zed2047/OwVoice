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

## v0.2.0 安装时下载资源

以下资源不放入公开 ZIP，由 `resource-lock.json` 固定来源、版本（或 revision）、大小和 SHA256；安装器只在校验通过后使用。资源锁文件是发布包的一部分，修改下载源必须重新审阅许可证和哈希。

| 资源 | 来源与固定标识 | 许可证/说明 | 分发方式 |
|---|---|---|---|
| FFmpeg essentials 9.0.1 | gyan.dev 固定 ZIP；SHA256 见 `resource-lock.json` | 静态构建为 GPLv3；使用时应保留上游许可要求 | 首次配置下载 |
| G2PWModel 1.1 | ModelScope `kamiorinn/g2pw` revision `827f4a519b083e3f37c790c938300241e75692d5` | 模型卡声明 Apache-2.0 | 首次配置下载 |
| fastText `lid.176.bin` | Meta fastText 官方公共文件；固定大小与 SHA256 | 模型使用 CC-BY-SA-3.0；fastText 项目代码为 MIT | 首次配置下载；失败可用依赖内置轻量模型 |
| NLTK `averaged_perceptron_tagger` | NLTK Data 官方 `gh-pages` 固定 ZIP 与 SHA256 | MIT；g2p_en 当前仍需要旧 pickle 资源 | 首次配置下载 |
| NLTK `averaged_perceptron_tagger_eng` | NLTK Data 官方固定 ZIP 与 SHA256 | MIT | 首次配置下载 |
| NLTK `cmudict` | NLTK Data 官方固定 ZIP 与 SHA256 | CMU 条款允许研究和商业使用，并要求来源致谢 | 首次配置下载 |
| GPT-SoVITS 预训练资源 | Hugging Face `lj1995/GPT-SoVITS` 固定 revision | 模型仓库标记 MIT；仍应遵守上游项目和各模型文件说明 | 首次配置下载 |

### 资源安全与覆盖规则

- 正式安装默认只接受 `resource-lock.json` 中的来源；`OWVOICE_LID_URL`、`OWVOICE_PRETRAINED_REPO`、`OWVOICE_PRETRAINED_REVISION` 等覆盖变量不会生效。
- 开发或测试需要替换来源时，必须显式设置 `OWVOICE_ALLOW_UNPINNED_RESOURCES=1`，不得把该变量写入正式安装入口。
- GPT-SoVITS 模型下载优先官方 Hugging Face，镜像只作为固定 revision 的备用源；下载后逐文件校验完整清单。
- 安装日志应记录资源名称、固定标识、校验结果和失败原因，不记录用户模型内容。
