import hashlib
import io
import contextlib
import tempfile
import unittest
import zipfile
import urllib.error
from pathlib import Path

from scripts.resource_download import (
    download_file,
    download_from_sources,
    ensure_zip_download,
    format_megabytes,
    safe_extract_all,
)


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResourceDownloadTests(unittest.TestCase):
    def test_download_progress_uses_megabytes_instead_of_raw_bytes(self):
        payload = b"x" * (2 * 1024 * 1024)

        def opener(request, timeout):
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                download_file(
                    "https://primary.example/asset.bin",
                    target,
                    attempts=1,
                    expected_size=len(payload),
                    expected_sha256=digest(payload),
                    opener=opener,
                )
            rendered = output.getvalue()
            self.assertIn("2.0 MB / 2.0 MB", rendered)
            self.assertEqual(rendered.count("下载进度："), 1)
            self.assertNotIn("2097152 字节", rendered)
            self.assertEqual(format_megabytes(len(payload)), "2.0 MB")

    def test_resume_uses_range_and_finishes_atomically(self):
        payload = b"OwVoice resource payload"
        requests = []

        def opener(request, timeout):
            requests.append(request.get_header("Range"))
            offset = len(b"OwVoice ")
            return FakeResponse(
                payload[offset:],
                status=206,
                headers={
                    "Content-Length": str(len(payload) - offset),
                    "Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}",
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            target.with_name("asset.bin.part").write_bytes(b"OwVoice ")
            download_file(
                "https://primary.example/asset.bin",
                target,
                attempts=1,
                expected_size=len(payload),
                expected_sha256=digest(payload),
                opener=opener,
            )
            self.assertEqual(requests, ["bytes=8-"])
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(target.with_name("asset.bin.part").exists())

    def test_range_unsupported_restarts_without_duplicate_bytes(self):
        payload = b"complete payload"
        requests = []

        def opener(request, timeout):
            requests.append(request.get_header("Range"))
            return FakeResponse(payload, status=200)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            target.with_name("asset.bin.part").write_bytes(b"stale")
            download_file(
                "https://primary.example/asset.bin",
                target,
                attempts=1,
                expected_size=len(payload),
                expected_sha256=digest(payload),
                opener=opener,
            )
            self.assertEqual(requests, ["bytes=5-"])
            self.assertEqual(target.read_bytes(), payload)

    def test_range_not_satisfiable_discards_partial_and_retries_from_zero(self):
        payload = b"fresh payload"
        calls = []

        def opener(request, timeout):
            calls.append(request.get_header("Range"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 416, "range", {}, None)
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            target.with_name("asset.bin.part").write_bytes(b"stale")
            download_file(
                "https://primary.example/asset.bin",
                target,
                attempts=2,
                expected_size=len(payload),
                expected_sha256=digest(payload),
                opener=opener,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(calls, ["bytes=5-", None])
            self.assertEqual(target.read_bytes(), payload)

    def test_corrupt_zip_cache_is_removed_and_redownloaded(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("resource.txt", "ok")
        payload = archive_bytes.getvalue()

        def opener(request, timeout):
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "resource.zip"
            target.write_bytes(b"not a zip")
            ensure_zip_download(
                "https://primary.example/resource.zip",
                target,
                expected_size=len(payload),
                expected_sha256=digest(payload),
                opener=opener,
            )
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read("resource.txt"), b"ok")

    def test_primary_failure_falls_back_to_mirror(self):
        payload = b"mirror payload"
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.endswith("primary.bin"):
                raise OSError("primary unavailable")
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            selected = download_from_sources(
                [
                    "https://primary.example/primary.bin",
                    "https://mirror.example/mirror.bin",
                ],
                target,
                attempts=1,
                expected_size=len(payload),
                expected_sha256=digest(payload),
                opener=opener,
            )
            self.assertEqual(selected, "https://mirror.example/mirror.bin")
            self.assertEqual(calls, [
                "https://primary.example/primary.bin",
                "https://mirror.example/mirror.bin",
            ])
            self.assertEqual(target.read_bytes(), payload)

    def test_size_or_hash_mismatch_leaves_no_final_target(self):
        payload = b"wrong payload"

        def opener(request, timeout):
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.bin"
            with self.assertRaises(RuntimeError):
                download_file(
                    "https://primary.example/asset.bin",
                    target,
                    attempts=1,
                    expected_size=len(payload) + 1,
                    expected_sha256=digest(payload),
                    opener=opener,
                )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name("asset.bin.part").exists())

    def test_safe_extract_rejects_duplicates_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("same.txt", "one")
                archive.writestr("same.txt", "two")
            with zipfile.ZipFile(duplicate) as archive:
                with self.assertRaises(RuntimeError):
                    safe_extract_all(archive, root / "duplicate-out")

            symlink = root / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (0o120777 << 16) | 0xA000
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(info, "outside.txt")
            with zipfile.ZipFile(symlink) as archive:
                with self.assertRaises(RuntimeError):
                    safe_extract_all(archive, root / "symlink-out")


if __name__ == "__main__":
    unittest.main()
