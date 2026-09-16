"""open_cached_doc 缓存重建测试（不触网，纯缓存读写）。"""

import asyncio
import tempfile

from pipeline import processor
from pipeline.processor import TEXT_LAYER_MODEL, open_cached_doc
from cache.file_cache import ocr_key, translate_key, text_hash, write_cache
from translate.providers.openai_compat import PROMPT_VERSION


def _page_cache(page: int, blocks: list) -> dict:
    return {"blocks": blocks}


def _block(page: int, text: str, block_id: int = 0) -> dict:
    return {
        "block_id": block_id,
        "page": page,
        "original": text,
        "translated": "",
        "position": {"y_start": 0, "y_end": 0},
    }


def _setup_cache_dir(monkeypatch) -> str:
    d = tempfile.mkdtemp()
    monkeypatch.setattr(processor.settings, "cache_dir", d)
    monkeypatch.setattr(processor.settings, "ocr_config", {"model": "test-vision"})
    monkeypatch.setattr(
        processor.settings,
        "translate_config",
        {"target_language": "en", "model": "test-model"},
    )
    return d


def test_rebuild_fills_translations_from_cache(monkeypatch):
    d = _setup_cache_dir(monkeypatch)
    # 页 0：整页 markdown 单块（真实 OCR 缓存形态，_single_block 写入）
    write_cache(
        d,
        ocr_key("h" * 40, 0, TEXT_LAYER_MODEL),
        _page_cache(
            0,
            [
                _block(
                    0,
                    "# **DEMO: Knowledge Agent**\n\nGraph neural networks help retrieval.",
                )
            ],
        ),
    )
    # 正文块的译文缓存（与管线同一 key 规则）
    write_cache(
        d,
        translate_key(text_hash("Graph neural networks help retrieval."), "en", "test-model", PROMPT_VERSION),
        {"translated": "图神经网络有助于检索。"},
    )

    out = asyncio.run(open_cached_doc("h" * 40, 1, ""))
    assert out["file_exists"] is False
    assert out["doc_title"] == "**DEMO: Knowledge Agent**"
    blocks = out["pages"][0]["blocks"]
    # 页眉剔除后标题块仍在（带 # 前缀的是真标题，不被剔除）
    assert any("DEMO" in b["original"] for b in blocks)
    body = next(b for b in blocks if "Graph neural" in b["original"])
    assert body["translated"] == "图神经网络有助于检索。"


def test_rebuild_raises_when_cache_all_missing(monkeypatch):
    _setup_cache_dir(monkeypatch)
    try:
        asyncio.run(open_cached_doc("h" * 40, 3, ""))
        assert False, "应当抛 ValueError"
    except ValueError as e:
        assert "重新翻译" in str(e)


def test_rebuild_keeps_page_order_on_partial_cache(monkeypatch):
    d = _setup_cache_dir(monkeypatch)
    write_cache(
        d,
        ocr_key("h" * 40, 1, TEXT_LAYER_MODEL),
        _page_cache(1, [_block(1, "Only page 1 cached.", 0)]),
    )
    out = asyncio.run(open_cached_doc("h" * 40, 3, ""))
    assert [p["page"] for p in out["pages"]] == [0, 1, 2]
    assert out["pages"][0]["blocks"] == []
    assert out["pages"][1]["blocks"]


def test_extract_doc_title_skips_generic_headings():
    """「Abstract」等章节头被误判成顶级标题时，应跳过取下一个真实标题。"""
    from pipeline.processor import _extract_doc_title

    def mk(pages_text):
        return [{"page": 0, "blocks": [{"original": t} for t in pages_text]}]

    pages = mk(["# Abstract", "# DEMO: Dual Aligned Knowledge Graphs", "# 1 Introduction"])
    assert _extract_doc_title(pages) == "DEMO: Dual Aligned Knowledge Graphs"
    # 带编号/冒号的通用名也跳过
    assert _extract_doc_title(mk(["# 1 Introduction", "# Results: all good"])) == "Results: all good"
    # 全部是通用章节名 → 空串（索引回退文件名）
    assert _extract_doc_title(mk(["# Abstract", "# References"])) == ""


def test_extract_doc_title_skips_generic_headings():
    """「Abstract」等章节头被误判成顶级标题时，应跳过取下一个真实标题。"""
    from pipeline.processor import _extract_doc_title

    def mk(pages_text):
        return [{"page": 0, "blocks": [{"original": t} for t in pages_text]}]

    pages = mk(["# Abstract", "# DEMO: Dual Aligned Knowledge Graphs", "# 1 Introduction"])
    assert _extract_doc_title(pages) == "DEMO: Dual Aligned Knowledge Graphs"
    # 带编号/冒号的通用名也跳过
    assert _extract_doc_title(mk(["# 1 Introduction", "# Results: all good"])) == "Results: all good"
    # 全部是通用章节名 → 空串（索引回退文件名）
    assert _extract_doc_title(mk(["# Abstract", "# References"])) == ""


def test_extract_doc_title_plain_text_title_tog_style():
    """TOG 型：标题是无 # 的独立全大写行，唯一 # 标题是 ABSTRACT（通用名）。"""
    from pipeline.processor import _extract_doc_title

    pages = [{
        "page": 0,
        "blocks": [
            {"original": "THINK-ON-GRAPH: DEEP AND RESPONSIBLE REASONING OF LLM ON KG"},
            {"original": "# ABSTRACT"},
            {"original": "Although large language models have achieved success..."},
        ],
    }]
    assert _extract_doc_title(pages) == "THINK-ON-GRAPH: DEEP AND RESPONSIBLE REASONING OF LLM ON KG"


def test_extract_doc_title_rejects_numbered_section_heading():
    """编号章节头（2.1.2 X）不是标题：跳过后取首页首个正文块。"""
    from pipeline.processor import _extract_doc_title

    pages = [{
        "page": 0,
        "blocks": [
            {"original": "# 2.1.2 EXPLORATION"},
            {"original": "Think-on-Graph performs beam search on knowledge graphs."},
        ],
    }]
    assert _extract_doc_title(pages) == "Think-on-Graph performs beam search on knowledge graphs."
