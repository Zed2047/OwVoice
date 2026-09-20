from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from frontend.app import (
    InferenceProcessStopBridge,
    TrainingRequestHandle,
    _run_inference_process_stop,
)
from frontend.startup import StartupController


def test_controller_stops_services_without_touching_finished_worker(tmp_path: Path) -> None:
    controller = StartupController(tmp_path)
    calls: list[str] = []
    controller.runtime = SimpleNamespace(stop_all=lambda: calls.append("all"))
    controller.worker = SimpleNamespace(
        stop_services=lambda: (_ for _ in ()).throw(AssertionError("不应访问已结束 worker"))
    )

    controller.stop_services()

    assert calls == ["all"]


def test_startup_warmup_thread_does_not_capture_qobject_worker() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "startup.py").read_text(
        encoding="utf-8-sig"
    )

    assert "target=self.warmup_gpt_sovits" not in source
    assert "target=warmup_gpt_sovits" in source


def test_training_release_uses_controller_instead_of_startup_worker() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )

    method = source.split("def stop_inference_service_for_training", 1)[1].split(
        "@Slot(int)", 1
    )[0]
    assert "_run_inference_process_stop" in method
    assert "threading.Thread(" in method
    assert "QThread(" not in method
    assert "startup_worker.stop_service" not in method

    worker = source.split("def _run_inference_process_stop", 1)[1].split(
        "class ModelCatalogWorker", 1
    )[0]
    assert 'controller.stop_service("gpt_sovits")' in worker


def test_training_release_does_not_stop_process_on_gui_thread() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    method = source.split("def stop_inference_service_for_training", 1)[1].split(
        "@Slot()", 1
    )[0]

    assert ".stop_pid(" not in method
    assert ".stop_service(" not in method


def test_inference_process_stop_thread_uses_plain_controller() -> None:
    calls: list[str] = []
    controller = SimpleNamespace(stop_service=lambda role: calls.append(role))
    handle = TrainingRequestHandle(0)
    bridge = InferenceProcessStopBridge()
    succeeded: list[bool] = []
    bridge.success.connect(lambda: succeeded.append(True))

    _run_inference_process_stop(handle, bridge, controller, 0)

    assert calls == ["gpt_sovits"]
    assert succeeded == [True]
    assert handle.done.is_set()


def test_close_event_does_not_block_gui_thread_with_qthread_wait() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    method = source.split("def closeEvent", 1)[1].split("def normalized_filename", 1)[0]

    assert ".wait(" not in method


def test_training_requests_do_not_create_short_lived_qthreads() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    request_method = source.split("def _ow_training_request(", 1)[1].split(
        "def _ow_training_request_loaded", 1
    )[0]
    heartbeat_method = source.split("def _ow_training_start_heartbeat", 1)[1].split(
        "def _ow_training_confirm_stop", 1
    )[0]

    assert "QThread(" not in request_method
    assert "threading.Thread(" in request_method
    assert "QThread(" not in heartbeat_method
    assert "threading.Thread(" in heartbeat_method


def test_startup_does_not_reselect_backend_active_voice() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )

    assert "backend_already_selected" in source


def test_startup_voice_catalog_is_loaded_off_the_gui_thread() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )
    load_method = source.split("def load_voices(", 1)[1].split(
        "def _voice_catalog_loaded", 1
    )[0]
    finish_method = source.split("def finish_startup", 1)[1].split(
        "def build_sidebar", 1
    )[0]

    assert "threading.Thread(" in load_method
    assert "requests.get(" not in load_method
    assert "callback=self._finish_startup_after_voices" in finish_method
    assert "self.load_voices()" not in finish_method


def test_startup_protects_gui_priority_from_model_loading_burst() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "startup.py").read_text(
        encoding="utf-8-sig"
    )

    assert "ABOVE_NORMAL_PRIORITY_CLASS" in source
    assert "BELOW_NORMAL_PRIORITY_CLASS" in source
    assert 'environment["OMP_NUM_THREADS"]' in source
    assert 'environment["MKL_NUM_THREADS"]' in source


def test_startup_has_ui_liveness_diagnostics() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "app.py").read_text(
        encoding="utf-8-sig"
    )

    assert "_check_startup_ui_liveness" in source
    assert "启动界面事件循环停顿" in source


def test_patched_startup_run_is_an_explicit_qt_slot() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "startup.py").read_text(
        encoding="utf-8-sig"
    )
    patched_run = source.split("def _ow_run_safe", 1)[0].rsplit("\n", 3)[-3:]

    assert any("@Slot()" in line for line in patched_run)


def test_patched_startup_run_is_forced_onto_the_worker_thread() -> None:
    source = (Path(__file__).parents[1] / "frontend" / "startup.py").read_text(
        encoding="utf-8-sig"
    )
    controller_start = source.split("class StartupController", 1)[1].split(
        "def _startup_finished", 1
    )[0]

    assert "Qt.ConnectionType.DirectConnection" in controller_start
