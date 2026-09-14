import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from translate.base import translate_batch, translate_text
from translate.providers import get_provider, list_providers
from translate.providers.deepl import DeepLProvider
from translate.providers.google import GoogleProvider


def _fake_response(json_data, status_code=200):
    class _R:
        def raise_for_status(self):
            return None

        def json(self):
            return json_data

    r = _R()
    r.status_code = status_code
    r.text = ""
    return r


def test_empty_text_returns_empty():
    out = asyncio.run(translate_text("", "en", "zh", {"provider": "siliconflow"}))
    assert out == ""


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        get_provider("does-not-exist")


def test_unimplemented_providers_raise():
    with pytest.raises(NotImplementedError):
        asyncio.run(GoogleProvider().translate("x", "en", "zh", {}))
    with pytest.raises(NotImplementedError):
        asyncio.run(DeepLProvider().translate("x", "en", "zh", {}))


def test_list_providers_flags():
    flags = list_providers()
    assert flags["siliconflow"] is True
    assert flags["openai"] is True
    assert flags["google"] is False
    assert flags["deepl"] is False


def test_openai_compat_call_mocked():
    cfg = {
        "provider": "siliconflow",
        "api_url": "https://example/v1",
        "api_key": "test-key",
        "model": "test-model",
    }
    fake = {"choices": [{"message": {"content": "你好世界"}}]}
    with patch(
        "translate.providers.openai_compat.httpx.AsyncClient.post",
        new=AsyncMock(return_value=_fake_response(fake)),
    ):
        out = asyncio.run(translate_text("hello world", "en", "zh", cfg))
    assert out == "你好世界"


def test_openai_compat_missing_key_raises():
    """无 Key 显式报错（2026-09-09）：不再静默返回空译文。"""
    cfg = {"provider": "siliconflow", "api_key": "", "api_url": "x", "model": "m"}
    with pytest.raises(ValueError, match="翻译 API Key 未配置"):
        asyncio.run(translate_text("hello", "en", "zh", cfg))


def test_batch_short_circuits_empty():
    cfg = {"provider": "siliconflow", "api_key": "", "api_url": "x", "model": "m"}
    out = asyncio.run(translate_batch(["", "  "], "en", "zh", cfg))
    assert out == ["", ""]


# ── JSON 数组结构化 I/O 协议（阶段12-T6：替代 <<<n>>> 分隔标记批量）─────

import json  # noqa: E402

from translate.providers.openai_compat import (  # noqa: E402
    PROMPT_VERSION,
    OpenAICompatProvider,
    _parse_json_array,
    _system_prompt,
)


def test_system_prompt_contains_json_protocol():
    """批量协议改为 JSON 数组结构化 I/O，旧 <<<n>>> 标记协议退役。"""
    prompt = _system_prompt("en", "zh", {"section": "3.2 Attention"})
    assert "JSON 数组" in prompt
    assert '"translation"' in prompt
    assert "当前小节：3.2 Attention" in prompt
    assert "<<<" not in prompt


def test_system_prompt_section_optional():
    assert "当前小节：" not in _system_prompt("en", "zh", {})


def test_prompt_version_bumped_for_json_protocol():
    assert PROMPT_VERSION == "pv6"


def test_parse_json_array_plain():
    out = json.dumps(
        [{"id": 0, "translation": "零"}, {"id": 1, "translation": "一"}],
        ensure_ascii=False,
    )
    assert _parse_json_array(out) == {0: "零", 1: "一"}


def test_parse_json_array_tolerates_fence_and_prose():
    out = '好的：\n```json\n[{"id": 0, "translation": "零"}]\n```'
    assert _parse_json_array(out) == {0: "零"}


def test_parse_json_array_lenient_key():
    # 模型偶发沿用输入键名 "text"
    assert _parse_json_array('[{"id": 0, "text": "零"}]') == {0: "零"}


def test_parse_json_array_bad_raises():
    import pytest

    with pytest.raises(ValueError):
        _parse_json_array("这不是 JSON")
    with pytest.raises(ValueError):
        _parse_json_array('{"id": 0}')  # 不是数组
    with pytest.raises(ValueError):
        _parse_json_array("[]")  # 空数组无有效条目


def test_batch_missing_segment_retried_individually():
    """JSON 缺段 → 仅缺失段逐段重发（好段直接采用，不再减半）。"""
    p = OpenAICompatProvider()
    calls: list[str] = []

    async def fake_translate(text, s, t, cfg):
        calls.append(text)
        if '"id": 0' in text:  # 批次 JSON 载荷；单段重发是裸原文
            return json.dumps([{"id": 0, "translation": "零"}], ensure_ascii=False)
        return "一"

    with patch.object(p, "translate", new=AsyncMock(side_effect=fake_translate)):
        outs = asyncio.run(p.translate_batch(["a", "b"], "en", "zh", {"k": 1}))
    assert outs == ["零", "一"]
    assert calls[1] == "b"  # 重发的是裸原文单段，不是再打包


def test_batch_fused_segment_retried_individually():
    """段融合（单段译文多段落膨胀）→ 仅融合段重发。"""
    p = OpenAICompatProvider()

    async def fake_translate(text, s, t, cfg):
        if '"id": 0' in text:  # 批次 JSON 载荷
            return json.dumps(
                [
                    {"id": 0, "translation": "零"},
                    {"id": 1, "translation": "甲\n\n乙\n\n丙"},  # 1→3 段膨胀
                ],
                ensure_ascii=False,
            )
        return "一（重发）"

    with patch.object(p, "translate", new=AsyncMock(side_effect=fake_translate)):
        outs = asyncio.run(p.translate_batch(["a", "b"], "en", "zh", {"k": 1}))
    assert outs == ["零", "一（重发）"]


def test_batch_glossary_filtered_to_hits():
    """术语表按本批命中过滤：只注入出现的条目（阶段12-T6）。"""
    p = OpenAICompatProvider()
    prompts: list[str] = []

    async def fake_post(*args, **kw):
        prompts.append(kw["json"]["messages"][0]["content"])
        return _fake_response({"choices": [{"message": {"content": "译"}}]})

    cfg = {
        "provider": "siliconflow",
        "api_url": "https://x/v1",
        "api_key": "k",
        "model": "m",
        "glossary": {"Foo Bar": "甲术语", "Baz": "乙术语"},
    }
    with patch(
        "translate.providers.openai_compat.httpx.AsyncClient.post",
        new=AsyncMock(side_effect=fake_post),
    ):
        asyncio.run(p.translate_batch(["text mentioning Foo Bar only"], "en", "zh", cfg))
    assert "- Foo Bar → 甲术语" in prompts[0]
    assert "Baz" not in prompts[0]
    # 原配置不被污染（后续批次仍可命中 Baz）
    assert cfg["glossary"] == {"Foo Bar": "甲术语", "Baz": "乙术语"}
