from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _powershell_scripts() -> list[Path]:
    return sorted(
        path
        for directory in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "updater")
        if directory.is_dir()
        for pattern in ("*.ps1", "*.psm1", "*.psd1")
        for path in directory.rglob(pattern)
    )


def _batch_files() -> list[Path]:
    files: list[Path] = []
    for directory in (PROJECT_ROOT, PROJECT_ROOT / "scripts", PROJECT_ROOT / "updater"):
        if not directory.is_dir():
            continue
        for pattern in ("*.bat", "*.cmd"):
            iterator = directory.glob(pattern) if directory == PROJECT_ROOT else directory.rglob(pattern)
            files.extend(iterator)
    return sorted(files)


def test_powershell_scripts_are_utf8_bom_crlf() -> None:
    scripts = _powershell_scripts()
    assert scripts
    for path in scripts:
        data = path.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), path
        body = data[3:]
        assert b"\r\n" in body, path
        assert b"\n" not in body.replace(b"\r\n", b""), path
        assert b"\r" not in body.replace(b"\r\n", b""), path


def test_batch_scripts_are_ascii_crlf() -> None:
    batches = _batch_files()
    assert batches
    for path in batches:
        data = path.read_bytes()
        assert all(byte < 0x80 for byte in data), path
        assert b"\n" not in data.replace(b"\r\n", b""), path
        assert b"\r" not in data.replace(b"\r\n", b""), path


def test_legacy_update_bridge_uses_direct_release_assets_without_github_api() -> None:
    script = (PROJECT_ROOT / "scripts" / "repair_update_legacy.ps1").read_text(encoding="utf-8-sig")

    assert "api.github.com" not in script
    assert "Invoke-RestMethod" not in script
    assert "[Console]::OutputEncoding" not in script
    assert "chcp 65001" not in (PROJECT_ROOT / "修复更新器.bat").read_text(encoding="ascii").lower()
    assert 'https://github.com/$Repository/releases/download/$Version' in script
    assert '$manifestUrl = "$releaseDownloadBase/$manifestName"' in script
    assert '$archiveUrl = "$releaseDownloadBase/$archiveName"' in script
    assert "Get-OwVoiceReleaseIdentity" not in script
    assert '[string]$Version = "v0.2.0"' in script
    assert "AllowLegacyTargetWithoutIdentity" in script
    assert "Update repair failed" not in (PROJECT_ROOT / "修复更新器.bat").read_text(encoding="ascii")
    updater = (PROJECT_ROOT / "updater" / "update_release.ps1").read_text(encoding="utf-8-sig")
    assert "legacy-release-layout-v0.1.2.json" in updater
    source_entry = (PROJECT_ROOT / "scripts" / "update_release.ps1").read_text(encoding="utf-8-sig")
    assert ".cache\\updates\\recovery\\update_transaction.ps1" in source_entry


@pytest.mark.skipif(os.name != "nt", reason="仅在 Windows 上验证 PowerShell 语法")
def test_powershell_scripts_parse_in_windows_powershell_51() -> None:
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        pytest.skip("未找到 Windows PowerShell 5.1")
    script = r'''
$ErrorActionPreference = "Stop"
    $root = [System.IO.Path]::GetFullPath($env:OWVOICE_TEST_ROOT)
$tokens = $null
$errors = $null
$failed = @()
    $files = @(
        foreach ($directory in @("scripts", "updater")) {
            $path = Join-Path $root $directory
            if (Test-Path -LiteralPath $path -PathType Container) {
                foreach ($pattern in @("*.ps1", "*.psm1", "*.psd1")) {
                    Get-ChildItem -LiteralPath $path -Filter $pattern -Recurse -File
                }
            }
        }
    )
    foreach ($file in $files) {
        [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
        if ($errors.Count -gt 0) { $failed += $file.FullName }
    }
if ($failed.Count -gt 0) { $failed | Write-Output; exit 1 }
'''
    env = os.environ.copy()
    env["OWVOICE_TEST_ROOT"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="仅在 Windows 上验证 PowerShell 模块导入")
def test_powershell_module_import_is_verified(tmp_path: Path) -> None:
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        pytest.skip("未找到 Windows PowerShell 5.1")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    module = scripts / "broken.psm1"
    module.write_bytes("throw 'module import failed'\r\n".encode("utf-8-sig"))
    gate = PROJECT_ROOT / "scripts" / "verify_text_compatibility.ps1"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(gate), "-ProjectRoot", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
