# OwVoice

OwVoice 是一个本地运行的《守望先锋》角色语音合成工具。它将前端界面、人物配置和 GPT-SoVITS 推理引擎解耦，支持在多个角色音色之间切换并生成 WAV 配音。

## 项目结构

```text
OwVoice/
├─ backend/                 # 本地 HTTP 服务
├─ frontend/                # PySide6 桌面前端
├─ config/                  # 人物配置模板
├─ scripts/                 # 启动脚本
├─ models/                  # 本地模型目录，不提交模型文件
├─ outputs/                 # 合成结果，不提交到仓库
├─ requirements.txt
└─ README.md
```

## 工作流程

```text
前端选择角色和文本
        ↓
OwVoice Backend
        ↓
切换角色 GPT/SoVITS 权重
        ↓
调用本机 GPT-SoVITS API
        ↓
返回 WAV 音频
```

## 本地准备

1. 安装 GPT-SoVITS，并确保其 API 监听 `http://127.0.0.1:9880`。
2. 复制 `config/voices.example.json` 为 `config/voices.local.json`。
3. 在 `voices.local.json` 中填写本机模型和参考音频路径。
4. 安装依赖：

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

5. 启动后端：

   ```powershell
   .\scripts\start_backend.ps1
   ```

6. 另开终端启动前端：

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python .\frontend\app.py
   ```

默认后端地址是 `http://127.0.0.1:8765`，GPT-SoVITS 地址是 `http://127.0.0.1:9880`。可以通过环境变量 `OWVOICE_GSV_API` 和 `OWVOICE_API` 修改。

## 模型与版权说明

本仓库不包含 GPT-SoVITS 引擎、模型权重、游戏原始语音或其他可能受版权保护的素材。使用者应自行确认训练数据、模型和生成音频的使用权限，并遵守相关法律法规。

