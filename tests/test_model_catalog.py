from backend.model_catalog import (
    InstalledModel,
    ModelCatalogError,
    load_installed_models,
    register_installed_model,
    save_installed_models,
    validate_model_id,
)
from backend.model_manager import ModelManager


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
