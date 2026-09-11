import json
import shutil

import pytest

from backend.model_catalog import (
    InstalledModel,
    ModelCatalogError,
    load_installed_models,
    register_installed_model,
    save_installed_models,
    validate_model_id,
)
from backend.model_manager import ModelManager, ModelManagerError


def test_model_registry_round_trip(tmp_path):
    model = InstalledModel(
        id="character.example",
        name="示例角色",
        version="1.0.0",
        path="models/character.example",
        files=["model.json"],
    )

    save_installed_models(tmp_path, [model])

    assert load_installed_models(tmp_path) == [model]


def test_register_replaces_same_model_id(tmp_path):
    first = InstalledModel("character.example", "旧版", "1.0.0", "old")
    second = InstalledModel("character.example", "新版", "2.0.0", "new")

    register_installed_model(tmp_path, first)
    register_installed_model(tmp_path, second)

    assert load_installed_models(tmp_path) == [second]


def test_model_id_rejects_path_traversal():
    try:
        validate_model_id("../unsafe")
    except ModelCatalogError:
        pass
    else:
        raise AssertionError("路径穿越模型 ID 未被拒绝")


def test_remove_model_only_removes_registry_entry(tmp_path):
    model_root = tmp_path / "data" / "models" / "ana"
    model_root.mkdir(parents=True)
    (model_root / "model.json").write_text('{"id":"ana"}\n', encoding="utf-8")
    model = InstalledModel("ana", "安娜", "1.0.0", "models/ana", ["model.json"])
    save_installed_models(tmp_path, [model])

    ModelManager(tmp_path).remove_model("ana")

    assert model_root.is_dir()
    assert (model_root / "model.json").is_file()
    assert load_installed_models(tmp_path) == []


def test_import_reregisters_existing_unlisted_model_directory(tmp_path):
    model_root = tmp_path / "data" / "models" / "ana"
    model_root.mkdir(parents=True)
    (model_root / "model.json").write_text(
        '{"id":"ana","name":"安娜","gpt_model":"gpt.ckpt",'
        '"sovits_model":"sovits.pth","reference_audio":"reference.wav"}\n',
        encoding="utf-8",
    )
    for name in ("gpt.ckpt", "sovits.pth", "reference.wav"):
        (model_root / name).write_bytes(b"model")

    imported = ModelManager(tmp_path).import_model(model_root)

    assert imported["id"] == "ana"
    assert [item.id for item in load_installed_models(tmp_path)] == ["ana"]


def _write_test_model(root, model_id):
    root.mkdir(parents=True)
    (root / "model.json").write_text(
        f'{{"id":"{model_id}","name":"{model_id}",'
        '"gpt_model":"gpt.ckpt","sovits_model":"sovits.pth",'
        '"reference_audio":"reference.wav"}\n',
        encoding="utf-8",
    )
    for name in ("gpt.ckpt", "sovits.pth", "reference.wav"):
        (root / name).write_bytes(b"model")


def test_import_model_collection_copies_each_model_to_managed_root(tmp_path):
    collection = tmp_path / "待导入模型"
    _write_test_model(collection / "voice001", "voice001")
    _write_test_model(collection / "voice002", "voice002")

    result = ModelManager(tmp_path).import_models(collection)

    assert len(result["imported"]) == 2
    assert result["failed"] == []
    for model_id in ("voice001", "voice002"):
        assert (tmp_path / "data" / "models" / model_id / "model.json").is_file()
    assert [item.id for item in load_installed_models(tmp_path)] == ["voice001", "voice002"]


def test_import_rejects_flat_files_in_model_root(tmp_path):
    model_root = tmp_path / "data" / "models"
    _write_test_model(model_root, "flat_voice")

    try:
        ModelManager(tmp_path).import_models(model_root)
    except Exception as exc:  # noqa: BLE001 - 只验证根目录散放被拒绝
        assert "不能把模型文件直接放在" in str(exc)
    else:
        raise AssertionError("data/models 根目录散放模型未被拒绝")


def test_import_registers_unlisted_model_already_in_managed_root(tmp_path):
    model_root = tmp_path / "data" / "models" / "voice001"
    _write_test_model(model_root, "voice001")

    result = ModelManager(tmp_path).import_models(tmp_path / "data" / "models")

    assert [item["id"] for item in result["imported"]] == ["voice001"]
    assert [item.id for item in load_installed_models(tmp_path)] == ["voice001"]


def test_import_rejects_executable_files(tmp_path):
    source = tmp_path / "package"
    source.mkdir()
    (source / "model.json").write_text(
        '{"id":"safe","gpt_model":"gpt.ckpt",'
        '"sovits_model":"sovits.pth","reference_audio":"reference.wav"}\n',
        encoding="utf-8",
    )
    for name in ("gpt.ckpt", "sovits.pth", "reference.wav"):
        (source / name).write_bytes(b"model")
    (source / "payload.exe").write_bytes(b"not allowed")

    try:
        ModelManager(tmp_path).import_model(source)
    except Exception as exc:  # noqa: BLE001 - 只验证导入被拒绝
        assert "禁止导入" in str(exc)
    else:
        raise AssertionError("模型包中的可执行文件未被拒绝")


def test_old_absolute_registry_path_is_migrated_when_model_is_local(tmp_path):
    model_root = tmp_path / "data" / "models" / "juno"
    _write_test_model(model_root, "juno")
    registry = tmp_path / "data" / "models" / "installed-models.json"
    registry.write_text(
        '{"schema":1,"models":[{"id":"juno","name":"朱诺",'
        '"version":"1.0.0","path":"D:\\\\OwVoice\\\\data\\\\models\\\\juno",'
        '"files":["model.json"]}]}',
        encoding="utf-8",
    )

    ModelManager(tmp_path)

    models = load_installed_models(tmp_path)
    assert models[0].path == "models/juno"
    assert "D:\\OwVoice" not in registry.read_text(encoding="utf-8")


def test_old_absolute_registry_keeps_renamed_local_directory(tmp_path):
    model_root = tmp_path / "data" / "models" / "朱诺__juno"
    _write_test_model(model_root, "juno")
    registry = tmp_path / "data" / "models" / "installed-models.json"
    registry.write_text(
        '{"schema":1,"models":[{"id":"juno","name":"朱诺",'
        '"version":"1.0.0","path":"D:\\\\OldOwVoice\\\\data\\\\models\\\\朱诺__juno",'
        '"files":["model.json"]}]}',
        encoding="utf-8",
    )

    models = load_installed_models(tmp_path)

    assert models[0].path == "models/朱诺__juno"
    assert "OldOwVoice" not in registry.read_text(encoding="utf-8")


def test_external_import_does_not_overwrite_unregistered_target(tmp_path):
    source = tmp_path / "incoming" / "juno"
    _write_test_model(source, "juno")
    target = tmp_path / "data" / "models" / "juno"
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)

    with pytest.raises(ModelManagerError, match="目标目录已存在但未登记"):
        ModelManager(tmp_path).import_model(source)
    assert load_installed_models(tmp_path) == []


def test_import_managed_collection_uses_json_id_and_keeps_directory_name(tmp_path):
    model_root = tmp_path / "data" / "models" / "朱诺模型"
    _write_test_model(model_root, "juno")

    result = ModelManager(tmp_path).import_models(tmp_path / "data" / "models")

    assert [item["id"] for item in result["imported"]] == ["juno"]
    assert result["skipped"] == []
    assert result["failed"] == []
    assert load_installed_models(tmp_path)[0].path == "models/朱诺模型"
    assert not (tmp_path / "data" / "models" / "juno").exists()


def test_importing_managed_collection_twice_is_idempotent(tmp_path):
    model_root = tmp_path / "data" / "models" / "voice-folder"
    _write_test_model(model_root, "voice001")
    manager = ModelManager(tmp_path)

    first = manager.import_models(tmp_path / "data" / "models")
    second = manager.import_models(tmp_path / "data" / "models")

    assert len(first["imported"]) == 1
    assert second["imported"] == []
    assert [item["id"] for item in second["skipped"]] == ["voice001"]
    assert second["failed"] == []


def test_import_rejects_model_id_changed_inside_registered_directory(tmp_path):
    model_root = tmp_path / "data" / "models" / "voice-folder"
    _write_test_model(model_root, "voice001")
    manager = ModelManager(tmp_path)
    manager.import_models(tmp_path / "data" / "models")
    metadata = json.loads((model_root / "model.json").read_text(encoding="utf-8"))
    metadata["id"] = "voice002"
    (model_root / "model.json").write_text(json.dumps(metadata), encoding="utf-8")

    result = manager.import_models(tmp_path / "data" / "models")

    assert result["imported"] == []
    assert "该目录已登记为模型 voice001" in result["failed"][0]["error"]


def test_import_replaces_stale_registry_entry_when_registered_directory_is_missing(tmp_path):
    stale = InstalledModel("voice001", "旧登记", "1.0.0", "models/missing")
    save_installed_models(tmp_path, [stale])
    source = tmp_path / "外部模型" / "voice"
    _write_test_model(source, "voice001")

    result = ModelManager(tmp_path).import_models(source)

    assert [item["id"] for item in result["imported"]] == ["voice001"]
    models = load_installed_models(tmp_path)
    assert len(models) == 1
    assert models[0].path == "models/voice001"


def test_import_external_chinese_path_copies_by_json_id(tmp_path):
    source = tmp_path / "外部模型" / "任意文件夹名称"
    _write_test_model(source, "voice001")

    result = ModelManager(tmp_path).import_models(source)

    assert [item["id"] for item in result["imported"]] == ["voice001"]
    assert (tmp_path / "data" / "models" / "voice001" / "model.json").is_file()
    assert source.is_dir()


def test_invalid_json_shape_returns_readable_error(tmp_path):
    source = tmp_path / "bad-model"
    source.mkdir()
    (source / "model.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    result = ModelManager(tmp_path).import_models(source)

    assert result["imported"] == []
    assert "顶层必须是对象" in result["failed"][0]["error"]


def test_collection_reports_invalid_model_without_rolling_back_valid_one(tmp_path):
    collection = tmp_path / "外部模型集合"
    _write_test_model(collection / "valid", "valid")
    invalid = collection / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "model.json").write_text("{broken", encoding="utf-8")

    result = ModelManager(tmp_path).import_models(collection)

    assert [item["id"] for item in result["imported"]] == ["valid"]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["source"].endswith("invalid")
    assert (tmp_path / "data" / "models" / "valid" / "model.json").is_file()


def test_import_api_treats_already_registered_models_as_success(monkeypatch):
    import backend.server as server

    class FakeModelManager:
        def import_models(self, _source_dir):
            return {
                "imported": [],
                "skipped": [{"source": "D:/models/juno", "id": "juno"}],
                "failed": [],
            }

    monkeypatch.setattr(server, "MODEL_MANAGER", FakeModelManager())

    result = server.import_local_model(server.LocalModelImportRequest(source_dir="D:/models"))

    assert result["status"] == "imported"
    assert result["models"] == []
    assert result["skipped"][0]["id"] == "juno"


def test_import_api_returns_clear_error_when_every_model_fails(monkeypatch):
    import backend.server as server
    from fastapi import HTTPException

    class FakeModelManager:
        def import_models(self, _source_dir):
            return {
                "imported": [],
                "skipped": [],
                "failed": [{"source": "D:/models/bad", "error": "model.json 无效"}],
            }

    monkeypatch.setattr(server, "MODEL_MANAGER", FakeModelManager())

    with pytest.raises(HTTPException, match="没有成功导入任何模型") as exc_info:
        server.import_local_model(server.LocalModelImportRequest(source_dir="D:/models"))

    assert exc_info.value.status_code == 400
    assert "model.json 无效" in exc_info.value.detail
