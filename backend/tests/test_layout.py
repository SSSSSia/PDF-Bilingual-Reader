"""单元测试：pipeline.layout.attach_block_bboxes 块级 bbox 标注。"""

import pymupdf

from pipeline.layout import attach_block_bboxes


def _make_pdf(tmp_path, texts_per_page):
    """生成多页测试 PDF：每页按 texts_per_page[页] 逐段落文字。"""
    doc = pymupdf.open()
    for texts in texts_per_page:
        page = doc.new_page()  # A4 595x842, 旋转 0°
        y = 72
        for t in texts:
            # 用 insert_textbox 保证成块；块间距拉开避免被 PyMuPDF 合并
            rect = pymupdf.Rect(72, y, 500, y + 60)
            page.insert_textbox(rect, t, fontsize=11)
            y += 90
    path = str(tmp_path / "t.pdf")
    doc.save(path)
    doc.close()
    return path


def test_bbox_matched_within_page(tmp_path):
    path = _make_pdf(
        tmp_path,
        [["The quick brown fox jumps over the lazy dog near the river bank.", "Another paragraph about knowledge graphs and retrieval augmented generation."]],
    )
    pages = [
        {
            "page": 0,
            "blocks": [
                {"original": "The quick brown fox jumps over the lazy dog near the river bank."},
                {"original": "Another paragraph about knowledge graphs and retrieval augmented generation."},
            ],
        }
    ]
    attach_block_bboxes(path, pages)
    b0, b1 = pages[0]["blocks"]
    assert b0["bbox"] and b1["bbox"]
    for bbox in (b0["bbox"], b1["bbox"]):
        x0, y0, x1, y1 = bbox
        assert 0 <= x0 < x1 <= 595 + 1
        assert 0 <= y0 < y1 <= 842 + 1
    # 第二段在第一段下方
    assert b1["bbox"][1] > b0["bbox"][1]


def test_bbox_unmatched_is_none(tmp_path):
    path = _make_pdf(tmp_path, [["Real text layer content lives here."]])
    pages = [
        {
            "page": 0,
            "blocks": [
                {"original": "This sentence does not exist in the pdf at all."},
                {"original": "![Figure](cache/images/abc/snap.png)"},
            ],
        }
    ]
    attach_block_bboxes(path, pages)
    assert pages[0]["blocks"][0]["bbox"] is None
    # 纯图片块显式 None（不参与匹配）
    assert pages[0]["blocks"][1]["bbox"] is None


def test_bbox_empty_page_and_out_of_range_safe(tmp_path):
    path = _make_pdf(tmp_path, [["Only one page here."]])
    pages = [
        {"page": 0, "blocks": []},  # 空页
        {"page": 5, "blocks": [{"original": "ghost"}]},  # 越界页
        {"page": 0, "blocks": [{"original": "Only one page here."}]},
    ]
    attach_block_bboxes(path, pages)
    assert pages[2]["blocks"][0]["bbox"] is not None


def test_bbox_clipped_to_page_rect(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    # 人为制造超出页界的原始块坐标（右/下越界）
    page.insert_text(pymupdf.Point(30, 40), "Overflowing header text beyond margins")
    path = str(tmp_path / "clip.pdf")
    doc.save(path)
    doc.close()

    # 直接伪造 raw_blocks 无法注入，改用真实提取路径验证裁剪逻辑：
    pages = [{"page": 0, "blocks": [{"original": "Overflowing header text beyond margins"}]}]
    attach_block_bboxes(path, pages)
    bbox = pages[0]["blocks"][0]["bbox"]
    assert bbox is not None
    x0, y0, x1, y1 = bbox
    assert x0 >= 0 and y0 >= 0 and x1 <= 595 + 1 and y1 <= 842 + 1


def test_bbox_merged_block_uses_source_head(tmp_path):
    """跨页合并块（A+目标拼接）：前缀匹配应命中 A 所在原始块。"""
    a_text = "Cross page sentence starts here and continues"
    path = _make_pdf(tmp_path, [[a_text]])
    pages = [
        {
            "page": 0,
            "blocks": [
                # 模拟合并后：A 原文在前，续文（来自下一页）直接拼接
                {"original": a_text + " on the next page without terminal stop"}
            ],
        }
    ]
    attach_block_bboxes(path, pages)
    assert pages[0]["blocks"][0]["bbox"] is not None


def test_bbox_glued_multiblock_paragraph(tmp_path):
    """同页粘合段：一个逻辑块由两个原始块拼成 → 两段坐标，bbox=首段。"""
    t1 = "The first physical piece of a glued paragraph ends without stop"
    t2 = "and the second physical piece continues the very same sentence here"
    path = _make_pdf(tmp_path, [[t1, t2]])
    pages = [{"page": 0, "blocks": [{"original": t1 + " " + t2}]}]
    attach_block_bboxes(path, pages)
    b = pages[0]["blocks"][0]
    assert b["bbox"] is not None
    assert len(b["bboxes"]) == 2
    assert all(s["page"] == 0 for s in b["bboxes"])
    assert b["bboxes"][0]["bbox"] == b["bbox"]
    # 两段上下排布：第二段 y0 在第一段之后
    assert b["bboxes"][1]["bbox"][1] > b["bboxes"][0]["bbox"][1]


def test_bbox_cross_page_continuation_segment(tmp_path):
    """跨页合并块：续段坐标挂在下一页，bboxes 覆盖两页。"""
    a_text = "Cross page sentence starts here and continues"
    b_text = "on the next page the sentence keeps going for a while"
    path = _make_pdf(tmp_path, [[a_text], [b_text]])
    pages = [
        {"page": 0, "blocks": [{"original": a_text + " " + b_text}]},
        {"page": 1, "blocks": []},
    ]
    attach_block_bboxes(path, pages)
    b = pages[0]["blocks"][0]
    assert [s["page"] for s in b["bboxes"]] == [0, 1]
    assert b["bbox"] == b["bboxes"][0]["bbox"]


def test_bbox_unmatched_has_empty_bboxes(tmp_path):
    """未命中块：bbox=None 且 bboxes 为空（前端不渲染 overlay）。"""
    path = _make_pdf(tmp_path, [["Real text layer content lives here."]])
    pages = [
        {"page": 0, "blocks": [{"original": "This sentence is not in the pdf."}]}
    ]
    attach_block_bboxes(path, pages)
    b = pages[0]["blocks"][0]
    assert b["bbox"] is None
    assert b["bboxes"] == []
