"""快照区域版面仲裁回归测试。

用户验收反馈：新上传论文（ICLR 版式）公式/表格/图片识别
处理很差。根因：附录 prompt 示例框（带矢量边框的纯文本块）被
cluster_drawings 当图表候选 → 整块快照 → truth 扣空（交叉校验失明，
p25-29 truth 仅 48 字符） + 遮罩搅乱 VLM（p5 bag 0.64 降级） + 降级路径
redact 挖空文本层（产物仅 200 字符）。/p18 附录同款潜伏 bug。

修法（协议第 9 条：信号源优先）：版面模型 plain text 区域仲裁最终快照
区域——区域内文本字符被 plain text 区域覆盖 ≥50%（块主体口径）且内部
文本 ≥200 字符即否决。实测全语料分离度：真图真表 ≤14%、prompt 框
≥60%（= 0.596），0.5 落空档中点。
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
    """版面模型判 plain text 的区域覆盖框内正文 → 快照否决（
    p22-29/）。"""
    page = _boxed_page()
    figs = _figure_regions(
        page, text_regions=[pymupdf.Rect(100, 100, 500, 400)]
    )
    assert figs == [], f"prompt 框应被仲裁否决: {figs}"


def test_uncovered_region_survives_arbitration():
    """plain text 区域不覆盖（真图/真表形态——内部文本不在任何正文区域内）
    → 快照保留（无框表 0.56/表群回归口径）。"""
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


# ── 注释边界收夹────────────────────

def _figure_page_with_caption():
    """页面上部一个矢量图（矩形），下方紧跟 Figure 注释行 + 正文段。"""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 80, 500, 300), color=(0, 0, 0), width=1)
    page.insert_text((110, 320), "Figure 1: The framework overview.", fontsize=10)
    page.insert_text((110, 345), "This body paragraph follows the caption.", fontsize=10)
    return page


def test_caption_clamp_releases_caption_and_body():
    """图注被卷进快照 → 底边收到注释上方：注释与其后正文留在文本流
    （注释被遮罩 → 图甩页尾、注释未翻译）。"""
    page = _figure_page_with_caption()
    figs = _figure_regions(page)
    assert len(figs) == 1
    # 注释行（y≈312-322）不再在区域内
    assert figs[0].y1 < 312, f"底边应收夹到注释上方: {figs[0]}"
    # 图主体未被误切（矢量矩形到 y=300）
    assert figs[0].y1 >= 300


def test_caption_clamp_guard_protects_model_regions():
    """守卫：收夹会切断模型 figure/table 区域主体 → 放弃收夹维持现状
    （跨栏合并大块实测：图注下方还有另一栏的表，切了就丢表）。"""
    page = _figure_page_with_caption()
    # 右下角再造一个"模型 table 区域"，横跨注释下沿——收夹会切到它
    model_tab = pymupdf.Rect(320, 310, 560, 420)
    figs = _figure_regions(
        page, extra_regions=[model_tab], caption_regions=[]
    )
    # 含模型区域合并后的大区域未被收夹（守卫触发）
    merged = [r for r in figs if (r & model_tab).get_area() > 0]
    assert any(r.y1 > 420 - 4 for r in merged), f"守卫应放弃收夹: {merged}"


def test_caption_clamp_keyword_block_and_model_caption_equivalent():
    """关键词注释行（Figure N 开头 ≤45pt 块）与模型 caption 区域等效——
    模型漏检注释的页由关键词兜底（用户建议的 Figure/Table 关键词方案）。"""
    page = _figure_page_with_caption()
    # 不给模型 caption 区域：关键词块兜底同样完成收夹
    figs = _figure_regions(page, caption_regions=[])
    assert figs[0].y1 < 312
    # 给模型 caption 区域：结果一致
    figs2 = _figure_regions(
        page, caption_regions=[pymupdf.Rect(105, 312, 400, 324)]
    )
    assert abs(figs[0].y1 - figs2[0].y1) < 1
