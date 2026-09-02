import hashlib
import io
import runpy
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frontend import gpu_runtime


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int, content_range: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": content_range}

    def getcode(self) -> int:
        return self.status


class GpuRuntimeTest(unittest.TestCase):
    def test_sitecustomize_activates_only_with_marker(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "runtime_sitecustomize.py"
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            script = runtime / "sitecustomize.py"
            script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            gpu_site = runtime / "gpu-site"
            gpu_site.mkdir()
            original_path = list(sys.path)
            try:
                runpy.run_path(str(script))
                self.assertNotEqual(sys.path[0], str(gpu_site))
                (runtime / "gpu.enabled").touch()
                runpy.run_path(str(script))
                self.assertEqual(sys.path[0], str(gpu_site))
            finally:
                sys.path[:] = original_path

    def test_resumes_and_verifies_download(self) -> None:
        self.assertFalse(gpu_runtime._uses_cu128("GeForce RTX 4090", "8.9"))
        self.assertTrue(gpu_runtime._uses_cu128("GeForce RTX 5090", "12.0"))
        self.assertFalse(gpu_runtime.driver_is_supported(gpu_runtime.NvidiaGpu("RTX 4090", "551.23", "cu126")))
        self.assertTrue(gpu_runtime.driver_is_supported(gpu_runtime.NvidiaGpu("RTX 4090", "566.36", "cu126")))
        payload = b"abcdefgh"
        wheel = ("fake.whl", "https://example.invalid/fake.whl", len(payload), hashlib.sha256(payload).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / "fake.whl.part").write_bytes(payload[:3])
            response = FakeResponse(payload[3:], 206, "bytes 3-7/8")
            with patch("frontend.gpu_runtime.urllib.request.urlopen", return_value=response):
                result = gpu_runtime._download_wheel(wheel, cache, 0, len(payload), lambda _: None, None)
            self.assertEqual(result.read_bytes(), payload)

            bad_wheel = ("bad.whl", "https://example.invalid/bad.whl", len(payload), "0" * 64)
            with patch(
                "frontend.gpu_runtime.urllib.request.urlopen",
                return_value=FakeResponse(payload, 200),
            ):
                with self.assertRaises(gpu_runtime.GpuRuntimeError):
                    gpu_runtime._download_wheel(
                        bad_wheel, cache, 0, len(payload), lambda _: None, None
                    )
            self.assertFalse((cache / "bad.whl.part").exists())

            oversized = ("large.whl", "https://example.invalid/large.whl", 4, "0" * 64)
            with patch(
                "frontend.gpu_runtime.urllib.request.urlopen",
                return_value=FakeResponse(payload, 200),
            ):
                with self.assertRaises(gpu_runtime.GpuRuntimeError):
                    gpu_runtime._download_wheel(
                        oversized, cache, 0, oversized[2], lambda _: None, None
                    )
            self.assertFalse((cache / "large.whl.part").exists())

    def test_safe_extract_rejects_traversal_and_skips_build_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "safe.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("torch/runtime.py", "ok = True")
                archive.writestr("torch/testing/helper.py", "ok = True")
                archive.writestr("torch/include/header.h", "unused")
                archive.writestr("torch/lib/link.lib", "unused")
            target = root / "target"
            target.mkdir()
            gpu_runtime._extract_wheels([wheel], target, 1024 * 1024, lambda _: None, None)
            self.assertTrue((target / "torch" / "runtime.py").is_file())
            self.assertTrue((target / "torch" / "testing" / "helper.py").is_file())
            self.assertFalse((target / "torch" / "include").exists())
            self.assertFalse((target / "torch" / "lib" / "link.lib").exists())

            unsafe = root / "unsafe.whl"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape.py", "bad = True")
            with zipfile.ZipFile(unsafe) as archive:
                with self.assertRaises(gpu_runtime.GpuRuntimeError):
                    gpu_runtime._safe_wheel_members(archive, 1024 * 1024)

    def test_install_activates_only_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            runtime = project / "runtime"
            runtime.mkdir()
            python_exe = runtime / "python.exe"
            python_exe.touch()
            wheel = project / "fake.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("torch/__init__.py", "__version__ = 'fake'")

            with (
                patch("frontend.gpu_runtime._acquire_install_mutex", return_value=None),
                patch("frontend.gpu_runtime._download_wheel", return_value=wheel),
                patch("frontend.gpu_runtime.probe_gpu_component", return_value="Test GPU"),
                patch(
                    "frontend.gpu_runtime.shutil.disk_usage",
                    return_value=SimpleNamespace(free=20 * gpu_runtime.GIB),
                ),
            ):
                name = gpu_runtime.install_gpu_component(
                    project, python_exe, "cu126", lambda _: None
                )

            self.assertEqual(name, "Test GPU")
            self.assertEqual(gpu_runtime.active_gpu_component(project), "cu126")
            self.assertTrue((runtime / "gpu-site" / "torch" / "__init__.py").is_file())

            with (
                patch("frontend.gpu_runtime._acquire_install_mutex", return_value=None),
                patch("frontend.gpu_runtime._download_wheel", return_value=wheel),
                patch(
                    "frontend.gpu_runtime.probe_gpu_component",
                    side_effect=gpu_runtime.GpuRuntimeError("probe failed"),
                ),
                patch(
                    "frontend.gpu_runtime.shutil.disk_usage",
                    return_value=SimpleNamespace(free=20 * gpu_runtime.GIB),
                ),
            ):
                with self.assertRaises(gpu_runtime.GpuRuntimeError):
                    gpu_runtime.install_gpu_component(
                        project, python_exe, "cu126", lambda _: None
                    )
            self.assertIsNone(gpu_runtime.active_gpu_component(project))
            self.assertTrue(python_exe.is_file())


if __name__ == "__main__":
    unittest.main()
