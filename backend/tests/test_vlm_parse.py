"""阶段12-T1：VLM 整页结构化解析模块单测。

覆盖：纯函数指标（norm/bag_f1）、双栏几何判定、_vlm_call 的成功/空输出/
HTTP 错误/缺 Key 路径、parse_page 截断对半重发（单栏上下切/双栏左右切）。
不真调 API——httpx.AsyncClient.post 按现有 test_providers 模式 mock。
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pymupdf
import pytest

from ocr import vlm_parse

_CFG = {"api_key": "test-key", "api_url": "https://example/v1", "model": "m"}


def _vlm_resp(content, finish_reason="stop"):
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ]
    }


def _http_resp(json_data, status_code=200):
    class _R:
        def json(self):
            return json_data

    r = _R()
    r.status_code = status_code
    r.text = ""
    return r


def _make_pdf(path, two_col: bool) -> None:
    """生成测试 PDF：双栏页左右各 3 个窄块，单栏页通栏若干块。
    双栏块必须多行且左右错行——get_text("blocks") 按 y 带合并，同行左右
    文本会并成一个通栏块（正是 textlayer 要对抗的 y 带交错）。"""
    doc = pymupdf.open()
    page = doc.new_page()  # 612x792
    if two_col:
        for k in range(3):
            page.insert_textbox(
                pymupdf.Rect(50, 80 + k * 130, 250, 175 + k * 130),
                f"Left column block number {k}\nwith two lines of text.",
            )
            page.insert_textbox(
                pymupdf.Rect(320, 130 + k * 130, 520, 225 + k * 130),
                f"Right column block number {k}\nwith two lines of text.",
            )
    else:
        for k in range(4):
            page.insert_textbox(
                pymupdf.Rect(50, 80 + k * 60, 560, 130 + k * 60),
                f"Full width body paragraph number {k} of the single column page.",
            )
    doc.save(path)
    doc.close()


# ── 纯函数 ───────────────────────────────────────────────────────────


def test_norm_strips_whitespace_and_nfkc():
    assert vlm_parse.norm("a b\u00a0c\t d\n") == "abcd"  # NFKC 把 nbsp 归一为空格
    assert vlm_parse.norm(None) == ""


def test_bag_f1_order_insensitive():
    assert vlm_parse.bag_f1("abc", "cba") == 1.0
    assert vlm_parse.bag_f1("abc", "ab") == pytest.approx(0.8)  # 2*2/(3+2)
    assert vlm_parse.bag_f1("", "") == 0.0


# ── 双栏几何判定 ─────────────────────────────────────────────────────


def test_two_column_detection(tmp_path):
    one = tmp_path / "one.pdf"
    two = tmp_path / "two.pdf"
    _make_pdf(str(one), two_col=False)
    _make_pdf(str(two), two_col=True)
    doc = pymupdf.open(str(one))
    assert vlm_parse.two_column(doc[0]) is False
    doc.close()
    doc = pymupdf.open(str(two))
    assert vlm_parse.two_column(doc[0]) is True
    doc.close()


# ── _vlm_call ────────────────────────────────────────────────────────


def test_vlm_call_ok_and_keeps_finish_reason():
    with patch(
        "ocr.vlm_parse.httpx.AsyncClient.post",
        new=AsyncMock(return_value=_http_resp(_vlm_resp("hi", "length"))),
    ):
        text, reason = asyncio.run(vlm_parse._vlm_call(b"png", _CFG))
    assert (text, reason) == ("hi", "length")


def test_vlm_call_empty_content():
    with patch(
        "ocr.vlm_parse.httpx.AsyncClient.post",
        new=AsyncMock(return_value=_http_resp(_vlm_resp(None))),
    ):
        text, reason = asyncio.run(vlm_parse._vlm_call(b"png", _CFG))
    assert (text, reason) == ("", "stop")


def test_vlm_call_http_error_raises():
    with patch(
        "ocr.vlm_parse.httpx.AsyncClient.post",
        new=AsyncMock(return_value=_http_resp({}, status_code=500)),
    ):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            asyncio.run(vlm_parse._vlm_call(b"png", _CFG))


def test_vlm_call_missing_key_raises():
    with pytest.raises(ValueError, match="API Key"):
        asyncio.run(vlm_parse._vlm_call(b"png", {"api_key": ""}))


# ── parse_page 截断对半重发 ──────────────────────────────────────────


def test_parse_page_single_column_splits_top_bottom(tmp_path):
    """单栏页截断 → 上下对半重发拼接；整页输出被丢弃（尾部已残）。"""
    pdf = str(tmp_path / "one.pdf")
    _make_pdf(pdf, two_col=False)
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(
            side_effect=[("full-page-truncated", "length"), ("top", "stop"), ("bottom", "stop")]
        ),
    ) as mock_call:
        out = asyncio.run(vlm_parse.parse_page(pdf, 0, _CFG))
    assert out["md"] == "top\n\nbottom"
    assert out["trunc"] == 1
    assert mock_call.await_count == 3


def test_parse_page_two_column_splits_left_right(tmp_path):
    """双栏页截断 → 左右对半重发（clip 以页宽中线为界）。"""
    pdf = str(tmp_path / "two.pdf")
    _make_pdf(pdf, two_col=True)
    clips: list[tuple | None] = []

    def fake_render(file_path, pno, clip=None, mask_regions=None):
        clips.append(clip)
        return b"png"

    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[("x", "length"), ("L", "stop"), ("R", "stop")]),
    ), patch("ocr.vlm_parse._render_png", side_effect=fake_render):
        out = asyncio.run(vlm_parse.parse_page(pdf, 0, _CFG))
    assert out["md"] == "L\n\nR"
    assert out["trunc"] == 1
    # 第 1 次整页无 clip；两个半页 clip 以页宽中线为界（默认 A4 595x842）
    doc = pymupdf.open(pdf)
    w, h = doc[0].rect.width, doc[0].rect.height
    doc.close()
    assert clips[0] is None
    assert clips[1] == (0, 0, w / 2 + 2, h)
    assert clips[2] == (w / 2 - 2, 0, w, h)


def test_parse_page_no_truncation_single_call(tmp_path):
    pdf = str(tmp_path / "one.pdf")
    _make_pdf(pdf, two_col=False)
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(return_value=("full text", "stop")),
    ) as mock_call:
        out = asyncio.run(vlm_parse.parse_page(pdf, 0, _CFG))
    assert out["md"] == "full text"
    assert out["trunc"] == 0
    assert mock_call.await_count == 1


def test_parse_page_half_still_truncated_counts(tmp_path):
    """半页仍截断：结果保留但 trunc 计 2（观测漂移用）。"""
    pdf = str(tmp_path / "one.pdf")
    _make_pdf(pdf, two_col=False)
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[("x", "length"), ("top", "stop"), ("bot…", "length")]),
    ):
        out = asyncio.run(vlm_parse.parse_page(pdf, 0, _CFG))
    assert out["md"] == "top\n\nbot…"
    assert out["trunc"] == 2


def test_parse_page_mask_regions_forwarded(tmp_path):
    """mask_regions 必须透传给每次渲染（含半页重渲染）。"""
    pdf = str(tmp_path / "one.pdf")
    _make_pdf(pdf, two_col=False)
    regions = [(50, 50, 200, 120)]
    seen: list = []

    def fake_render(file_path, pno, clip=None, mask_regions=None):
        seen.append(mask_regions)
        return b"png"

    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[("x", "length"), ("a", "stop"), ("b", "stop")]),
    ), patch("ocr.vlm_parse._render_png", side_effect=fake_render):
        asyncio.run(vlm_parse.parse_page(pdf, 0, _CFG, mask_regions=regions))
    assert seen == [regions, regions, regions]
