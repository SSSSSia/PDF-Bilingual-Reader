"""快照区域版面仲裁回归测试（阶段12-T10 反馈 2，2026-09-15）。

用户验收反馈：新上传论文（SubgraphRAG，ICLR 版式）公式/表格/图片识别
处理很差。根因：附录 prompt 示例框（带矢量边框的纯文本块）被
cluster_drawings 当图表候选 → 整块快照 → truth 扣空（交叉校验失明，
p25-29 truth 仅 48 字符）+ 遮罩搅乱 VLM（p5 bag 0.64 降级）+ 降级路径
redact 挖空文本层（产物仅 200 字符）。DALK p17/p18 附录同款潜伏 bug。

修法（协议第 9 条：信号源优先）：版面模型 plain text 区域仲裁最终快照
区域——区域内文本字符被 plain text 区域覆盖 ≥50%（块主体口径）且内部
文本 ≥200 字符即否决。实测全语料分离度：真图真表 ≤14%、prompt 框
≥60%（SubgraphRAG p5 = 0.596），0.5 落空档中点。
"""

import pymupdf

from ocr.textlayer import _figure_regions


def _boxed_page(n_lines: int = 14) -> pymupdf.Page:
    """一页 612x792：中部画一个矢量边框，框内逐行插入正文（prompt 框形态）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 100, 500, 400), color=(0, 0, 0), width=1)
    for i in range(n_lines):
        page.insert_text(
            (115, 130 + i * 14),
            "System: Based on the retrieved knowledge answer it.",
            fontsize=10,
        )
    return page


def test_boxed_text_kept_without_layout_signal():
    """无版面信号（textlayer 独立路径）时几何候选照常快照——仲裁不越权。"""
    page = _boxed_page()
    figs = _figure_regions(page)
    assert len(figs) == 1, f"无边框文本应被几何检测命中: {figs}"


def test_boxed_text_vetoed_by_plain_text_regions():
    """版面模型判 plain text 的区域覆盖框内正文 → 快照否决（SubgraphRAG
    p22-29/DALK p17-18 实测形态）。"""
    page = _boxed_page()
    figs = _figure_regions(
        page, text_regions=[pymupdf.Rect(100, 100, 500, 400)]
    )
    assert figs == [], f"prompt 框应被仲裁否决: {figs}"


def test_uncovered_region_survives_arbitration():
    """plain text 区域不覆盖（真图/真表形态——内部文本不在任何正文区域内）
    → 快照保留（FG-RAG 无框表 0.56/DALK 表群回归口径）。"""
    page = _boxed_page()
    figs = _figure_regions(
        page, text_regions=[pymupdf.Rect(0, 600, 612, 792)]
    )
    assert len(figs) == 1


def test_small_text_region_below_floor_survives():
    """内部文本 <200 字符的区域不参评（小区域覆盖统计噪声大，防误杀）。"""
    page = _boxed_page(n_lines=3)  # 3 行 ≈ 150 字符 < 200
    figs = _figure_regions(
        page, text_regions=[pymupdf.Rect(100, 100, 500, 400)]
    )
    assert len(figs) == 1
