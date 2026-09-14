"""阶段12 验收期修复：字号几何证据（标题提升/版权块剔除）单测。

合成 PDF 覆盖类级规则：粗体+字号判标题、编号行与标题同行合并、
相邻编号标题不误并、作者行（页 0 粗体短行）抑制、表列头（同行 ≥3
粗体组）剔除、句号收尾粗体行（run-in 引导）不提升、首页小字印刷块。
"""
import pymupdf

from ocr.textlayer import apply_font_evidence, collect_font_evidence


def _make_pdf(path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()  # A4 595x842
    y = 70
    page.insert_text((150, y), "Test Paper Title", fontsize=17, fontname="hebo")
    y += 30
    page.insert_text((200, y), "Alice Smith and Bob Lee", fontsize=12, fontname="hebo")
    y += 25
    page.insert_text((53, y), "Abstract", fontsize=11, fontname="hebo")
    y += 20
    for k in range(4):  # 正文（众数来源）
        page.insert_text(
            (53, y), f"Body paragraph {k} with enough text to dominate the size mode." * 2,
            fontsize=10, fontname="helv",
        )
        y += 16
    # 编号与标题文本同行拆两段（span 拆分形态）
    page.insert_text((53, y + 30), "1", fontsize=11, fontname="hebo")
    page.insert_text((68, y + 30), "Introduction", fontsize=11, fontname="hebo")
    # 相邻的下一级编号标题（不得与上一行合并）
    page.insert_text((53, y + 48), "1.1", fontsize=11, fontname="hebo")
    page.insert_text((75, y + 48), "Scope", fontsize=11, fontname="hebo")
    # 表列头：同行 ≥3 个并排粗体组
    page.insert_text((53, y + 80), "Col A", fontsize=10, fontname="hebo")
    page.insert_text((200, y + 80), "Col B", fontsize=10, fontname="hebo")
    page.insert_text((350, y + 80), "Col C", fontsize=10, fontname="hebo")
    # run-in 引导（句号收尾粗体）与小写粗体表内句
    page.insert_text((53, y + 110), "Datasets.", fontsize=10, fontname="hebo")
    page.insert_text((53, y + 130), "llms when solving tasks here.", fontsize=10, fontname="hebo")
    # 首页小字印刷（版权块）
    page.insert_text(
        (53, 780),
        "Permission to make digital or hard copies of all or part of this work",
        fontsize=8, fontname="helv",
    )
    page.insert_text(
        (53, 792),
        "for personal or classroom use is granted without fee.",
        fontsize=8, fontname="helv",
    )
    doc.save(path)
    doc.close()


def test_collect_font_evidence(tmp_path):
    pdf = str(tmp_path / "ev.pdf")
    _make_pdf(pdf)
    doc = pymupdf.open(pdf)
    ev = collect_font_evidence(doc[0], is_first_page=True)
    doc.close()
    heads = dict(ev["headings"])  # norm → level（norm 唯一）
    assert ev["body_size"] == 10.0
    assert heads.get("testpapertitle") == 1          # 文档标题
    assert heads.get("abstract") in (2, 3)            # 通用节名（页 0 白名单）
    assert heads.get("1introduction") == 2            # 编号同行合并，深度 0
    assert heads.get("11scope") == 3                  # 深度 1；未与上行误并
    # 抑制/剔除：作者、表列头、句号引导、小写表句
    for bad in ("alicesmithandboblee", "cola", "datasets", "llmswhensolvingtaskshere"):
        assert bad not in heads
    # 首页小字印刷块：两行 8pt 合并成块
    assert any("permissiontomakedigital" in s for s in ev["smallprint"])


def test_apply_font_evidence_promotes_and_removes():
    ev = {
        "body_size": 10.0,
        "headings": [("1introduction", 2), ("abstract", 3)],
        "smallprint": ["permissiontomakedigitalorhardcopies"],
    }
    md = (
        "Test Paper Title\n\nAbstract\n\nBody text here.\n\n"
        "1 Introduction\n\nMore body.\n\n"
        "Permission to make digital or hard copies of all or part of this work "
        "for personal use.\n\nTail."
    )
    out = apply_font_evidence(md, ev)
    assert "### Abstract" in out
    assert "## 1 Introduction" in out
    assert "Permission to make" not in out       # 小字印刷剔除
    assert "Tail." in out
    assert "Body text here." in out


def test_apply_font_evidence_idempotent_and_guarded():
    ev = {"headings": [("abstract", 3)], "smallprint": []}
    # 已是标题的段落不重复加前缀；长正文段不误提升
    md = "### Abstract\n\nAbstract-like sentence that is actually a long body "
    md += "paragraph discussing the abstract concept " * 3
    out = apply_font_evidence(md, ev)
    assert out.count("###") == 1
    long_ev = {"headings": [("averylongheading", 2)], "smallprint": []}
    body = "A very long heading continuation sentence that exceeds the guard."
    assert apply_font_evidence(body, long_ev) == body
