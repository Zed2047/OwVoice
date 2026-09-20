from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "environment" / "OwVoice.Environment.psm1"
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


pytestmark = pytest.mark.skipif(not POWERSHELL.is_file(), reason="未找到 Windows PowerShell 5.1")


def run_environment_command(root: Path, expression: str) -> dict:
    command = f"Import-Module -Name $env:OWVOICE_ENV_MODULE -Force; $result = {expression}; $result | ConvertTo-Json -Depth 12 -Compress"
    environment = os.environ.copy()
    environment["OWVOICE_ENV_MODULE"] = str(MODULE_PATH)
    environment["OWVOICE_ENV_ROOT"] = str(root)
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
    return json.loads(result.stdout.strip())


def write_spec(root: Path) -> None:
    uv_path = root / "tools" / "uv" / "uv.exe"
    uv_path.parent.mkdir(parents=True)
    uv_path.write_bytes(b"test uv")
    (root / "resource-lock.json").write_text("{}", encoding="utf-8")
    pyproject = root / "pyproject.toml"
    lock = root / "uv.lock"
    pyproject.write_text("[project]\nname='test'\n", encoding="utf-8")
    lock.write_text("version = 1\n", encoding="utf-8")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    spec = {
        "schema": 1,
        "python": {
            "implementation": "CPython",
            "version": "3.10.10",
            "version_range": ">=3.10,<3.11",
            "compatible_major_minor": "3.10",
            "tested_versions": ["3.10.10", "3.10.21"],
            "architecture": "x64",
            "source": "existing-system",
        },
        "dependencies": {
            "pyproject_sha256": digest(pyproject),
            "lock_sha256": digest(lock),
            "modes": ["CPU", "GPU"],
            "training_extra": "training",
        },
        "resources": {"schema": 2, "lock_file": "resource-lock.json"},
        "tools": {"uv_version": "0.12.12", "uv_sha256": digest(uv_path)},
    }
    (root / "environment-spec.json").write_text(json.dumps(spec), encoding="utf-8")


def test_environment_spec_is_strictly_readable(tmp_path: Path) -> None:
    write_spec(tmp_path)
    result = run_environment_command(
        tmp_path,
        "Read-OwVoiceEnvironmentSpec -ProjectRoot $env:OWVOICE_ENV_ROOT",
    )
    assert result["schema"] == 1
    assert result["python"]["version"] == "3.10.10"
    assert result["python"]["version_range"] == ">=3.10,<3.11"
    assert result["dependencies"]["modes"] == ["CPU", "GPU"]


def test_powershell_python_compatibility_accepts_31021_only_with_matching_runtime() -> None:
    accepted = run_environment_command(
        PROJECT_ROOT,
        "Test-OwVoicePythonCompatible "
        "([PSCustomObject]@{ implementation='CPython'; version='3.10.21'; architecture='x64' }) "
        "([PSCustomObject]@{ implementation='CPython'; compatible_major_minor='3.10'; architecture='x64' })",
    )
    rejected = run_environment_command(
        PROJECT_ROOT,
        "Test-OwVoicePythonCompatible "
        "([PSCustomObject]@{ implementation='CPython'; version='3.11.0'; architecture='x64' }) "
        "([PSCustomObject]@{ implementation='CPython'; compatible_major_minor='3.10'; architecture='x64' })",
    )
    assert accepted is True
    assert rejected is False


def test_windows_powershell_preflight_reads_free_space_without_psdrive_cim(tmp_path: Path) -> None:
    write_spec(tmp_path)
    result = run_environment_command(
        tmp_path,
        "Test-OwVoiceEnvironmentPreflight -ProjectRoot $env:OWVOICE_ENV_ROOT -Mode CPU "
        "-WithTraining:$false -RequiredFreeBytes 1 -UvPath (Join-Path $env:OWVOICE_ENV_ROOT 'tools\\uv\\uv.exe')",
    )
    assert result["ok"] is True
    source = MODULE_PATH.read_text(encoding="utf-8-sig")
    assert "System.IO.DriveInfo" in source
    assert ".PSDrive" not in source


def test_candidate_environment_uses_existing_python_and_project_uv_cache() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8-sig")
    env_setup = source.index("$env:UV_PROJECT_ENVIRONMENT = $candidate")
    venv_call = source.index('Invoke-OwVoiceTrackedProcess -FilePath $UvPath -Arguments @("venv"', env_setup)
    assert "$env:UV_CACHE_DIR = $cache" in source[env_setup:venv_call]
    assert "function Get-OwVoiceExistingPythonPath" in source
    assert "$existingPython = Get-OwVoiceExistingPythonPath" in source
    assert "--managed-python" not in source[venv_call : source.index("$created = $true", venv_call)]
    assert "python install" not in source[env_setup : source.index("$created = $true", env_setup)]
    assert "Get-OwVoicePythonInfo -PythonPath $command.Source" in source


def test_environment_plan_distinguishes_create_and_repair(tmp_path: Path) -> None:
    write_spec(tmp_path)
    create = run_environment_command(
        tmp_path,
        "Get-OwVoiceEnvironmentPlan -ProjectRoot $env:OWVOICE_ENV_ROOT -Mode CPU -WithTraining:$false",
    )
    assert create["action"] == "create"

    (tmp_path / ".venv").mkdir()
    repair = run_environment_command(
        tmp_path,
        "Get-OwVoiceEnvironmentPlan -ProjectRoot $env:OWVOICE_ENV_ROOT -Mode CPU -WithTraining:$false",
    )
    assert repair["action"] == "repair"


def test_legacy_setup_state_is_migrated_without_deleting_source(tmp_path: Path) -> None:
    write_spec(tmp_path)
    legacy = tmp_path / ".cache" / "setup-state.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps(
            {
                "schema": 2,
                "dependenciesReady": True,
                "trainingReady": False,
                "mode": "CPU",
                "environmentSha256": "legacy-fingerprint",
                "python": "3.10|64|CPython",
            }
        ),
        encoding="utf-8",
    )
    state = run_environment_command(
        tmp_path,
        "Get-OwVoiceEnvironmentState -ProjectRoot $env:OWVOICE_ENV_ROOT -MigrateLegacy",
    )
    assert state["schema"] == 1
    assert state["legacy"] is True
    assert state["python_version"] == "3.10.10"
    assert state["python_version_range"] == ">=3.10,<3.11"
    assert state["architecture"] == "x64"
    assert state["resource_schema"] == 2
    assert legacy.is_file()
    assert (tmp_path / ".runtime" / "environment-state.json").is_file()


def test_environment_state_builder_records_lock_identity(tmp_path: Path) -> None:
    write_spec(tmp_path)
    state = run_environment_command(
        tmp_path,
        "New-OwVoiceEnvironmentState -Spec (Read-OwVoiceEnvironmentSpec -ProjectRoot $env:OWVOICE_ENV_ROOT) "
        "-ProjectRoot $env:OWVOICE_ENV_ROOT -Mode GPU -WithTraining:$true "
        "-EnvironmentFingerprint 'aabb' -Verified:$true",
    )
    assert state["python_version"] == "3.10.10"
    assert state["python_version_range"] == ">=3.10,<3.11"
    assert state["python_implementation"] == "CPython"
    assert state["architecture"] == "x64"
    assert state["pyproject_sha256"]
    assert state["lock_sha256"]
    assert state["resource_schema"] == 2
    assert state["mode"] == "GPU"
    assert state["with_training"] is True


def test_environment_state_builder_records_actual_compatible_patch_version(tmp_path: Path) -> None:
    write_spec(tmp_path)
    state = run_environment_command(
        tmp_path,
        "New-OwVoiceEnvironmentState -Spec (Read-OwVoiceEnvironmentSpec -ProjectRoot $env:OWVOICE_ENV_ROOT) "
        "-ProjectRoot $env:OWVOICE_ENV_ROOT -Mode GPU -WithTraining:$false "
        "-EnvironmentFingerprint 'aabb' -Verified:$true "
        "-Python ([PSCustomObject]@{ implementation='CPython'; version='3.10.21'; architecture='x64' })",
    )
    assert state["python_version"] == "3.10.21"
    assert state["python_version_range"] == ">=3.10,<3.11"


def test_environment_switch_and_restore_only_touch_transaction_paths(tmp_path: Path) -> None:
    write_spec(tmp_path)
    old = tmp_path / ".venv"
    candidate = tmp_path / ".venv.next"
    old.mkdir()
    candidate.mkdir()
    (old / "marker.txt").write_text("old", encoding="utf-8")
    (candidate / "marker.txt").write_text("new", encoding="utf-8")
    transaction_id = "11111111-1111-1111-1111-111111111111"
    state = {"schema": 1, "mode": "CPU", "with_training": False, "environment_fingerprint": "new"}
    encoded_state = json.dumps(state).replace("'", "''")
    switched = run_environment_command(
        tmp_path,
        "Switch-OwVoiceEnvironment -ProjectRoot $env:OWVOICE_ENV_ROOT -TransactionId '"
        + transaction_id
        + "' -CandidatePath (Join-Path $env:OWVOICE_ENV_ROOT '.venv.next') -State (ConvertFrom-Json -InputObject '"
        + encoded_state
        + "')",
    )
    assert switched["ok"] is True
    assert (tmp_path / ".venv" / "marker.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / ".venv.old" / "marker.txt").read_text(encoding="utf-8") == "old"

    restored = run_environment_command(
        tmp_path,
        "Restore-OwVoiceEnvironment -ProjectRoot $env:OWVOICE_ENV_ROOT -TransactionId '" + transaction_id + "'",
    )
    assert restored["ok"] is True
    assert (tmp_path / ".venv" / "marker.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / ".venv.next").exists()


def test_candidate_path_outside_project_is_rejected(tmp_path: Path) -> None:
    write_spec(tmp_path)
    outside = tmp_path.parent / "outside-venv"
    result = run_environment_command(
        tmp_path,
        "New-OwVoiceCandidateEnvironment -ProjectRoot $env:OWVOICE_ENV_ROOT -TransactionId '11111111-1111-1111-1111-111111111111' -CandidatePath '"
        + str(outside).replace("'", "''")
        + "' -Mode CPU -WithTraining:$false",
    )
    assert result["ok"] is False
    assert result["code"] == "ENV_CANDIDATE_PATH_INVALID"


def test_install_and_training_entries_delegate_environment_decisions() -> None:
    setup = (PROJECT_ROOT / "scripts" / "setup_v2.ps1").read_text(encoding="utf-8-sig")
    training = (PROJECT_ROOT / "scripts" / "setup_training.ps1").read_text(encoding="utf-8-sig")
    for script in (setup, training):
        assert "Get-OwVoiceEnvironmentPlan" in script
        assert "Test-OwVoiceEnvironmentPreflight" in script
        assert "New-OwVoiceCandidateEnvironment" in script
        assert "Test-OwVoiceCandidateEnvironment" in script
        assert "Switch-OwVoiceEnvironment" in script
        assert "Restore-OwVoiceEnvironment" in script
        assert "Invoke-OwVoiceTrackedProcess" in script
        assert "HF_HUB_DISABLE_PROGRESS_BARS" in script


def test_release_and_update_bridge_include_environment_core() -> None:
    release = (PROJECT_ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8-sig")
    bridge_build = (PROJECT_ROOT / "scripts" / "build_update_bridge.ps1").read_text(encoding="utf-8-sig")
    assert '"environment-spec.json"' in release
    assert 'Join-Path $projectRoot "scripts\\environment"' in release
    assert '"update.json"' in release
    assert 'build_release_manifest.py' in release
    bridge = (PROJECT_ROOT / "scripts" / "repair_update_legacy.ps1").read_text(encoding="utf-8-sig")
    assert '$manifestName = "update.json"' in bridge
    assert '$manifest.package.sha256' in bridge
    assert 'Get-LegacyEnvironmentSelection' in bridge
    assert 'ApproveEnvironmentMigration' in bridge
    assert 'EnvironmentMode' in bridge
    assert '输入 Y 同意更新应用和环境' in bridge
    assert 'minimum_temporary_space_bytes' in bridge
    assert 'Get-OwVoiceReleaseIdentity' not in bridge
    assert '[string]$Version = "v0.2.0"' in bridge
    assert 'Join-Path $PSScriptRoot "environment"' in bridge_build
    assert 'legacy-release-layout-v0.1.2.json' in bridge_build
    for identity_file in ('"version.json"', '"release-layout.json"', '"environment-spec.json"'):
        assert f'Copy-Item -LiteralPath (Join-Path $projectRoot {identity_file}) -Destination $packageDir' not in bridge_build
    assert '"__pycache__"' in bridge_build
    assert '".pyc"' in bridge_build


def test_update_transaction_links_environment_switch_to_application_health_check() -> None:
    transaction = (PROJECT_ROOT / "scripts" / "update_transaction.ps1").read_text(encoding="utf-8-sig")
    updater = (PROJECT_ROOT / "updater" / "update_release.ps1").read_text(encoding="utf-8-sig")
    assert "ApproveEnvironmentMigration" in transaction
    assert "New-OwVoiceCandidateEnvironment" in transaction
    assert "Test-OwVoiceCandidateEnvironment" in transaction
    assert "ENV_CANDIDATE_RUNTIME_VERIFY_FAILED" in (PROJECT_ROOT / "scripts" / "environment" / "OwVoice.Environment.psm1").read_text(encoding="utf-8-sig")
    assert "Switch-OwVoiceEnvironment" in transaction
    assert "Restore-OwVoiceEnvironment" in transaction
    assert "Complete-OwVoiceEnvironmentTransaction" in transaction
    assert '"health_verified"' in transaction
    assert 'Invoke-EnvironmentMigration $journal' in transaction
    assert 'Complete-EnvironmentForTransaction $journal' in transaction
    assert "ApproveEnvironmentMigration" in updater
    assert "legacy-release-layout.json" in updater
    assert "AllowLegacyTargetWithoutIdentity" in updater
    assert "legacy-release-layout.json" in transaction
    assert "Invoke-EnvironmentResourcePreparation" in transaction
    assert "-VerifyProjectResources" in transaction
    assert transaction.index("Invoke-EnvironmentResourcePreparation") < transaction.index("-VerifyProjectResources")
    assert 'Join-Path $targetDir ".venv\\Scripts\\python.exe"' in transaction
    assert '"-m", "uvicorn", "backend.server:app"' in transaction
    assert "更新验证期间不会显示主界面" in transaction
    assert "update-health-" in transaction
    assert "Get-UpdateHealthFailureSummary" in transaction
    assert transaction.index('Complete-EnvironmentForTransaction $journal') < transaction.index('Start-Process -FilePath ([System.IO.Path]::GetFullPath($RestartPath))')


def test_update_progress_is_human_readable_and_stage_based() -> None:
    transaction = (PROJECT_ROOT / "scripts" / "update_transaction.ps1").read_text(encoding="utf-8-sig")
    environment = (PROJECT_ROOT / "scripts" / "environment" / "OwVoice.Environment.psm1").read_text(encoding="utf-8-sig")

    assert "Format-OwVoiceMegabytes" in transaction
    assert "System.Net.Http.HttpClient" in transaction
    assert "Invoke-WebRequest -Uri $Url -OutFile $partial" not in transaction
    assert "下载更新包" in transaction
    assert "正在复制并复用旧环境" in environment
    assert "正在按锁文件同步依赖" in environment
    assert "已用时" in environment
    assert "Invoke-OwVoiceTrackedProcess" in transaction
    assert "2>&1 | Out-Host" not in transaction
    assert "Get-OwVoiceLastProcessFailureMessage" in transaction
    assert "下载进度：" not in transaction
    assert "更新包下载完成" in transaction


def test_tracked_native_process_does_not_treat_stderr_as_failure(tmp_path: Path) -> None:
    root = str(tmp_path).replace("'", "''")
    python = str(Path(sys.executable)).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module -Name '{str(MODULE_PATH).replace("'", "''")}' -Force
$code = Invoke-OwVoiceTrackedProcess -FilePath '{python}' -Arguments @('-c', 'import sys; print("normal progress", file=sys.stderr)') -Activity 'test' -LogRoot '{root}' -WorkingDirectory '{root}'
[ordered]@{{ exit_code=$code; failure=(Get-OwVoiceLastProcessFailureMessage) }} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(next(line for line in reversed(result.stdout.splitlines()) if line.strip().startswith("{")))
    assert payload == {"exit_code": 0, "failure": ""}


def test_tracked_native_process_preserves_real_failure_details(tmp_path: Path) -> None:
    root = str(tmp_path).replace("'", "''")
    python = str(Path(sys.executable)).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module -Name '{str(MODULE_PATH).replace("'", "''")}' -Force
$code = Invoke-OwVoiceTrackedProcess -FilePath '{python}' -Arguments @('-c', 'import sys; print("specific failure", file=sys.stderr); raise SystemExit(7)') -Activity 'test' -LogRoot '{root}' -WorkingDirectory '{root}'
[ordered]@{{ exit_code=$code; failure=(Get-OwVoiceLastProcessFailureMessage) }} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(next(line for line in reversed(result.stdout.splitlines()) if line.strip().startswith("{")))
    assert payload["exit_code"] == 7
    assert "specific failure" in payload["failure"]
    assert "子进程日志" in payload["failure"]


def test_update_transaction_snapshots_only_whitelisted_user_metadata() -> None:
    transaction = (PROJECT_ROOT / "scripts" / "update_transaction.ps1").read_text(encoding="utf-8-sig")

    for path in (
        "config/voices.local.json",
        "data/models/installed-models.json",
        "data/models",
        "data/training/jobs",
    ):
        assert path in transaction
    assert "Get-UserDataSnapshot" in transaction
    assert "Backup-CriticalMetadata" in transaction
    assert "Assert-UserDataSnapshot" in transaction
    assert "data/models/[^/]+/model" in transaction
    assert "data/training/jobs/[^/]+/job" in transaction


def test_update_health_is_required_before_commit() -> None:
    transaction = (PROJECT_ROOT / "scripts" / "update_transaction.ps1").read_text(encoding="utf-8-sig")

    assert "$Health.ready_for_update_commit -ne $true" in transaction
    assert "$Health.update_health.environment.fingerprint" in transaction
