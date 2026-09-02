# OwVoice

OwVoice 是一个守望先锋角色本地语音合成工具，内置安娜、禅雅塔和末日铁拳三个角色，使用 GPT-SoVITS 进行本地推理。

## 普通用户使用

### 1. 下载

请从 GitHub 的 **Releases** 下载 `OwVoice-Setup-v*.exe`，不要下载页面上的 `Source code` 压缩包。

### 2. 使用前准备

- Windows 10/11 64 位
- 所有电脑都可以使用 CPU 推理，不需要 NVIDIA 显卡或 CUDA
- 检测到 NVIDIA 显卡时，可选择安装 GPU 加速，也可以选择继续使用 CPU
- 安装器会显示所需磁盘空间

安装器已包含可直接启动的 CPU 运行时、推理依赖和模型。普通用户不需要安装 Python或运行脚本；只有主动选择 GPU 加速时才会联网下载 NVIDIA 运行组件。

### 3. 安装和启动

1. 双击 `OwVoice-Setup-v*.exe`。
2. 按向导完成安装。
3. 从开始菜单打开 OwVoice；也可以在安装时选择创建桌面快捷方式。

### 4. 生成语音

1. 选择角色。
2. 输入配音文案。
3. 拖动语速滑块，或点击倍率数字精确输入。语速范围为 `0.10x` 到 `3.00x`。
4. 设置 WAV 文件名和输出目录。
5. 点击“合成语音”。
6. 合成完成后可以播放、打开文件或删除 WAV 文件。

> 提示：CPU 首次加载语音引擎可能需要几分钟，请耐心等待。GPU 组件只需确认一次，下载或启用失败时会自动回退 CPU。

## 常见问题

### 推理速度较慢

未安装 GPU 组件时使用 CPU 推理，首次加载和长文本合成需要更多时间。详细错误可查看安装目录中的 `logs` 目录。

### 检测到 NVIDIA 显卡后可以不安装吗

可以。选择“本次使用 CPU”不会下载任何文件，下次启动仍可重新选择。GPU 组件约需下载 2.6–3.2 GB，并需要 7–9 GB 临时可用空间。

### 合成失败

请查看：

- `logs\gpt_sovits.error.log`
- `logs\backend.error.log`
- `logs\warmup.error.log`

### 启动很慢

首次启动需要加载模型和启动本地服务，等待时间较长属于正常现象。启动页会显示当前阶段和进度。

## 发布包内容

安装包中包含：

- 私有 Python 3.10 运行时和 PySide6 界面
- 可选、自动校验且可安全回退的 NVIDIA GPU 组件安装流程
- OwVoice 后端与 GPT-SoVITS 推理依赖
- 三个角色模型和参考音频
- GPT-SoVITS 推理代码
- 配置模板和许可证说明

普通用户不需要修改配置文件，也不需要直接运行 Python 文件或脚本。

## 开发者构建

```powershell
.\scripts\setup.ps1
.\scripts\build_installer.ps1 -Version v0.1.1
```

构建机需要 Python 3.10 x64、Inno Setup，以及未提交到 Git 的三套角色模型。脚本生成 `dist\installer\OwVoice-Setup-v*-Universal.exe` 和 SHA256 manifest。开发诊断功能默认关闭，不进入用户界面。

## 版权与使用说明

OwVoice 与暴雪娱乐没有官方关系。

本项目仅供学习和非商业二次创作使用。部分模型、参考音频、头像等素材来源于网络，相关权利归原作者或权利人所有。请勿将本项目及其素材用于商业用途或未经授权的公开传播，使用者需自行确认合规性。

GPT-SoVITS 的许可证见项目中的 `LICENSE` 文件，第三方组件说明见 `THIRD_PARTY_NOTICES.md`。
