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
        for path in directory.rglob("*.ps1")
    )


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
foreach ($file in @(Get-ChildItem -LiteralPath (Join-Path $root "scripts") -Filter *.ps1 -Recurse -File) + @(Get-ChildItem -LiteralPath (Join-Path $root "updater") -Filter *.ps1 -Recurse -File)) {
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
