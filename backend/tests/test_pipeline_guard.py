"""run_pipeline 入口 fail-fast 守卫。

背景：管线各环节的 per-block 容错（_translate_chunk 减半重试、
figtranslate 保留原文）会把异常吞成空译文——无 Key 时若不拦截，
表现为"翻译完成却没有译文"的静默错误。run_pipeline 必须在启动
前校验 OCR/翻译 Key 任一缺失即拒绝。
"""

import asyncio

import pytest

from config import settings
from pipeline.processor import run_pipeline


def _fake_pdf(tmp_path):
    pdf = tmp_path / "guard.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake for guard test")
    return str(pdf)


def test_run_pipeline_missing_ocr_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.ocr_config, "api_key", "")
    monkeypatch.setitem(settings.translate_config, "api_key", "k")
    with pytest.raises(ValueError, match="OCR API Key 未配置"):
        asyncio.run(run_pipeline(_fake_pdf(tmp_path)))


def test_run_pipeline_missing_translate_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.ocr_config, "api_key", "k")
    monkeypatch.setitem(settings.translate_config, "api_key", "")
    with pytest.raises(ValueError, match="翻译 API Key 未配置"):
        asyncio.run(run_pipeline(_fake_pdf(tmp_path)))


def test_run_pipeline_missing_ocr_key_whitespace_rejected(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.ocr_config, "api_key", "   ")
    monkeypatch.setitem(settings.translate_config, "api_key", "k")
    with pytest.raises(ValueError, match="OCR API Key 未配置"):
        asyncio.run(run_pipeline(_fake_pdf(tmp_path)))
