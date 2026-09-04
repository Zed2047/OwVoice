# OwVoice 本地环境与训练说明

## 默认安装

默认安装只准备 OwVoice 推理、桌面界面和后端所需环境，不下载或内置角色模型权重。首次配置遇到 NLTK 报错时，重新运行首次配置；重点检查 .venv\nltk_data\taggers\averaged_perceptron_tagger_eng 和 .venv\nltk_data\corpora\cmudict 是否存在。

如果自动安装失败，可在项目目录运行：
.\\.venv\Scripts\python.exe scripts\download_nltk_data.py

## 本地训练

推荐直接点击主界面的“本地训练”，使用 OwVoice 内置训练向导：

1. 填写模型名称并选择训练语言。
2. 上传自己的语音素材（支持 WAV、MP3、FLAC、M4A 等）。
3. 点击“自动识别文本”，然后在表格中检查、修改并保存文本。
4. 点击“准备训练数据”后开始训练；训练期间可以最小化窗口。
5. 训练完成后点击“保存到本地模型库”，返回工作台即可使用。

向导会自动调用项目内 GPT-SoVITS 的数据准备和训练脚本，并把每个任务的输出保存在 `data/training/jobs`，日志在对应任务目录的 `training.log`。当前自动识别以中文为主；其他语言可手动填写文本，但仍需要相应的训练资源。

`scripts\start_training.ps1` 仅保留给开发者排查 GPT-SoVITS 原始 WebUI，不是普通用户入口。训练失败时请优先展开 OwVoice 向导中的训练日志，不要直接运行原始 WebUI。

训练依赖属于可选组件，不放进默认运行环境；用户只有在确实需要训练时才安装 GPT-SoVITS 训练依赖。这样普通用户不会因为训练功能额外下载一整套大型依赖和缓存。

## 模型授权

OwVoice 不提供角色模型权重，也不提供联网模型广场。请仅使用自己拥有授权的音频、参考音频和模型，并自行承担使用模型产生内容的合规责任。
