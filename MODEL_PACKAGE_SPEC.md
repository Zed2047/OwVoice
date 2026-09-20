# OwVoice 本地模型包规范

## 目标

角色模型独立导入、安装和删除；程序本体、运行环境、公共底模与用户模型分开管理。当前版本的模型广场是本地模型库，不提供联网模型下载。

## 本地注册表

用户模型放在 `data/models/<模型目录>/`，注册信息保存在 `data/models/installed-models.json`。模型身份以 `model.json` 中的 `id` 为准，因此模型目录名可以与 ID 不同。应用更新不能覆盖 `data` 目录。

注册表中的每个模型至少包含：

```json
{
  "id": "character.example",
  "name": "示例角色",
  "version": "1.0.0",
  "path": "models/character.example",
  "files": ["model.json", "GPT_weights/model.ckpt", "SoVITS_weights/model.pth", "reference.wav"],
  "sha256": "",
  "minAppVersion": "0.0.0",
  "minEngineVersion": "0.0.0",
  "license_note": "用户本地模型，请自行确认授权。"
}
```

`data\models\installed-models.json` 是首页和本地模型库的唯一模型来源。用户可以在 `data\models\` 下保留尚未导入的模型目录，但程序不会自动扫描或使用这些目录。应用更新不应覆盖 `data` 目录。

## 模型包目录

模型目录内至少有 `model.json` 和一组明确的模型文件：

```text
character.example/
├─ model.json
├─ GPT_weights/
│  └─ model.ckpt
├─ SoVITS_weights/
│  └─ model.pth
└─ preview.png
```

用户应选择上面的单个模型目录导入，也可以选择一个包含多个此类模型目录的集合文件夹
进行批量导入。选择当前项目的 `data/models/` 时，程序会读取各一级子目录中的
`model.json` 并原地登记，不会重复复制大型权重；选择外部目录时，程序会先校验再复制到
本地模型库。未导入的模型可以放在用户自己的储存文件夹中，程序正常启动时只读取
`installed-models.json`，不会自动扫描或使用未登记目录。模型文件不得直接散放在
`data/models/` 根目录。

从模型库移除模型时，程序只移除注册表条目，不删除模型文件；当前正在使用的模型不能删除。旧版本使用 `models/<id>` 结构时，可以使用 `scripts\normalize_local_models.ps1` 迁移到规范目录；该脚本不会删除旧模型文件。`config\voices.local.json` 仅用于兼容旧版本，不再作为首页模型来源。

## `model.json` 格式

`model.json` 描述角色信息，路径都相对于角色目录，客户端安装后会自动转换为项目内路径：

```json
{
  "id": "character.example",
  "name": "示例角色",
  "display_name": "示例角色",
  "avatar": "preview.png",
  "locale": "zh-CN",
  "mode": "finetuned",
  "gpt_model": "GPT_weights/model.ckpt",
  "sovits_model": "SoVITS_weights/model.pth",
  "reference_audio": "reference.wav",
  "prompt_text": "角色参考文本。",
  "prompt_language": "zh",
  "enabled": true,
  "inference": {
    "top_k": 15,
    "top_p": 0.9,
    "temperature": 0.75,
    "sample_steps": 32,
    "cut_punc": "，。？！；：,.?!…"
  }
}
```

`gpt_model`、`sovits_model` 和 `reference_audio` 是必需项；GPT 权重必须是 `.ckpt`，SoVITS 权重必须是 `.pth`，参考音频支持 `.wav`、`.mp3`、`.flac`、`.m4a`、`.ogg` 和 `.aac`。`prompt_language` 支持 `zh`、`yue`、`en`、`ja`、`ko`；`avatar` 可省略。`inference` 中的数值必须在程序允许范围内，非法类型或越界值会拒绝导入。模型包只能包含模型和展示素材，不能通过元数据执行代码。

## 安全规则

- 本地导入完成后才写入注册表并显示“已安装”；如使用 zip 传递模型，导入前应校验文件大小和 SHA256。
- 禁止模型包包含 `.exe`、`.dll`、`.py`、`.ps1`、`.bat` 等可执行或脚本文件。
- 解压时拒绝绝对路径、`..` 路径和目录穿越。
- 导入先写入同盘临时目录，校验通过后再移动到角色目录。
- 当前使用中的角色不能删除；删除前提示切换角色。
- 模型包不覆盖配置、用户数据、公共底模和其他角色。
- `installed-models.json` 损坏时，程序保留只读诊断副本并生成恢复预览；只有用户明确确认后才会从一级模型目录中的合法 `model.json` 重建注册表。

## 更新规则

- 当前本地模型库不提供联网模型更新；`minAppVersion` 和 `minEngineVersion` 只作为模型兼容性元数据保留，不触发联网下载。
- 模型重新导入失败不应破坏旧版本。
- 重新导入同 ID 模型不会静默覆盖现有模型；需要替换时应保留原目录备份并按导入提示操作。
