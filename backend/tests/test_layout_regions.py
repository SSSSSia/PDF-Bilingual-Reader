"""阶段12-T9.2：版面模型区域消费单测（纯函数 + _prepare/_finalize 集成）。

区域分类、标题编号定级、abandon 三通道匹配（全等/首窗口前缀/截断片段）、
title 提升的幂等与长度约束；_prepare 的 truth 扣除与无框表快照补位、
_finalize_md 的剔除+提升链。合成 PDF + 手工区域，不依赖版面模型。
"""
import asyncio

import pymupdf

from ocr import vlm_parse
from ocr.textlayer import _md_norm


# ── 区域分类 ─────────────────────────────────────────────────────

def test_split_layout_regions_classifies_and_drops_malformed():
    regions = [
        {"label": "table", "conf": 0.9, "bbox": [10, 10, 200, 100]},
        {"label": "figure", "conf": 0.8, "bbox": [10, 120, 200, 200]},
        {"label": "abandon", "conf": 0.85, "bbox": [0, 700, 600, 780]},
        {"label": "title", "conf": 0.92, "bbox": [40, 40, 500, 90]},
        {"label": "plain text", "conf": 0.99, "bbox": [0, 0, 100, 100]},  # 非目标类
        {"label": "title", "conf": 0.5, "bbox": [1, 1]},                  # 畸形 bbox
        {"label": "abandon", "conf": 0.7, "bbox": None},                  # 畸形 bbox
        {"label": "figure", "conf": 0.7, "bbox": [5, 5, 5, 5]},           # 空矩形
    ]
    lr = vlm_parse._split_layout_regions(regions)
    assert len(lr["fig"]) == 2          # table + figure
    assert len(lr["table"]) == 1        # 仅 table
    assert len(lr["abandon"]) == 1
    assert len(lr["title"]) == 1
    assert vlm_parse._split_layout_regions(None) == {"fig": [], "table": [], "abandon": [], "title": []}


# ── 标题定级 ─────────────────────────────────────────────────────

def test_title_level_numbered_depth_and_doc_title():
    assert vlm_parse._title_level("4 Experimental Settings", is_doc_title=True) == 1
    assert vlm_parse._title_level("4 Experimental Settings", is_doc_title=False) == 2
    assert vlm_parse._title_level("3.4 Ablation", is_doc_title=False) == 3
    assert vlm_parse._title_level("3.4.1 Deep dive", is_doc_title=False) == 4
    assert vlm_parse._title_level("3.4.1.2 too deep", is_doc_title=False) == 4  # 封顶
    assert vlm_parse._title_level("Abstract", is_doc_title=False) == 2
    assert vlm_parse._title_level("C. GraphRAG", is_doc_title=False) == 2       # 字母编号→无编号档


# ── abandon 段落剔除 ─────────────────────────────────────────────

def test_drop_abandon_long_region_prefix():
    md = "First body paragraph stays here.\n\nACM Reference Format: Zhang, Qinggang et al. 2024.\n\nSecond body stays."
    a = _md_norm("ACM Reference Format: Zhang, Qinggang et al. 2024.")
    out = vlm_parse._drop_abandon_paragraphs(md, [a])
    assert "First body paragraph stays here." in out
    assert "ACM Reference Format" not in out
    assert "Second body stays." in out


def test_drop_abandon_short_region_requires_equality():
    md = "2\n\n2024 was a good year for research in this area."
    a = _md_norm("2")
    out = vlm_parse._drop_abandon_paragraphs(md, [a])
    # 短区域只全等杀页码段；"2024 ..." 不被误杀
    assert "2024 was a good year" in out
    assert not any(p.strip() == "2" for p in out.split("\n\n"))


def test_drop_abandon_truncated_fragment_inside_region():
    # VLM 把一块版权拆成多段输出：第二段是区域文本的截断片段（in 匹配）
    region = _md_norm("Permission to make digital or hard copies of all or part of this work for personal use.")
    md = "Body text.\n\nhard copies of all or part of this work"
    out = vlm_parse._drop_abandon_paragraphs(md, [region])
    assert "Body text." in out
    assert "hard copies" not in out


def test_drop_abandon_idempotent_and_empty():
    md = "A\n\nB\n\nC"
    a = _md_norm("B")
    once = vlm_parse._drop_abandon_paragraphs(md, [a])
    assert vlm_parse._drop_abandon_paragraphs(once, [a]) == once
    assert vlm_parse._drop_abandon_paragraphs(md, []) == md


# ── title 提升 ───────────────────────────────────────────────────

def test_promote_titles_levels_and_idempotent():
    md = "1 Introduction\n\nSome body paragraph that is long enough to look like prose.\n\n3.4 Ablation Study"
    titles = [
        (_md_norm("1 Introduction"), 2),
        (_md_norm("3.4 Ablation Study"), 3),
    ]
    out = vlm_parse._promote_titles(md, titles)
    assert "## 1 Introduction" in out
    assert "### 3.4 Ablation Study" in out
    assert "Some body paragraph" in out
    # 幂等：已是标题的段落不再动
    assert vlm_parse._promote_titles(out, titles) == out


def test_promote_titles_length_guard_blocks_body_paragraph():
    # 正文段以标题词开头但远长于标题：不提升（apply_font_evidence 同款纪律）
    tnorm = _md_norm("1 Introduction")
    md = "1 Introduction is the section where authors usually describe the motivation " \
         "and contributions of the paper in great detail for readers."
    out = vlm_parse._promote_titles(md, [(tnorm, 2)])
    assert out == md


# ── _prepare / _finalize_md 集成（合成 PDF + 手工区域）───────────

def _make_layout_pdf(path) -> None:
    """页 0：标题 + 正文 + 无框表文本 + 版权段（坐标供区域对位）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 45, 520, 85),
        "1 Introduction to the Synthetic Corpus",
    )
    page.insert_textbox(
        pymupdf.Rect(50, 110, 545, 200),
        "Body paragraph alpha with several lines of ordinary prose that must "
        "survive every filtering stage in the preparation pipeline.",
    )
    # 无框表：三行“表格”文本（无绘图线，find_tables 检不出）
    for k, row in enumerate(["alpha 0.31 beta 0.44", "gamma 0.52 delta 0.61", "epsilon 0.70 zeta 0.88"]):
        page.insert_textbox(pymupdf.Rect(60, 300 + k * 30, 360, 322 + k * 30), row)
    page.insert_textbox(
        pymupdf.Rect(45, 700, 570, 770),
        "Permission to make digital or hard copies of this work for personal use is granted without fee.",
    )
    doc.save(path)
    doc.close()


_LAYOUT = [
    {"label": "title", "conf": 0.92, "bbox": [45, 40, 525, 90]},
    {"label": "table", "conf": 0.95, "bbox": [55, 292, 365, 395]},
    {"label": "abandon", "conf": 0.88, "bbox": [40, 695, 575, 775]},
]


def test_prepare_consumes_layout_regions(tmp_path):
    pdf = str(tmp_path / "l.pdf")
    _make_layout_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "img"), _LAYOUT)
    # truth 扣除：表格区域与版权段不进校验基准
    assert _md_norm("epsilon 0.70 zeta 0.88") not in _md_norm(prep["truth"])
    assert "Permission to make digital" not in prep["truth"]
    # 正文与标题保留
    assert "Body paragraph alpha" in prep["truth"]
    assert "1 Introduction to the Synthetic Corpus" in prep["truth"]
    # abandon 区域文本与标题候选已采集
    assert any("permissiontomakedigital" in a for a in prep["abandon_norms"])
    assert prep["layout_titles"] and prep["layout_titles"][0][1] == 1  # 页 0 最大 title → #
    # 无框表进了快照管线：refs 一张、命名 tab_*（table_regions 分类）
    assert len(prep["refs"]) == 1
    assert "tab_" in prep["refs"][0]


def test_finalize_md_drops_abandon_and_promotes_title(tmp_path):
    pdf = str(tmp_path / "l.pdf")
    _make_layout_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "img"), _LAYOUT)
    md = (
        "1 Introduction to the Synthetic Corpus\n\n"
        "Body paragraph alpha with several lines of ordinary prose.\n\n"
        "Permission to make digital or hard copies of this work without fee provided that copies are not made or distributed for profit.\n"
    )
    out = vlm_parse._finalize_md(prep, md)
    assert "# 1 Introduction" in out          # title 提升（文档标题级）
    assert "Permission to make digital" not in out  # abandon 剔除
    assert "Body paragraph alpha" in out
    # 快照引用插回（无框表以图片呈现）
    assert "![Figure](" in out


def test_finalize_md_without_layout_keeps_behavior(tmp_path):
    """无版面信号（T9.4 兜底激活）：字号证据登场——本合成 PDF 全文同字号
    同字重，证据为空 → 输出不变（兜底无害性的下界）。"""
    pdf = str(tmp_path / "l.pdf")
    _make_layout_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "img"), None)
    assert prep["layout_ok"] is False
    assert prep["font_evidence"] is not None  # 兜底证据已采集
    md = "1 Introduction to the Synthetic Corpus\n\nBody paragraph alpha.\n\nPermission junk line."
    out = vlm_parse._finalize_md(prep, md)
    assert "Permission junk line" in out       # 无字号差异 → 无小字印刷判定
    assert not out.lstrip().startswith("#")    # 无字号差异 → 无标题提升


def _make_font_pdf(path) -> None:
    """页 0：16pt 粗体标题 + 11pt 常规正文（字号证据可判）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 45, 520, 90),
        "Bold Big Title of the Font Evidence Page",
        fontsize=16,
        fontname="hebo",
    )
    page.insert_textbox(
        pymupdf.Rect(50, 120, 545, 220),
        "Regular body text at the default size for the font evidence corpus "
        "with enough words to form a real paragraph.",
    )
    doc.save(path)
    doc.close()


def test_font_evidence_fallback_promotes_title(tmp_path):
    """T9.4：版面信号缺席 → 字号证据把粗体大字标题提升为 #。"""
    pdf = str(tmp_path / "f.pdf")
    _make_font_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "img"), None)
    md = (
        "Bold Big Title of the Font Evidence Page\n\n"
        "Regular body text at the default size for the font evidence corpus "
        "with enough words to form a real paragraph."
    )
    out = vlm_parse._finalize_md(prep, md)
    assert "# Bold Big Title" in out
    assert "Regular body text" in out


def test_layout_ok_suppresses_font_fallback(tmp_path):
    """T9.4 关键语义：版面模型成功出区域（即使本页无 title）→ 字号证据
    不越权——「本页无标题」是版面模型的可信判定。"""
    pdf = str(tmp_path / "f.pdf")
    _make_font_pdf(pdf)
    # 只给一个 abandon 区域：layout_ok=True 但无 title
    regions = [{"label": "abandon", "conf": 0.9, "bbox": [40, 700, 575, 780]}]
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "img"), regions)
    assert prep["layout_ok"] is True
    assert prep["font_evidence"] is None  # 采集都被跳过
    md = "Bold Big Title of the Font Evidence Page\n\nRegular body text goes here."
    out = vlm_parse._finalize_md(prep, md)
    assert not out.lstrip().startswith("#")  # 字号兜底未介入


# ── parse_page_verified：遮罩含 abandon 区域 ─────────────────────

class _FakeLayout:
    """regions() 返回固定区域（模拟 provider 页级产出）。"""

    def __init__(self, regions):
        self._regions = regions

    async def regions(self, page):
        return self._regions


def test_parse_page_verified_mask_includes_abandon(tmp_path, monkeypatch):
    pdf = str(tmp_path / "l.pdf")
    _make_layout_pdf(pdf)
    captured = {}

    async def fake_parse_page(file_path, pno, config, mask_regions=None):
        captured["mask"] = list(mask_regions or [])
        return {
            "md": (
                "1 Introduction to the Synthetic Corpus\n\n"
                "Body paragraph alpha with several lines of ordinary prose that "
                "must survive every filtering stage in the preparation pipeline."
            ),
            "trunc": 0,
        }

    monkeypatch.setattr(vlm_parse, "parse_page", fake_parse_page)
    cache_dir = str(tmp_path / "cache")

    async def run():
        return await vlm_parse.parse_page_verified(
            pdf, 0, "hash-l", str(tmp_path / "img"),
            {"api_key": "k"}, cache_dir, layout=_FakeLayout(_LAYOUT),
        )

    res = asyncio.run(run())
    assert res["source"] == "vlm"
    # 遮罩 = 快照区域（无框表）+ abandon 区域（版权段）
    rects = [pymupdf.Rect(r) for r in captured["mask"]]
    assert any(r.y0 > 600 for r in rects)                      # abandon（页脚带）
    assert any(250 < r.y0 < 400 for r in rects)                # 表格快照
    # 终态 md：title 提升 + abandon 已剔除（fake md 本身无版权段）
    assert "# 1 Introduction" in res["md"]
