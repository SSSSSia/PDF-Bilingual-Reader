"""列感知阅读顺序重排回归测试（阶段12-T10 验收反馈，2026-09-15）。

用户验收反馈：DALK 首页标题/引言顺序乱。根因链（四层叠加）：
1. VLM 整页解析偶发乱序（引言在标题前，PaddleOCR-VL 免费档改不了）；
2. `_match_block` 无锚定包含：表格残块（"C %"→norm "c"）子串命中几乎所有
   段落（DALK p8 实测 6 段全配到一个 w=17 残块，排序 key 全为垃圾坐标）；
3. 重排守卫「任一文本段失配即整页放弃」：单字符措辞漂移（PDF 原文拼错
   "Constributions"、VLM 输出 "Contributions"）就令整页维持乱序；
4. 栏判定只认块级左右各 ≥3 窄块：右栏整栏一个粗粒度文本块的首页判不出
   双栏（DALK 首页 rights=1），重排仍被拦。

修法（类级通用，非个案）：匹配锚定 + 少数失配段跟随邻段 + 版面模型
plain text 区域作栏判定兜底信号。用例几何取自 DALK 首页实测坐标。
"""

import re

import pymupdf

from ocr.textlayer import (
    _MD_NORM_ALPHA,
    _column_reading_order,
    _match_block,
    _md_norm,
)


def _norms(raw_blocks):
    norms = [_md_norm(b[4]) for b in raw_blocks]
    alpha = [_MD_NORM_ALPHA.sub("", (b[4] or "").lower()) for b in raw_blocks]
    return norms, alpha


# ── 匹配锚定（根因 2）───────────────────────────────────────────────

def test_match_block_rejects_tiny_block_spurious_hit():
    """短块（"C %"→"c"）不得被子串包含命中任意段落——旧逻辑 `t[:24] in h`
    对 1 字符块恒真，DALK p8 六个段落全配到表格残块上。"""
    raw_blocks = [
        (489, 111, 506, 123, "C %"),  # 表格残块（norm 后仅 "c"）
        (70, 182, 524, 206, "Table 5: An example for the case study"),
    ]
    norms, alpha = _norms(raw_blocks)
    # 只允许命中真块 1，绝不命中残块 0
    h = _md_norm("Table 5: An example for the case study")[:24]
    assert _match_block(h, norms, alpha) == 1

def test_match_block_anchored_short_block_merge():
    """锚定语义保留既有能力：VLM 把短块与后继块粘成一段（"2187" + 页脚）
    时，段落以块文本开头即可命中。"""
    raw_blocks = [
        (288, 781, 310, 794, "2187"),
        (306, 74, 524, 98, "Findings of the Association for Computational"),
    ]
    norms, alpha = _norms(raw_blocks)
    h = _md_norm("2187 Findings of the Association for Computational")[:24]
    assert _match_block(h, norms, alpha) == 0

def test_match_block_short_head_returns_none():
    """<4 字符的段落头（页码/残渣）直接放弃匹配——由重排的邻段跟随兜住。"""
    raw_blocks = [(70, 182, 524, 206, "Table 5: An example for the case study")]
    norms, alpha = _norms(raw_blocks)
    assert _match_block("c", norms, alpha) is None
    assert _match_block("2187", norms, alpha) is None  # 头不含于任何块


# ── 重排守卫放宽 + 区域栏判定（根因 1/3/4，DALK 首页几何）───────────

# DALK 首页实测块几何（页宽 595）：标题/作者通栏，左栏摘要→引言→脚注，
# 右栏整栏一个粗粒度文本块（rights=1，块级栏判定必失败）
RAW_BLOCKS = [
    (102, 67, 493, 102, "DALK: Dynamic Co-Augmentation of LLMs and KG"),
    (113, 105, 481, 150, "Dawei Li, Shu Yang, Zhen Tan"),
    (158, 219, 202, 235, "Abstract"),
    (88, 244, 274, 566, "Recent advancements in large language models"),
    (71, 576, 154, 592, "1 Introduction"),
    (70, 598, 291, 747, "Alzheimer's Disease (AD) is a neurodegenerative"),
    (84, 751, 173, 775, "* Equal Constributions"),  # PDF 原文即拼错
    (306, 221, 526, 776, "As large language models (LLMs) achieve"),
]

# 版面模型 plain text 区域（该页实测）：左 2 右 2、栏沟干净（291→306）
COL_RECTS = [
    pymupdf.Rect(87, 244, 275, 566),
    pymupdf.Rect(71, 599, 291, 747),
    pymupdf.Rect(305, 222, 526, 478),
    pymupdf.Rect(305, 479, 526, 776),
]

# VLM 乱序输出（实测形态）：引言在前，标题/摘要在中后段，脚注单字符漂移
SCRAMBLED_MD = (
    "1 Introduction\n\n"
    "Alzheimer's Disease (AD) is a neurodegenerative disorder\n\n"
    "* Equal Contributions\n\n"
    "DALK: Dynamic Co-Augmentation of LLMs and KG\n\n"
    "Dawei Li, Shu Yang, Zhen Tan\n\n"
    "Abstract\n\n"
    "Recent advancements in large language models\n\n"
    "As large language models (LLMs) achieve"
)


def _paras(md):
    return [p.strip() for p in re.split(r"\n\s*\n", md) if p.strip()]


def test_reorder_with_region_signal_and_minority_drift():
    """区域信号 + 少数失配跟随：乱序页重排为标题→作者→摘要→引言→右栏，
    漂移脚注跟随其前邻段（不跨页乱跳、不挡整页重排）。"""
    out = _column_reading_order(RAW_BLOCKS, SCRAMBLED_MD, col_rects=COL_RECTS)
    paras = _paras(out)
    assert paras[0].startswith("DALK: Dynamic"), f"文档标题应居首: {paras[0][:30]!r}"
    assert paras[1].startswith("Dawei Li"), "作者行应紧跟标题"
    assert paras[2] == "Abstract"
    i_intro = next(i for i, p in enumerate(paras) if p == "1 Introduction")
    i_body = next(
        i for i, p in enumerate(paras) if p.startswith("Alzheimer")
    )
    i_note = next(i for i, p in enumerate(paras) if "Equal" in p)
    assert i_intro < i_body < i_note < len(paras) - 1, "左栏顺序：引言头→正文→脚注"
    assert paras[-1].startswith("As large language"), "右栏在左栏之后"


def test_reorder_without_region_signal_bails_on_coarse_blocks():
    """无区域信号（textlayer 降级路径）时块级栏判定失败即原样返回——
    区域信号是放行的唯一依据，不是无条件重排。"""
    assert _column_reading_order(RAW_BLOCKS, SCRAMBLED_MD) == SCRAMBLED_MD


def test_reorder_majority_miss_still_bails():
    """大面积失配（≥1/3，版式未被理解的数学页/参考文献页形态）仍整体
    放弃——放宽只针对少数措辞漂移，不放弃防搅乱纪律。"""
    md = SCRAMBLED_MD.replace(
        "Recent advancements in large language models",
        "Completely unrelated drifted text alpha",
    ).replace(
        "Dawei Li, Shu Yang, Zhen Tan",
        "Completely unrelated drifted text beta",
    ).replace(
        "As large language models (LLMs) achieve",
        "Completely unrelated drifted text gamma",
    )
    # 8 段中 4 段失配（漂移脚注 + 3 段伪漂移）> 8//3
    assert _column_reading_order(RAW_BLOCKS, md, col_rects=COL_RECTS) == md
