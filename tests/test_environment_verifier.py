import copy

from scripts.environment.verify_environment import environment_fingerprint


def test_environment_fingerprint_includes_mode_and_training_flag() -> None:
    spec = {
        "python": {"version": "3.10.10", "version_range": ">=3.10,<3.11"},
        "dependencies": {"pyproject_sha256": "a" * 64, "lock_sha256": "b" * 64},
    }
    cpu = environment_fingerprint(spec, "CPU", False)
    gpu = environment_fingerprint(spec, "GPU", False)
    training = environment_fingerprint(spec, "CPU", True)
    assert len(cpu) == 64
    assert len({cpu, gpu, training}) == 3
    assert environment_fingerprint(copy.deepcopy(spec), "cpu", False) == cpu
