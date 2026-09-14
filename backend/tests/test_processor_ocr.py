"""阶段12-T4：_load_or_run_ocr 管线接入测试（mock VLM，不真调 API）。

覆盖：VLM 主路线挂载与 stats/缓存记录、vlm.enabled=false 整体回退
旧文本层路径、最终产物缓存命中秒挂、扫描页占位后走视觉通道。
"""
import asyncio

import pymupdf

from cache.file_cache import ocr_key, read_cache, write_cache
from config import settings
from pipeline import processor


def _one_page_pdf(tmp_path) -> str:
    pdf = str(tmp_path / "p.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 60, 545, 200),
        "A digital body paragraph with enough text to pass the text layer check.",
    )
    doc.save(pdf)
    doc.close()
    return pdf


_CFG = {"api_key": "k", "api_url": "https://x/v1", "model": "vision-m"}


def _job() -> dict:
    return {"pages": [], "progress": 0, "stats": {"ocr_cache_hit": 0, "ocr_total": 0}}


def test_vlm_route_mounts_and_records(tmp_path, monkeypatch):
    pdf = _one_page_pdf(tmp_path)
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setattr(settings, "cache_dir", cache_dir)

    async def fake_verified(file_path, pno, pdf_hash, image_dir, config, cache_dir_, **kw):
        return {"md": "# Title\n\nBody.", "source": "vlm", "bag": 0.99, "trunc": 0}

    monkeypatch.setattr(processor.vlm_parse, "parse_page_verified", fake_verified)
    job = _job()
    pages = asyncio.run(processor._load_or_run_ocr(pdf, "h1", _CFG, job))
    assert pages[0]["blocks"][0]["original"] == "# Title\n\nBody."
    assert job["stats"]["vlm_pages"] == 1
    assert job["stats"]["vlm_fallback"] == 0
    # 最终产物缓存记录 source（重跑时 stats 可还原来源）
    cached = read_cache(cache_dir, ocr_key("h1", 0, processor.TEXT_LAYER_MODEL))
    assert cached["source"] == "vlm"
    # job_pages 与 pages 同源（前端轮询见同一对象）
    assert job["pages"][0]["blocks"][0]["original"] == "# Title\n\nBody."


def test_vlm_killswitch_falls_back_to_textlayer(tmp_path, monkeypatch):
    """ocr.vlm.enabled=false → 整体回退旧文本层路径（与 v18 行为一致）。"""
    pdf = _one_page_pdf(tmp_path)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    cfg = dict(_CFG, vlm={"enabled": False})

    async def must_not_run(*a, **kw):
        raise AssertionError("VLM 路线不应启用")

    monkeypatch.setattr(processor.vlm_parse, "parse_page_verified", must_not_run)
    monkeypatch.setattr(
        processor, "extract_pages", lambda *a, **kw: ["# From Textlayer\n\nBody."]
    )
    job = _job()
    pages = asyncio.run(processor._load_or_run_ocr(pdf, "h2", cfg, job))
    assert pages[0]["blocks"][0]["original"] == "# From Textlayer\n\nBody."
    assert job["stats"]["vlm_pages"] == 0


def test_no_api_key_disables_vlm(tmp_path, monkeypatch):
    """未配 OCR Key：视觉/VLM 都不可用，数字页走文本层（视觉页后续才报错）。"""
    pdf = _one_page_pdf(tmp_path)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))

    async def must_not_run(*a, **kw):
        raise AssertionError("VLM 路线不应启用")

    monkeypatch.setattr(processor.vlm_parse, "parse_page_verified", must_not_run)
    monkeypatch.setattr(
        processor, "extract_pages", lambda *a, **kw: ["Plain textlayer output."]
    )
    pages = asyncio.run(processor._load_or_run_ocr(pdf, "h3", {"api_key": ""}, _job()))
    assert pages[0]["blocks"][0]["original"] == "Plain textlayer output."


def test_final_cache_hit_skips_everything(tmp_path, monkeypatch):
    pdf = _one_page_pdf(tmp_path)
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    blocks = [{"block_id": 0, "page": 0, "original": "cached", "translated": ""}]
    write_cache(
        cache_dir,
        ocr_key("h4", 0, processor.TEXT_LAYER_MODEL),
        {"blocks": blocks, "source": "textlayer"},
    )

    async def must_not_run(*a, **kw):
        raise AssertionError("缓存命中不应触发提取")

    monkeypatch.setattr(processor.vlm_parse, "parse_page_verified", must_not_run)
    job = _job()
    pages = asyncio.run(processor._load_or_run_ocr(pdf, "h4", _CFG, job))
    assert pages[0]["blocks"][0]["original"] == "cached"
    assert job["stats"]["ocr_cache_hit"] == 1
    assert job["stats"]["vlm_fallback"] == 1  # 缓存记录来源可还原


def test_scanned_page_routes_to_vision(tmp_path, monkeypatch):
    """VLM 路线判定扫描页 → 占位保持页序，循环后统一视觉 OCR 替换。"""
    pdf = str(tmp_path / "blank.pdf")
    doc = pymupdf.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))

    async def fake_verified(*a, **kw):
        return {"scanned": True}

    monkeypatch.setattr(processor.vlm_parse, "parse_page_verified", fake_verified)

    async def fake_call_ocr(file_path, config, only_pages=None, page_image_dir=None):
        assert only_pages == [0]
        return [{"page": 0, "blocks": [{"block_id": 0, "page": 0,
                "original": "vision result", "translated": ""}]}]

    monkeypatch.setattr(processor, "call_ocr", fake_call_ocr)
    pages = asyncio.run(processor._load_or_run_ocr(pdf, "h5", _CFG, _job()))
    assert pages[0]["blocks"][0]["original"] == "vision result"
