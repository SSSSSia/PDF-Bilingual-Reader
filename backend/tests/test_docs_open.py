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


# ---------------- /api/docs/open 缺页数记录自愈（v0.14.0/14.1 装机回归） ----------------


def test_api_open_doc_heals_record_missing_page_count(tmp_path, monkeypatch):
    """完成钩子曾因作用域缺 display_name 静默失败，索引停留启动半条
    （无 page_count）→ 重开 404 误报「缓存已失效」。open 端应从库副本
    读真实页数重建并回写修复记录。"""
    import os

    import pymupdf
    from fastapi.testclient import TestClient

    import docs_index
    from main import app, settings

    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: str(tmp_path)))
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))

    # 库内副本：2 页真实 PDF
    lib = tmp_path / "files"
    lib.mkdir()
    pdf_path = lib / "9e642d9cecabedb9.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page()
    doc.save(str(pdf_path))
    doc.close()

    # 两页提取缓存都在（翻译本身是成功的，只是索引记录坏了）
    write_cache(
        str(tmp_path / "cache"),
        ocr_key("9" * 40, 0, TEXT_LAYER_MODEL),
        _page_cache(0, [_block(0, "# **DEMO Selfheal**\n\nBody text one.", 0)]),
    )
    write_cache(
        str(tmp_path / "cache"),
        ocr_key("9" * 40, 1, TEXT_LAYER_MODEL),
        _page_cache(1, [_block(1, "Body text two.", 0)]),
    )

    # 启动半条记录：无 page_count、status 还是 translating
    docs_index.upsert_doc(
        str(tmp_path),
        {
            "doc_id": "9e642d9cecabedb9",
            "title": "DEMO Selfheal",
            "file_path": str(pdf_path),
            "pdf_hash": "9" * 40,
            "status": "translating",
        },
    )

    client = TestClient(app)
    r = client.post("/api/docs/open", json={"doc_id": "9e642d9cecabedb9"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["pages"]) == 2
    assert any("DEMO Selfheal" in b["original"] for b in body["pages"][0]["blocks"])

    # 记录已回写修复
    healed = docs_index.get_doc(str(tmp_path), "9e642d9cecabedb9")
    assert healed["page_count"] == 2
    assert healed["status"] == "done"


def test_done_hook_passes_display_name():
    """静态回归：完成钩子所在 _process_pipeline 必须接收 display_name，
    且 run_pipeline 的 create_task 调用必须传满 4 参——
    v0.14.0/14.1 因作用域缺参致索引写入静默失败（NameError 被吞）。"""
    import ast
    import pathlib

    from pipeline import processor

    src = pathlib.Path(processor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "_process_pipeline"
    )
    assert "display_name" in {a.arg for a in fn.args.args}
    call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_process_pipeline"
    )
    assert len(call.args) >= 4
