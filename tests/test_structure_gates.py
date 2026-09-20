from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("final_bindings.json")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _expected_assignments(manifest: dict) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for module_name, module in manifest["modules"].items():
        for class_name, class_info in module["classes"].items():
            for method_name, implementation in class_info["bindings"].items():
                result[(module["source"], class_name, method_name)] = implementation
    return result


def _actual_assignments(manifest: dict) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for module in manifest["modules"].values():
        source = module["source"]
        tree = ast.parse((PROJECT_ROOT / source).read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
                    continue
                result[(source, target.value.id, target.attr)] = node.value.id
    return result


def test_final_runtime_bindings_match_manifest() -> None:
    manifest = _manifest()
    expected = _expected_assignments(manifest)
    actual = _actual_assignments(manifest)
    assert actual == expected, f"运行时覆盖与清单不一致：actual={actual}, expected={expected}"


def test_imported_classes_use_manifested_final_methods() -> None:
    manifest = _manifest()
    for module_name, module_info in manifest["modules"].items():
        module = importlib.import_module(module_name)
        for class_name, class_info in module_info["classes"].items():
            cls = getattr(module, class_name)
            for method_name, implementation in class_info["bindings"].items():
                final_method = getattr(cls, method_name)
                assert final_method.__name__ == implementation
                assert final_method.__module__ == module_name
            for saved_name in class_info["saved_originals"]:
                assert callable(getattr(module, saved_name)), f"缺少旧实现保存别名：{module_name}.{saved_name}"


def test_build_exe_declares_new_runtime_modules_and_release_copies_them() -> None:
    build_exe = (PROJECT_ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8-sig")
    for module_name in ("backend.process_lifecycle", "frontend.update_ui"):
        assert f'"{module_name}"' in build_exe

    build_release = (PROJECT_ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8-sig")
    assert 'foreach ($directory in @("backend", "frontend"))' in build_release

    layout = json.loads((PROJECT_ROOT / "release-layout.json").read_text(encoding="utf-8"))
    managed_items = set(layout["managedItems"])
    for module_path in ("backend/process_lifecycle.py", "frontend/update_ui.py"):
        module_root = module_path.split("/", 1)[0]
        assert module_root in managed_items
        assert (PROJECT_ROOT / module_path).is_file()
