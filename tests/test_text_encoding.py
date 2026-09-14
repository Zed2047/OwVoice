from __future__ import annotations

from backend.text_encoding import decode_text, read_text_compat, read_text_tail


def test_decode_text_accepts_utf8_with_and_without_bom() -> None:
    text = "中文路径与日志"
    assert decode_text(text.encode("utf-8")) == text
    assert decode_text(text.encode("utf-8-sig")) == text


def test_decode_text_accepts_legacy_simplified_chinese_code_page() -> None:
    text = "模型导入失败"
    assert decode_text(text.encode("gb18030")) == text


def test_read_text_compat_and_tail_use_same_decoder(tmp_path) -> None:
    path = tmp_path / "用户配置.json"
    path.write_bytes("第一行\n第二行\n".encode("gb18030"))

    assert read_text_compat(path) == "第一行\n第二行\n"
    assert read_text_tail(path, limit=3) == "二行"
