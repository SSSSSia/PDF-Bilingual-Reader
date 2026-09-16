"""VLM 整页结构化解析模块单测。

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


def test_verify_bag_immune_to_math_syntax():
    """交叉校验口径：真值 Unicode 数学碎屑 vs VLM LaTeX 语法不稀释。"""
    truth = "compute ∑𝑄𝑖 scores where 𝑄 is the query matrix and 𝑘 keys"
    vlm = (
        "compute \\(\\sum_{i} Q_i\\) scores where \\(Q\\) is the query "
        "matrix and \\(k\\) keys"
    )
    assert vlm_parse.verify_bag(vlm, truth) >= 0.90


def test_verify_bag_still_catches_real_loss():
    truth = "first paragraph content " * 10 + "second paragraph content " * 10
    assert vlm_parse.verify_bag("only first paragraph content" * 10, truth) < 0.90


def test_verify_bag_keeps_cjk():
    """中文文档：CJK 保留参与校验（不能被字母数字白名单剥空）。"""
    truth = "知识图谱问答系统通过检索增强生成技术提升回答质量"
    vlm = "知识图谱问答系统借助检索增强生成技术提高了回答的质量"
    assert vlm_parse.verify_bag(vlm, truth) >= 0.70


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


# ── 快照/truth 协同──────────────────────────────────────


def _make_figure_pdf(path) -> None:
    """正文段 + 栅格图（内含标签文字） + 图注的页面。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 60, 545, 110),
        "Body text paragraph that lives outside any figure region.",
    )
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 36))
    png = pix.tobytes("png")
    page.insert_image(pymupdf.Rect(170, 130, 420, 310), stream=png)
    # 图内标签：块主体落在图片区域内（redact/truth 扣除的对象）
    page.insert_textbox(
        pymupdf.Rect(180, 140, 410, 160), "Label Inside Figure"
    )
    page.insert_textbox(
        pymupdf.Rect(170, 320, 420, 345), "Figure 1: A test figure caption."
    )
    doc.save(path)
    doc.close()


def test_prepare_truth_excludes_figure_text(tmp_path):
    """truth 含正文与图注、不含图内标签；refs/regions 就位。"""
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "imgs"))
    assert prep["scanned"] is False
    assert len(prep["refs"]) == 1
    assert len(prep["regions"]) == 1
    assert "Body text paragraph" in prep["truth"]
    assert "Figure 1: A test figure caption" in prep["truth"]  # 图注豁免
    assert "Label Inside Figure" not in prep["truth"]


def test_prepare_scanned_classification(tmp_path):
    """无文本无快照 = 扫描页；短文本但有快照 = 数字页（同 extract_pages 口径）。"""
    blank = str(tmp_path / "blank.pdf")
    doc = pymupdf.open()
    doc.new_page()
    doc.save(blank)
    doc.close()
    assert vlm_parse._prepare(blank, 0, None)["scanned"] is True

    fig_only = str(tmp_path / "fig_only.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 36))
    page.insert_image(pymupdf.Rect(170, 130, 420, 310), stream=pix.tobytes("png"))
    page.insert_textbox(
        pymupdf.Rect(170, 320, 420, 345), "Figure 1: caption only."
    )
    doc.save(fig_only)
    doc.close()
    prep = vlm_parse._prepare(fig_only, 0, str(tmp_path / "imgs2"))
    assert prep["scanned"] is False  # 有快照 → 数字页（文字进图）
    assert prep["refs"]


def test_finalize_md_inserts_ref_before_caption(tmp_path):
    """快照引用按 caption 锚定插回 VLM 输出（引用在 caption 段之前）。"""
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    prep = vlm_parse._prepare(pdf, 0, str(tmp_path / "imgs3"))
    vlm_md = (
        "# Test Paper\n\nBody text paragraph that lives outside any figure "
        "region.\n\nFigure 1: A test figure caption.\n\nTail paragraph."
    )
    out = vlm_parse._finalize_md(prep, vlm_md)
    assert out.count("![Figure](") == 1
    assert out.index("![Figure](") < out.index("Figure 1: A test figure caption.")
    assert "Tail paragraph." in out


# ── 交叉校验降级────────────────────────────────────────

from cache.file_cache import ocr_key, read_cache, write_cache  # noqa: E402

_GOOD_VLM_MD = (
    "# Test Paper\n\nBody text paragraph that lives outside any figure "
    "region.\n\nFigure 1: A test figure caption."
)


def test_verified_accepts_good_vlm(tmp_path):
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    cache_dir = str(tmp_path / "cache")
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[(_GOOD_VLM_MD, "stop")]),
    ):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                pdf, 0, "hash1", str(tmp_path / "imgs"), _CFG, cache_dir
            )
        )
    assert out["source"] == "vlm"
    assert out["bag"] >= 0.90
    assert out["md"].count("![Figure](") == 1
    # 原始解析结果已落缓存（回归重放免重打 API）
    raw = read_cache(cache_dir, ocr_key("hash1", 0, vlm_parse.VLM_PARSE_MODEL))
    assert raw and raw["md"] == _GOOD_VLM_MD


def test_verified_raw_cache_hit_skips_api(tmp_path):
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    cache_dir = str(tmp_path / "cache")
    write_cache(
        cache_dir,
        ocr_key("hash1", 0, vlm_parse.VLM_PARSE_MODEL),
        {"md": _GOOD_VLM_MD, "trunc": 0},
    )
    mock = AsyncMock()
    with patch("ocr.vlm_parse._vlm_call", new=mock):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                pdf, 0, "hash1", str(tmp_path / "imgs"), _CFG, cache_dir
            )
        )
    mock.assert_not_awaited()
    assert out["source"] == "vlm"


def test_verified_falls_back_on_low_bag(tmp_path):
    """VLM 输出低质（bag 远低于阈值）→ 整页回退现行 textlayer 提取；
    低质原始输出不落缓存（下次运行自然重试，跨运行自愈）。"""
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    cache_dir = str(tmp_path / "cache")
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[("完全无关的输出", "stop")]),
    ):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                pdf, 0, "hash2", str(tmp_path / "imgs"), _CFG, cache_dir
            )
        )
    assert out["source"] == "textlayer"
    assert out["fallback_reason"] == "bag"
    assert out["bag"] < 0.90
    # 回退产物仍是完整文本层 markdown（正文/图注都在）
    assert "Body text paragraph" in out["md"]
    assert "Figure 1: A test figure caption" in out["md"]
    # 低质原始解析不落缓存
    assert (
        read_cache(cache_dir, ocr_key("hash2", 0, vlm_parse.VLM_PARSE_MODEL)) is None
    )


def test_verified_falls_back_on_api_error(tmp_path):
    """VLM API 失败 → 不抛异常，回退 textlayer（任务不失败）。"""
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=RuntimeError("HTTP 500")),
    ):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                pdf, 0, "hash3", str(tmp_path / "imgs"), _CFG, str(tmp_path / "cache")
            )
        )
    assert out["source"] == "textlayer"
    assert out["fallback_reason"] == "empty"
    assert "Body text paragraph" in out["md"]


def test_verified_keeps_vlm_when_fallback_also_fails(tmp_path):
    """降级路径自身异常（实测：链接注解损坏页 pymupdf4llm 崩）→ 保留
    未校验 VLM 输出，绝不让单页失败炸掉整篇任务。"""
    pdf = str(tmp_path / "fig.pdf")
    _make_figure_pdf(pdf)
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[(_GOOD_VLM_MD, "stop")]),
    ), patch(
        "ocr.vlm_parse.extract_page_md",
        side_effect=IndexError("list index out of range"),
    ), patch(
        "ocr.vlm_parse._textlayer_fallback",
        side_effect=IndexError("list index out of range"),
    ):
        # 查全率人为压低触发降级：VLM 输出与 truth 无重叠字符的假场景
        # （判定改为查全率/精确率双阈值，bag 仅作统计观测）
        with patch(
            "ocr.vlm_parse.verify_recall_precision", return_value=(0.1, 1.0)
        ), patch("ocr.vlm_parse.verify_bag", return_value=0.1):
            out = asyncio.run(
                vlm_parse.parse_page_verified(
                    pdf, 0, "hash6", str(tmp_path / "imgs"),
                    _CFG, str(tmp_path / "cache"),
                )
            )
    assert out["source"] == "vlm"
    assert out["fallback_reason"] == "fallback_error"
    assert "Body text paragraph" in out["md"]


def test_verified_scanned_page_short_circuits(tmp_path):
    blank = str(tmp_path / "blank.pdf")
    doc = pymupdf.open()
    doc.new_page()
    doc.save(blank)
    doc.close()
    mock = AsyncMock()
    with patch("ocr.vlm_parse._vlm_call", new=mock):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                blank, 0, "hash4", None, _CFG, str(tmp_path / "cache")
            )
        )
    mock.assert_not_awaited()
    assert out == {"scanned": True}


def test_verified_short_truth_accepts_vlm(tmp_path):
    """纯图页 truth 过短无从校验 → 直接接受 VLM 输出（无内容可损失）。"""
    fig_only = str(tmp_path / "fig_only.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 36))
    page.insert_image(pymupdf.Rect(170, 130, 420, 310), stream=pix.tobytes("png"))
    page.insert_textbox(pymupdf.Rect(170, 320, 420, 345), "Figure 1: caption only.")
    doc.save(fig_only)
    doc.close()
    with patch(
        "ocr.vlm_parse._vlm_call",
        new=AsyncMock(side_effect=[("Figure 1: caption only.", "stop")]),
    ):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                fig_only, 0, "hash5", str(tmp_path / "imgs"),
                _CFG, str(tmp_path / "cache"),
            )
        )
    assert out["source"] == "vlm"


# ── 顺序几何兜底与噪音覆盖──────────────────────────────


def test_finalize_md_repairs_column_swap(tmp_path):
    """VLM 整栏交换（形态）→ 高置信双栏页几何重排纠正。"""
    pdf = str(tmp_path / "two.pdf")
    _make_pdf(pdf, two_col=True)
    prep = vlm_parse._prepare(pdf, 0, None)
    left = [f"Left column block number {k}" for k in range(3)]
    right = [f"Right column block number {k}" for k in range(3)]
    swapped = "\n\n".join(right + left)  # 全部段落可定位坐标才触发重排
    out = vlm_parse._finalize_md(prep, swapped)
    assert out.index("Left column block number 0") < out.index(
        "Right column block number 0"
    )
    assert out.index("Left column block number 2") < out.index(
        "Right column block number 0"
    )


def test_finalize_md_leaves_unmatched_order_alone(tmp_path):
    """段落定位不到坐标（VLM 改写措辞）→ 不重排，原样返回（防搅乱）。"""
    pdf = str(tmp_path / "two.pdf")
    _make_pdf(pdf, two_col=True)
    prep = vlm_parse._prepare(pdf, 0, None)
    md = "# Title\n\nCompletely paraphrased text that matches no block.\n\nMore unmatched words here."
    assert vlm_parse._finalize_md(prep, md) == md


def test_vlm_noise_blocks_filtered_downstream():
    """VLM 输出残留的页码/页眉/页脚行由 split_into_blocks 过滤（验证覆盖）。"""
    from ocr.siliconflow import split_into_blocks

    md = (
        "# 3 Method\n\nThe encoder maps inputs to outputs.\n\n2195\n\n"
        "Peng et al.\n\narXiv:1706.03762v7 [cs.CL] 3 Dec 2024"
    )
    parts = split_into_blocks(md)
    assert parts == ["# 3 Method", "The encoder maps inputs to outputs."]


# ── 查全率/精确率双阈值──────────────

def test_verify_recall_precision_admits_latex_dilution():
    """数学页误杀根因：LaTeX 命令字母膨胀 VLM 侧字符、真值侧 Unicode 数学
    符被口径剥空——F1 <0.90 但查全率≈1（内容零丢失）。双阈值下应通过。
    配比取自实测（F1 0.895 / recall 0.9994 / precision
    0.811）：每 55 个正文 alnum 字符配 3 个数学符（真值侧剥空），
    LaTeX 表示新增 ~12 个命令字母。"""
    prose = "the probability estimate of the query answer satisfies the bound "
    truth = (prose + "ℙ𝑄≤𝔼 ") * 20
    vlm = (prose + r"\(\mathbb{P}(Q) \leq \mathbb{E}\) ") * 20
    f1 = vlm_parse.verify_bag(vlm, truth)
    rec, prec = vlm_parse.verify_recall_precision(vlm, truth)
    assert f1 < 0.90, f"此用例的前提：F1 口径确实稀释（实测 {f1:.3f}）"
    assert rec >= 0.90, "真值正文全覆盖"
    assert prec >= 0.75, "LaTeX 膨胀幅度在防幻觉下限之上"


def test_verify_recall_precision_rejects_under_output():
    """真欠输出（VLM 只吐部分内容）：查全率低 → 仍拒绝（p1/p3 实测形态）。"""
    truth = "first paragraph body text " * 20 + "second paragraph tail " * 20
    vlm = "first paragraph body text " * 10
    rec, prec = vlm_parse.verify_recall_precision(vlm, truth)
    assert rec < 0.90


def test_verify_recall_precision_rejects_hallucination():
    """幻觉输出（真值全覆盖 + 大量多余字符）：精确率低 → 拒绝。"""
    truth = "short factual sentence about retrieval"
    vlm = "short factual sentence about retrieval " + "hallucinated garbage text " * 20
    rec, prec = vlm_parse.verify_recall_precision(vlm, truth)
    assert rec >= 0.90
    assert prec < 0.75


def test_verified_degenerate_output_retried_once(tmp_path):
    """退化输出（hosted 偶发只回页码）：字母数字 <10% 真值 → 重试一次，
    第二次正常输出被采纳（整页只返回 '7'）。"""
    # 真值需 ≥200 alnum 字符才触发退化判定（防短页误重试）
    body = " ".join(
        f"Long body paragraph number {i} keeps enough prose characters."
        for i in range(12)
    )
    pdf = str(tmp_path / "degenerate.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 60, 545, 260), body)
    doc.save(pdf)
    doc.close()
    calls = AsyncMock(
        side_effect=[
            ("7", "stop"),  # 第一次：退化
            (body, "stop"),  # 重试：正常（与真值逐字一致）
        ]
    )
    with patch("ocr.vlm_parse._vlm_call", new=calls):
        out = asyncio.run(
            vlm_parse.parse_page_verified(
                pdf, 0, "hash_deg", str(tmp_path / "imgs"),
                _CFG, str(tmp_path / "cache"),
            )
        )
    assert calls.await_count == 2
    assert out["source"] == "vlm"
    assert "Long body paragraph number 0" in out["md"]
