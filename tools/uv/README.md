# Bundled uv

OwVoice bundles the official Windows x64 `uv` 0.12.12 executable so that users can create the project-local Python environment without installing Python or modifying `PATH` first.

- Upstream: <https://github.com/astral-sh/uv/releases/tag/0.12.12>
- Archive: `uv-x86_64-pc-windows-msvc.zip`
- Archive SHA256: `3d54912924c36e862c14f427d04f2ed70a99e8001d1c30caa101f6d5711626d5`
- `uv.exe` SHA256: `efb9599543b26b3ea5adc1649bef69788633d9cc25c6cfd97b799e4dfa0c2cfb`
- Version output: `uv 0.12.12 (c4be69153 2026-09-09 x86_64-pc-windows-msvc)`
- License: MIT OR Apache-2.0; see the upstream project and `THIRD_PARTY_NOTICES.md`.

Verification performed on 2026-09-12:

1. The downloaded archive matched the SHA256 published with the upstream release.
2. The bundled executable matched the executable extracted from that archive.
3. Windows Authenticode verification returned `Valid`.

Do not replace `uv.exe` without updating these hashes, `scripts\setup_v2.ps1`, the third-party notice, and the release verification tests.
