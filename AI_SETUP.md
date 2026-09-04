# 用 AI 协助配置 OwVoice

如果 `setup.ps1` 报错，可以先在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_env.ps1
```

然后把 `environment-report.txt` 的内容和完整报错一起发给 AI，并说明你要使用 CPU 还是 GPU。

AI 应根据报告判断 Python 版本、系统位数、NVIDIA 驱动、CUDA/PyTorch 和依赖问题，再给出针对当前电脑的检查或修复命令。不要把密码、令牌等敏感信息发给 AI，也不要直接执行来源不明的命令。

正常配置方式：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

CPU：下载较少，不需要 NVIDIA 显卡，但推理较慢。

GPU：需要 NVIDIA 显卡和兼容的 CUDA 驱动/运行组件，下载较多，但推理更快。
