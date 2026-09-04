# 人物配置

公开发布版不包含角色模型权重。首次启动没有模型也可以进入空工作台。

用户可以在“本地模型库”导入自己训练且具有合法授权的模型；模型文件默认保存在 项目目录\data\models，应用更新不会覆盖这里的内容。模型库界面和首页只读取 `data\models\installed-models.json`。从模型库移除模型时只删除注册表条目，不删除模型文件。

`voices.local.json` 仅用于兼容旧版本配置，不再作为首页模型来源。

请自行确认训练音频、参考音频、声音素材及模型的授权情况，并遵守所在地法律法规。
## 本地模型目录规范

每个模型放在 `项目目录\data\models\<模型ID>\`，并在 `data\models\installed-models.json` 中登记。模型目录只保留一组明确使用的 GPT、SoVITS 权重和参考音频，避免把多个训练候选文件平铺在同一个角色目录中。

示例：

```text
项目目录\data\models\ana\
├─ model.json
├─ avatar.png                 # 可选
├─ GPT_weights\model.ckpt
├─ SoVITS_weights\model.pth
└─ reference.wav
```

`model.json` 中的 `gpt_model`、`sovits_model`、`reference_audio` 必须是相对于模型目录的路径。旧版本的 `models\<id>` 可以使用 `scripts\normalize_local_models.ps1` 迁移到规范目录；脚本不会删除旧模型文件。
