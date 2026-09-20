from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_SCRIPT = PROJECT_ROOT / "scripts" / "update_transaction.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


pytestmark = pytest.mark.skipif(not POWERSHELL.is_file(), reason="未找到 Windows PowerShell 5.1")


FAKE_ENVIRONMENT_MODULE = r'''function Test-OwVoiceEnvironmentPreflight { param($ProjectRoot, $Mode, $WithTraining, $RequiredFreeBytes) return [PSCustomObject]@{ ok=$true; code="OK" } }
function New-OwVoiceCandidateEnvironment { param($ProjectRoot, $TransactionId, $CandidatePath, $Mode, $WithTraining) New-Item -ItemType Directory -Force -Path $CandidatePath | Out-Null; [IO.File]::WriteAllText((Join-Path $CandidatePath "marker.txt"), "candidate"); return [PSCustomObject]@{ ok=$true; code="OK" } }
function Test-OwVoiceCandidateEnvironment { param($ProjectRoot, $TransactionId, $CandidatePath, $Mode, $WithTraining, [switch]$VerifyProjectResources) return [PSCustomObject]@{ ok=$true; code="OK"; state=[PSCustomObject]@{ environment_fingerprint="f"; python=[PSCustomObject]@{ version="3.10.10" } } } }
function Read-OwVoiceEnvironmentSpec { param($ProjectRoot) return [PSCustomObject]@{ python=[PSCustomObject]@{ version="3.10.10" }; dependencies=[PSCustomObject]@{}; resources=[PSCustomObject]@{} } }
function New-OwVoiceEnvironmentState { param($Spec, $ProjectRoot, $Mode, $WithTraining, $EnvironmentFingerprint, $Python, $Verified, $TransactionId) return [PSCustomObject]@{ schema=1; mode=$Mode; with_training=$WithTraining; environment_fingerprint=$EnvironmentFingerprint; transaction_id=$TransactionId } }
function Switch-OwVoiceEnvironment { param($ProjectRoot, $TransactionId, $CandidatePath, $State) Move-Item -LiteralPath (Join-Path $ProjectRoot ".venv") -Destination (Join-Path $ProjectRoot ".venv.old"); Move-Item -LiteralPath $CandidatePath -Destination (Join-Path $ProjectRoot ".venv"); return [PSCustomObject]@{ ok=$true; code="OK" } }
function Restore-OwVoiceEnvironment { param($ProjectRoot, $TransactionId) Remove-Item -LiteralPath (Join-Path $ProjectRoot ".venv") -Recurse -Force; Move-Item -LiteralPath (Join-Path $ProjectRoot ".venv.old") -Destination (Join-Path $ProjectRoot ".venv"); return [PSCustomObject]@{ ok=$true; code="OK" } }
function Complete-OwVoiceEnvironmentTransaction { param($ProjectRoot, $TransactionId) Remove-Item -LiteralPath (Join-Path $ProjectRoot ".venv.old") -Recurse -Force; return [PSCustomObject]@{ ok=$true; code="OK" } }
Export-ModuleMember -Function *
'''


def test_legacy_retry_accepts_cached_layout_without_version_file(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "models").mkdir(parents=True)
    (tmp_path / "OwVoice.exe").write_text("fixture", encoding="utf-8")
    recovery = tmp_path / ".cache" / "updates" / "recovery"
    recovery.mkdir(parents=True)
    (recovery / "legacy-release-layout.json").write_text(
        '{"schema":1,"managedItems":[],"preservePaths":[]}', encoding="utf-8"
    )
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TRANSACTION_SCRIPT),
            "-TargetDirectory",
            str(tmp_path),
            "-RecoverOnly",
            "-AllowLegacyTargetWithoutIdentity",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已处理未完成的 OwVoice 更新事务" in result.stdout


def test_joint_update_environment_transaction_switches_rolls_back_and_commits(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "marker.txt").write_text("old", encoding="utf-8")
    (tmp_path / "scripts" / "environment").mkdir(parents=True)
    (tmp_path / "scripts" / "environment" / "OwVoice.Environment.psm1").write_text(
        FAKE_ENVIRONMENT_MODULE, encoding="utf-8"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "data" / "models").mkdir(parents=True)
    (tmp_path / "OwVoice.exe").write_text("fixture", encoding="utf-8")
    (tmp_path / "version.json").write_text('{"version":"0.2.0"}', encoding="utf-8")
    (tmp_path / "release-layout.json").write_text('{"schema":1}', encoding="utf-8")

    environment = os.environ.copy()
    environment["OWVOICE_TEST_ROOT"] = str(tmp_path)
    environment["OWVOICE_TRANSACTION"] = str(TRANSACTION_SCRIPT)
    command = r'''
$ErrorActionPreference = "Stop"
. $env:OWVOICE_TRANSACTION -TargetDirectory $env:OWVOICE_TEST_ROOT -RecoverOnly
function Invoke-EnvironmentResourcePreparation { param($State) }
$ApproveEnvironmentMigration = $true
$transactionId = "11111111-1111-1111-1111-111111111111"
$journalPath = Join-Path $env:OWVOICE_TEST_ROOT ".cache\updates\transactions\$transactionId\journal.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $journalPath) | Out-Null
$journal = [ordered]@{
    schema=1; id=$transactionId; phase="verified"; target_dir=$env:OWVOICE_TEST_ROOT; backup_dir=(Join-Path $env:OWVOICE_TEST_ROOT "backup")
    managed_items=@(); environment=[ordered]@{ required=$true; eligible=$true; spec_valid=$true; mode="CPU"; with_training=$false; phase="prepared" }
}
Invoke-EnvironmentMigration $journal
$switched = (Get-Content -LiteralPath (Join-Path $env:OWVOICE_TEST_ROOT ".venv\marker.txt") -Raw) -eq "candidate"
$oldExists = Test-Path -LiteralPath (Join-Path $env:OWVOICE_TEST_ROOT ".venv.old") -PathType Container
Restore-EnvironmentForTransaction $journal
$restored = (Get-Content -LiteralPath (Join-Path $env:OWVOICE_TEST_ROOT ".venv\marker.txt") -Raw) -eq "old"
$journal.environment.phase = "prepared"
Invoke-EnvironmentMigration $journal
Complete-EnvironmentForTransaction $journal
[ordered]@{ switched=$switched; old_exists_during_switch=$oldExists; restored=$restored; old_exists_after_commit=(Test-Path -LiteralPath (Join-Path $env:OWVOICE_TEST_ROOT ".venv.old")); committed_phase=$journal.environment.phase } | ConvertTo-Json -Compress
'''
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(next(line for line in reversed(result.stdout.splitlines()) if line.strip()))
    assert payload == {
        "switched": True,
        "old_exists_during_switch": True,
        "restored": True,
        "old_exists_after_commit": False,
        "committed_phase": "committed",
    }
