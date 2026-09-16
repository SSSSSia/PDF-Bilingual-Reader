"""图表区域快照回归测试。

依赖仓库内测试样张 test_ocr/GraphRAG-Bench.pdf（LaTeX 导出、矢量图表 +
页码页眉），验证：
1. 矢量图表（matplotlib 导出，轴标签为真实文本）能被检测并截图落盘；
2. 快照引用按「图在上、caption 在下」锚定插回 markdown；
3. 表格区域（find_tables 命中）不被误快照（文本层已可完整提取）；
4. 页码噪音块（独立纯数字行）被 split_into_blocks 过滤。
"""

import os
import re
import tempfile

import pytest

from ocr.siliconflow import split_into_blocks
from ocr.textlayer import extract_pages

_PDF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "test_ocr",
    "GraphRAG-Bench.pdf",
)

pytestmark = pytest.mark.skipif(not os.path.isfile(_PDF), reason="缺测试样张 PDF")


_IMG_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def test_figure_snapshots_extracted(tmp_path):
    """page9（1-based）有两个矢量图（Figure 2/3），应快照出 2 个 PNG 引用。"""
    pages = extract_pages(_PDF, page_nums=[8], image_dir=str(tmp_path))
    assert pages[0], "page9 应有有效文本层"
    refs = _IMG_REF.findall(pages[0])
    assert len(refs) == 2, f"期望 2 个图引用，实际 {len(refs)}"
    for p in refs:
        assert os.path.isfile(p), f"快照文件不存在: {p}"
        assert os.path.getsize(p) > 10_000, f"快照疑似空图: {p}"


def test_figure_inserted_above_caption():
    """快照应插在对应 Figure caption 之前（图在上、注在下）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pages = extract_pages(_PDF, page_nums=[8], image_dir=d)
    md = pages[0] or ""
    m_img = md.find("![")
    m_cap = md.find("Figure 2:")
    assert 0 <= m_img < m_cap, "图片引用应位于 Figure 2 caption 之前"


def test_table_page_snapshotted_as_figure():
    """表格也按快照处理：
    page7（1-based）的两个表格应各产生一个快照（该样张表格无表注，
    快照按页尾追加兜底）。断言须在临时目录存活期内完成。"""
    with tempfile.TemporaryDirectory() as d:
        pages = extract_pages(_PDF, page_nums=[6], image_dir=d)
        md = pages[0] or ""
        refs = _IMG_REF.findall(md)
        assert len(refs) >= 2, f"两个表格应产生至少 2 个快照引用，实际 {len(refs)}"
        for p in refs:
            assert os.path.isfile(p), f"快照文件不存在: {p}"


def test_page_number_noise_filtered():
    """独立页码行（纯数字 / Page x of y）不应成为翻译块。"""
    blocks = split_into_blocks("Introduction\n2\nPage 3 of 12\nMethods")
    joined = "\n".join(blocks)
    assert "Introduction" in joined and "Methods" in joined
    assert not any(b.strip() in {"2", "Page 3 of 12"} for b in blocks)


# ── v2 几何配对 + 重排守卫修复──────────────


def test_insert_figures_geo_pairs_by_proximity():
    """快照检测序与 caption markdown 序不一致时按坐标就近配对。
    ：快照序 tab→fig→tab，caption 序 Table3→Table4→Figure3，
    旧序号配对把 Figure 3 的图配到了 Table 4 caption 旁。"""
    import pymupdf

    from ocr.textlayer import _insert_figures

    raw_blocks = [
        (70, 60, 290, 74, "Table 3: results with and without retrieval."),
        (70, 187, 291, 401, "less pronounced. Furthermore, we observe more."),
        (306, 316, 535, 345, "Figure 3: The size of the knowledge graph."),
        (70, 430, 251, 443, "Table 4: results with generative construction."),
    ]
    # 快照区域 y 序：表3(80-120) → 图3(100-310，右栏) → 表4(445-507)
    regions = [
        pymupdf.Rect(73, 80, 286, 120),
        pymupdf.Rect(303, 100, 536, 310),
        pymupdf.Rect(108, 445, 251, 507),
    ]
    refs = ["![Figure](a.png)", "![Figure](b.png)", "![Figure](c.png)"]
    md = (
        "Table 3: results with and without retrieval.\n\n"
        "less pronounced. Furthermore, we observe more.\n\n"
        "Figure 3: The size of the knowledge graph.\n\n"
        "Table 4: results with generative construction."
    )
    out = _insert_figures(md, refs, snap_regions=regions, raw_blocks=raw_blocks)
    paras = [p.strip() for p in re.split(r"\n\s*\n", out) if p.strip()]
    i_t3 = next(i for i, p in enumerate(paras) if p.startswith("Table 3"))
    i_f3 = next(i for i, p in enumerate(paras) if p.startswith("Figure 3"))
    i_t4 = next(i for i, p in enumerate(paras) if p.startswith("Table 4"))
    # 每个快照紧跟自己的 caption（插入在 caption 段之前）
    assert paras[i_t3 - 1] == refs[0], f"Table 3 快照错位: {paras[i_t3 - 1]!r}"
    assert paras[i_f3 - 1] == refs[1], f"Figure 3 快照错位: {paras[i_f3 - 1]!r}"
    assert paras[i_t4 - 1] == refs[2], f"Table 4 快照错位: {paras[i_t4 - 1]!r}"


def test_column_reorder_applies_with_image_paras():
    """有快照图片段的页也必须列重排。旧守卫因图片段匹配不到坐标而整页放弃
    （死代码 glue），换栏断词的续文（右栏顶）被排在其段头（左栏底）之前，
    向前合并永远够不着。"""
    from ocr.textlayer import _column_reading_order

    raw_blocks = [
        (70, 60, 290, 74, "Table 3: results with and without retrieval."),
        (70, 187, 291, 401, "less pronounced. Furthermore, we observe more."),
        (70, 562, 291, 775, "This trade-off between coverage and accuracy un-"),
        (306, 74, 524, 98, "derscores the critical importance of denoising."),
        (306, 500, 535, 530, "Figure 3: The size of the knowledge graph."),
        (306, 540, 535, 700, "To comprehensively understand how the performance evolves."),
    ]
    # y 带交错序：derscores(74) 在最前，段头 un-(562) 反而在后
    md = (
        "derscores the critical importance of denoising.\n\n"
        "Table 3: results with and without retrieval.\n\n"
        "less pronounced. Furthermore, we observe more.\n\n"
        "This trade-off between coverage and accuracy un-\n\n"
        "![Figure](D:/x/fig_p007_01.png)\n\n"
        "Figure 3: The size of the knowledge graph.\n\n"
        "To comprehensively understand how the performance evolves."
    )
    out = _column_reading_order(raw_blocks, md)
    paras = [p.strip() for p in re.split(r"\n\s*\n", out) if p.strip()]
    i_head = next(i for i, p in enumerate(paras) if p.endswith("accuracy un-"))
    # 重排后段头（左栏底）紧跟换栏续文（右栏顶）→ 前向合并可拼接
    assert paras[i_head + 1].startswith("derscores"), (
        f"换栏续文应紧跟段头，实际: {paras[i_head + 1][:40]!r}"
    )
    # 图片段仍紧贴其 caption
    i_fig = next(i for i, p in enumerate(paras) if p.startswith("Figure 3"))
    assert paras[i_fig - 1].startswith("![Figure]"), "图片段应紧贴其 caption"
