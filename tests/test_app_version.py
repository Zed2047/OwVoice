import json

import pytest

from backend.app_version import ReleaseIdentityError, get_app_version, load_release_identity


def write_identity(tmp_path, **changes):
    payload = {"version": "0.2.0", "channel": "stable", "updateSchema": 2}
    payload.update(changes)
    (tmp_path / "version.json").write_text(json.dumps(payload), encoding="utf-8")


def test_release_identity_reads_canonical_file(tmp_path):
    write_identity(tmp_path)
    identity = load_release_identity(tmp_path)
    assert identity.version == "0.2.0"
    assert identity.tag == "v0.2.0"
    assert identity.channel == "stable"
    assert identity.update_schema == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "v0.2.0"},
        {"version": "0.2"},
        {"channel": "nightly"},
        {"updateSchema": 0},
        {"updateSchema": 2.5},
        {"updateSchema": True},
    ],
)
def test_release_identity_rejects_invalid_values(tmp_path, changes):
    write_identity(tmp_path, **changes)
    with pytest.raises(ReleaseIdentityError):
        load_release_identity(tmp_path)


def test_environment_cannot_silently_override_release_version(tmp_path):
    write_identity(tmp_path)
    assert get_app_version(tmp_path, {"OWVOICE_APP_VERSION": "9.9.9"}) == "0.2.0"


def test_explicit_test_override_is_supported(tmp_path):
    write_identity(tmp_path)
    environment = {
        "OWVOICE_APP_VERSION": "9.9.9",
        "OWVOICE_ALLOW_VERSION_OVERRIDE": "1",
    }
    assert get_app_version(tmp_path, environment) == "9.9.9"
