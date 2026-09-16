"""title_detect 几何标题检测测试。

用合成 PDF 验证排版铁律判定（第 1 页字号最大、最靠上的连续行 = 标题），
不依赖任何真实论文文件。
"""

import os

import pymupdf
import pytest

from ocr.title_detect import detect_title, norm_text


def _make_paper(path, title_lines, meta_title="", stamp=False):
    """合成典型论文首页：标题(16pt 两行) > 作者(11pt) > 正文(10pt)，
    可选 arXiv 左缘竖排印章(12pt)与 metadata title。"""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for ln in title_lines:
        page.insert_text((120, y), ln, fontsize=16, fontname="helv")
        y += 20
    y += 10
    page.insert_text((150, y), "Author One, Author Two", fontsize=11, fontname="helv")
    y += 24
    for i in range(6):
        page.insert_text(
            (72, y + i * 14),
            "This is body text with normal size font for filling the page.",
            fontsize=10,
            fontname="helv",
        )
    if stamp:
        page.insert_text(
            (12, 400), "arXiv:2404.16130v2 [cs.CL] 19 Feb 2025",
            fontsize=12, fontname="helv", rotate=90,
        )
    if meta_title:
        doc.set_metadata({"title": meta_title})
    doc.save(path)
    doc.close()


def test_geometry_picks_largest_topmost(tmp_path):
    p = str(tmp_path / "a.pdf")
    _make_paper(p, ["A Study of Nothing in Particular", "Second Line of the Title"])
    t = detect_title(p)
    assert t is not None
    assert norm_text(t).startswith("astudyofnothinginparticularsecondline")


def test_two_line_title_merged_and_metadata_preferred(tmp_path):
    p = str(tmp_path / "b.pdf")
    full = "A Study of Nothing in Particular Second Line of the Title"
    _make_paper(p, ["A Study of Nothing in Particular", "Second Line of the Title"],
                meta_title=full)
    assert detect_title(p) == full


def test_arxiv_vertical_stamp_ignored(tmp_path):
    p = str(tmp_path / "c.pdf")
    _make_paper(
        p, ["Real Paper Title Stands Here"], meta_title="Real Paper Title Stands Here",
        stamp=True,
    )
    t = detect_title(p)
    assert t is not None and "arXiv" not in t
    assert norm_text(t) == norm_text("Real Paper Title Stands Here")


def test_missing_file_returns_none(tmp_path):
    assert detect_title(str(tmp_path / "nope.pdf")) is None
