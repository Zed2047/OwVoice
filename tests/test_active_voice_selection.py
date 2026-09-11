from pathlib import Path
from unittest.mock import Mock, patch


def test_select_voice_updates_selected_state():
    import backend.server as server

    fake_file = str(Path(server.__file__).resolve())
    voice = {
        "id": "doomfist",
        "gpt_model": fake_file,
        "sovits_model": fake_file,
        "reference_audio": fake_file,
    }
    previous = server._selected_voice_id
    try:
        with patch.object(server, "find_voice", return_value=voice):
            result = server.select_voice("doomfist")
        assert result == {"voice_id": "doomfist", "status": "selected"}
        assert server._selected_voice_id == "doomfist"
    finally:
        server._selected_voice_id = previous


def test_delete_only_blocks_the_loaded_voice_and_clears_deleted_selection():
    import backend.server as server

    calls = {}

    class FakeModelManager:
        def remove_model(self, model_id, active_model_id=None):
            calls["model_id"] = model_id
            calls["active_model_id"] = active_model_id

    previous_selected = server._selected_voice_id
    previous_active = server._active_voice_id
    previous_manager = server.MODEL_MANAGER
    try:
        server._selected_voice_id = "ana"
        server._active_voice_id = None
        server.MODEL_MANAGER = FakeModelManager()
        result = server.delete_model("ana")
        assert result == {"status": "deleted", "model_id": "ana"}
        assert calls == {"model_id": "ana", "active_model_id": None}
        assert server._selected_voice_id is None
    finally:
        server._selected_voice_id = previous_selected
        server._active_voice_id = previous_active
        server.MODEL_MANAGER = previous_manager


def test_release_active_model_clears_loaded_state_without_stopping_backend():
    import backend.server as server

    previous_key = server._active_model_key
    previous_active = server._active_voice_id
    try:
        server._active_model_key = "ana-model"
        server._active_voice_id = "ana"
        with patch.object(server, "engine_online", return_value=False):
            result = server.release_active_model()
        assert result == {"status": "unloaded"}
        assert server._active_model_key is None
        assert server._active_voice_id is None
    finally:
        server._active_model_key = previous_key
        server._active_voice_id = previous_active


def test_engine_model_ready_checks_loaded_model_paths():
    import backend.server as server

    gpt_path = Path(r"D:\OwVoice\data\models\zarya\GPT_weights\model.ckpt")
    sovits_path = Path(r"D:\OwVoice\data\models\zarya\SoVITS_weights\model.pth")
    response = Mock(status_code=200)
    response.json.return_value = {
        "loaded": True,
        "gpt_model_path": str(gpt_path).replace("\\", "/"),
        "sovits_model_path": str(sovits_path).replace("\\", "/"),
    }
    with patch.object(server.requests, "get", return_value=response):
        assert server.engine_model_ready(gpt_path, sovits_path)


def test_engine_model_ready_rejects_unloaded_engine():
    import backend.server as server

    response = Mock(status_code=200)
    response.json.return_value = {"loaded": False, "gpt_model_path": "", "sovits_model_path": ""}
    with patch.object(server.requests, "get", return_value=response):
        assert not server.engine_model_ready(Path("gpt.ckpt"), Path("sovits.pth"))
