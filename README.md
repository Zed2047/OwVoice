# OwVoice

OwVoice 是一个守望先锋角色本地语音合成工具，内置安娜、禅雅塔和末日铁拳三个角色，使用 GPT-SoVITS 进行本地推理。
大家好，项目目前先删除了release包，在这里对大家说声对不起。评论区反馈了这是种侵权行为，我决定将项目收束成一个可以自定义训练模型、导入模型的学习项目，目前在修改项目中，还请大家耐心等待，谢谢大家！
## 普通用户使用

### 1. 下载

请从 GitHub 的 **Releases** 下载 `OwVoice-v*.zip`，不要下载页面上的 `Source code` 压缩包。

将 ZIP 解压到一个有足够空间、并且有读写权限的目录，例如 `D:\OwVoice`。不建议解压到 `C:\Program Files` 等受保护目录。

### 2. 使用前准备

- Windows 10/11 64 位
- Python 3.10 64 位，建议从 [python.org](https://www.python.org/downloads/windows/) 安装
- NVIDIA 显卡及可用的 NVIDIA 驱动
- 首次配置需要联网
- 建议预留至少 5GB 可用空间

本项目使用项目目录内的 `.venv` 虚拟环境运行，不需要 Anaconda。首次配置会安装 Python 依赖、下载 GPT-SoVITS 推理模型并准备 NLTK 资源。

### 3. 首次配置

1. 双击 `setup.bat`。
2. 等待窗口显示 `Setup complete`。
3. 配置完成后关闭窗口。

首次配置因为要配置环境、下载模型，可能需要较长时间，请不要中途关闭窗口。
之后使用时直接双击 `OwVoice.exe`，不需要再次运行配置脚本。

### 4. 生成语音

1. 选择角色。
2. 输入配音文案。
3. 拖动语速滑块，或点击倍率数字精确输入。语速范围为 `0.10x` 到 `3.00x`。
4. 设置 WAV 文件名和输出目录。
5. 点击“合成语音”。
6. 合成完成后可以播放、打开文件或删除 WAV 文件。

> 提示：首次启动通常需要约 15 秒加载语音引擎，请耐心等待。

## 常见问题

### 找不到 Python

请安装 64 位 Python 3.10，并重新打开 `setup.bat`。如果电脑中安装了多个 Python，请确认 `py -3.10` 可以正常运行。

### PyTorch、CUDA 或显卡错误

请确认 NVIDIA 驱动正常，并使用支持 CUDA 的 NVIDIA 显卡。详细错误可查看 `logs` 目录中的日志文件。

### 合成失败

请先确认首次配置已经完成，然后查看：

- `logs\gpt_sovits.error.log`
- `logs\backend.error.log`
- `logs\warmup.error.log`

### 启动很慢

首次启动需要加载模型和启动本地服务，等待时间较长属于正常现象。启动页会显示当前阶段和进度。

## 发布包内容

发布 ZIP 中包含：

- `OwVoice.exe` 和运行文件
- `setup.bat` 首次配置脚本
- 三个角色模型和参考音频
- GPT-SoVITS 推理代码
- 配置模板和许可证说明

普通用户不需要修改配置文件，也不需要直接运行 Python 文件。

## 开发者构建

```powershell
.\scripts\build_exe.ps1
.\scripts\build_release.ps1
```

先构建 EXE，再生成 `dist\OwVoice-v*.zip`。开发诊断功能默认关闭，不进入用户界面。

## 版权与使用说明

OwVoice 与暴雪娱乐没有官方关系。

本项目仅供学习和非商业二次创作使用。部分模型、参考音频、头像等素材来源于网络，相关权利归原作者或权利人所有。请勿将本项目及其素材用于商业用途或未经授权的公开传播，使用者需自行确认合规性。

GPT-SoVITS 的许可证见项目中的 `LICENSE` 文件，第三方组件说明见 `THIRD_PARTY_NOTICES.md`。
