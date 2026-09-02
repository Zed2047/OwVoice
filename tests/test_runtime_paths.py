import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from frontend.startup import StartupError, StartupWorker, resolve_runtime_paths


class RuntimePathsTest(unittest.TestCase):
    def test_prefers_installed_runtime_and_falls_back_to_venv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_dir = Path(directory)
            self.assertEqual(
                resolve_runtime_paths(project_dir),
                (
                    project_dir / ".venv" / "Scripts" / "python.exe",
                    project_dir / ".venv" / "nltk_data",
                ),
            )
            (project_dir / "runtime").mkdir()
            self.assertEqual(
                resolve_runtime_paths(project_dir),
                (
                    project_dir / "runtime" / "python.exe",
                    project_dir / "runtime" / "nltk_data",
                ),
            )

    def test_gpu_start_failure_retries_once_with_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            for path in (
                project / "runtime" / "python.exe",
                project / "GPT-SoVITS" / "api.py",
                project / "models" / "voice.ckpt",
                project / "models" / "voice.pth",
                project / "models" / "reference.wav",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (project / "runtime" / "nltk_data" / "taggers" / "averaged_perceptron_tagger_eng").mkdir(parents=True)
            (project / "runtime" / "nltk_data" / "corpora" / "cmudict").mkdir(parents=True)
            voice = {
                "gpt_model": "models/voice.ckpt",
                "sovits_model": "models/voice.pth",
                "reference_audio": "models/reference.wav",
                "prompt_text": "测试。",
                "prompt_language": "zh",
            }
            worker = StartupWorker(project)
            worker.gpu_enabled = True
            gpu_process = Mock()
            cpu_process = Mock()
            with (
                patch("frontend.startup.port_open", return_value=False),
                patch.object(worker, "start_process", side_effect=(gpu_process, cpu_process)) as start,
                patch.object(worker, "wait_for_port", side_effect=(StartupError("gpu failed"), None)),
                patch.object(worker, "stop_process") as stop,
            ):
                worker.start_gpt_sovits(voice)

            self.assertEqual(start.call_count, 2)
            self.assertNotIn("-d", start.call_args_list[0].args[1])
            self.assertIn("-d", start.call_args_list[1].args[1])
            self.assertEqual(start.call_args_list[1].args[3]["OWVOICE_DISABLE_GPU"], "1")
            stop.assert_called_once_with(gpu_process)

    def test_cancel_race_stops_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            worker = StartupWorker(project)
            process = Mock()

            def create_process(*_args, **_kwargs):
                worker.cancel_event.set()
                return process

            with (
                patch("frontend.startup.subprocess.Popen", side_effect=create_process),
                patch.object(worker, "stop_process") as stop,
            ):
                with self.assertRaises(StartupError):
                    worker.start_process("race", ["fake.exe"], project)
            stop.assert_called_once_with(process)
            for handle in worker.log_files:
                handle.close()


if __name__ == "__main__":
    unittest.main()
