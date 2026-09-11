from frontend.app import _sync_voice_prompt_defaults


def test_reimported_model_invalidates_only_its_stale_prompt_cache():
    loaded_defaults = {
        "juno": "下次我一定能做到！……但愿吧。",
        "dva": "D.Va正在侦察这里！",
    }
    prompt_cache = {
        "juno": "下次我一定能做到！……但愿吧。",
        "dva": "玩家为D.Va输入的自定义文案",
    }

    # 删除朱诺时保留上次模型默认值，以便同 ID 模型重新导入后进行比较。
    assert _sync_voice_prompt_defaults(
        loaded_defaults,
        prompt_cache,
        [{"id": "dva", "prompt_text": "D.Va正在侦察这里！"}],
    ) == set()

    changed = _sync_voice_prompt_defaults(
        loaded_defaults,
        prompt_cache,
        [
            {
                "id": "juno",
                "prompt_texts": ["好了朱诺，你完全有能力应付这一切。"],
                "prompt_text": "备用文案",
            },
            {"id": "dva", "prompt_text": "D.Va正在侦察这里！"},
        ],
    )

    assert changed == {"juno"}
    assert "juno" not in prompt_cache
    assert prompt_cache["dva"] == "玩家为D.Va输入的自定义文案"
    assert loaded_defaults["juno"] == "好了朱诺，你完全有能力应付这一切。"
