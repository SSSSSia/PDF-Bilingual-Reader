"""文本层提取 vs PaddleOCR-VL 整页结构化解析的 A/B 对比（2026-09-14）。

回答换引擎决策问题："数字页也走 PaddleOCR-VL 结构化解析，质量能否全面
超过现行 textlayer（pymupdf4llm classic + 启发式补丁）？"

与 2026-09-08 的 ab_textlayer_vs_ocr.py（docs/archive/文本层vsOCR对比.md）
同源但维度更全：旧工具只测了「顺序敏感字符相似度」，该指标在双栏页上
系统性惩罚正确的列序输出（真值 get_text 本身按 y 带交错，列序正确的
输出反而相似度下降）。本次新增四个维度：

1. char_sim  顺序敏感相似度（与旧报告同口径，保证可比）；
2. bag_sim   顺序无关字符 F1（Counter 交集）——衡量「内容完整度」，
             剥离阅读顺序的影响；
3. order     双栏阅读顺序得分——真值块按左右列分组，输出中「左块在
             右块前」的配对正确率（1.0=完美列序，0.5≈y 带交错）；
4. formula   Route B 输出的 $...$/$$...$$ 公式段与 LaTeX 宏计数 vs
             真值数学字体 span 数（CMMI/CMSY/数学字母区）；
5. table     Route B 表格标记（<table> / markdown 分隔行）vs
             pymupdf find_tables 检出数 vs Route A 表格快照数。

截断验证：PaddleOCR-VL 总上下文 16384 / max_tokens 8192，密集页可能
finish_reason=length。命中时按列对半裁剪重发拼接（两栏页左右半、单栏页
上下半），统计发生率与缓解成功率。

用法：.venv/Scripts/python.exe backend/tools/ab_textlayer_vs_vlm_parse.py
输出：docs/VLM结构化解析对比.md
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import httpx
import pymupdf

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from ocr import siliconflow  # noqa: E402
from ocr.textlayer import extract_pages  # noqa: E402

DOCS = BACKEND.parent / "docs"

# 语料：真实论文（用户论文目录），版式覆盖 EMNLP/ACM 双栏、NeurIPS 单栏
PAPERS: list[tuple[str, list[int]]] = [
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\DALK.pdf", [0, 1, 4, 8]),
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\FG-RAG.pdf", [0, 2, 4]),
    (r"E:\ZiLiao\论文阅读\2017Attention is all you need.pdf", [0, 1, 3]),
    (r"E:\ZiLiao\论文阅读\GraphRAG&KGQA\A Survey of Graph Retrieval-Augmented Generation for Customized Large Language Models.pdf", [0, 5]),
]

_IMG_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_ALNUM = re.compile(r"[^a-z0-9]+")
_MATH_FONT = re.compile(r"(CMMI|CMSY|CMEX|Math|MT-Extra|Euclid)", re.IGNORECASE)
_TEX_MACRO = re.compile(r"\\[a-zA-Z]+")
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{3,}\|[\s:|-]*$", re.M)


def norm(s: str) -> str:
    """与旧工具同口径：NFKC + 去全部空白。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def md_norm(s: str) -> str:
    """前缀匹配用：只留小写字母数字。"""
    return _ALNUM.sub("", (s or "").lower())


def bag_f1(a: str, b: str) -> float:
    """顺序无关字符 F1（内容完整度）。"""
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    return 2 * inter / max(len(a) + len(b), 1)


# ── 真值与几何分析 ───────────────────────────────────────────────────

def page_truth(page) -> tuple[str, list]:
    """(y 序字符流真值, 原始块)。"""
    return page.get_text("text"), page.get_text("blocks")


def math_span_count(page) -> int:
    """数学字体 span 数（真值侧公式量代理）。"""
    n = 0
    d = page.get_text("dict")
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                t = sp.get("text", "")
                if _MATH_FONT.search(sp.get("font", "")) or any(
                    0x1D400 <= ord(c) <= 0x1D7FF for c in t
                ):
                    n += 1
    return n


def column_blocks(raw_blocks: list, page_w: float) -> tuple[list[str], list[str]] | None:
    """双栏页的（左栏块前缀, 右栏块前缀）归一化文本；非双栏返回 None。
    判定阈值与 textlayer._column_reading_order 一致（0.55 通栏比 + 干净栏沟）。"""
    rects = [pymupdf.Rect(b[:4]) for b in raw_blocks]
    lefts, rights = [], []
    for r, b in zip(rects, raw_blocks):
        if r.width > 0.55 * page_w or r.height > 200:
            continue  # 通栏块（标题/图表）与超长块不参与顺序评分
        if r.x1 <= page_w / 2 + 5:
            lefts.append(md_norm(b[4] if len(b) > 4 else "")[:24])
        elif r.x0 >= page_w / 2 - 5:
            rights.append(md_norm(b[4] if len(b) > 4 else "")[:24])
    lefts = [t for t in lefts if len(t) >= 12][:6]
    rights = [t for t in rights if len(t) >= 12][:6]
    if len(lefts) < 3 or len(rights) < 3:
        return None
    lx = [r for r in rects if r.x1 <= page_w / 2 + 5 and r.width <= 0.55 * page_w]
    rx = [r for r in rects if r.x0 >= page_w / 2 - 5 and r.width <= 0.55 * page_w]
    if lx and rx and max(r.x1 for r in lx) > min(r.x0 for r in rx) + 5:
        return None  # 无干净分栏沟
    return lefts, rights


def order_score(out_md: str, cols) -> float | None:
    """左块先于右块的配对正确率（输出中找不到的块不计入）。"""
    lefts, rights = cols
    hay = md_norm(out_md)
    lpos = [hay.find(t) for t in lefts]
    rpos = [hay.find(t) for t in rights]
    lp = [p for p in lpos if p >= 0]
    rp = [p for p in rpos if p >= 0]
    if not lp or not rp:
        return None
    ok = sum(1 for a in lp for b in rp if a < b)
    return ok / (len(lp) * len(rp))


# ── Route B：PaddleOCR-VL 结构化解析（与生产扫描页同通道同提示） ─────

async def _vlm_call(png: bytes, cfg: dict) -> tuple[str, str]:
    """返回 (text, finish_reason)。payload 与 siliconflow._ocr_image 完全一致，
    但保留 finish_reason 供截断统计（生产版把它折叠成文本后缀了）。"""
    import base64

    b64 = base64.b64encode(png).decode("ascii")
    payload = {
        "model": cfg.get("model", "PaddlePaddle/PaddleOCR-VL-1.5"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
                    {"type": "text", "text": siliconflow.OCR_PROMPT},
                ],
            }
        ],
        "max_tokens": 8192,
        "temperature": 0.01,
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{cfg.get('api_url', 'https://api.siliconflow.cn/v1')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"VLM 请求失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    choice = data["choices"][0]
    return (choice["message"]["content"] or "").strip(), choice.get("finish_reason") or ""


def _render_png(path: str, pno: int, clip: tuple | None = None) -> bytes:
    doc = pymupdf.open(path)
    try:
        p = doc[pno]
        r = pymupdf.Rect(clip) & p.rect if clip else p.rect
        pix = p.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), clip=r, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


async def vlm_parse_page(path: str, pno: int, cfg: dict) -> dict:
    """整页解析；finish_reason=length 时按栏向对半裁剪重发拼接。"""
    t0 = time.perf_counter()
    trunc = 0
    text, reason = await _vlm_call(_render_png(path, pno), cfg)
    if reason == "length":
        trunc = 1
        doc = pymupdf.open(path)
        w, h = doc[pno].rect.width, doc[pno].rect.height
        doc.close()
        # 判双栏：左右对半；否则上下对半
        raw = pymupdf.open(path)
        cols = column_blocks(raw[pno].get_text("blocks"), w)
        raw.close()
        halves = (
            [(0, 0, w / 2 + 2, h), (w / 2 - 2, 0, w, h)]
            if cols
            else [(0, 0, w, h / 2 + 2), (0, h / 2 - 2, w, h)]
        )
        parts = []
        for clip in halves:
            t, r = await _vlm_call(_render_png(path, pno, clip), cfg)
            parts.append(t)
            if r == "length":
                trunc += 1
        text = "\n\n".join(p for p in parts if p.strip())
    return {"text": text, "trunc": trunc, "elapsed": time.perf_counter() - t0}


# ── 公式/表格统计 ────────────────────────────────────────────────────

def count_formulas(md: str) -> tuple[int, int]:
    """(定界公式段数, LaTeX 宏种数)。定界式覆盖 \\(..\\)、\\[..\\]（PaddleOCR-VL
    实测输出形态，与 pymupdf4llm 的 LaTeX PDF 输出同构）与 $..$/$$..$$。"""
    n = len(re.findall(r"\\\(.*?\\\)", md, re.S))
    n += len(re.findall(r"\\\[.*?\\\]", md, re.S))
    n += len(re.findall(r"(?<!\$)\$(?!\$)[^\$\n]+?(?<!\$)\$(?!\$)", md))
    n += len(re.findall(r"\$\$.+?\$\$", md, re.S))
    return n, len(set(_TEX_MACRO.findall(md)))


def count_tables(md: str) -> int:
    return md.count("<table") + len(_MD_TABLE_SEP.findall(md))


# ── 主流程 ───────────────────────────────────────────────────────────

async def main() -> None:
    cfg_path = os.path.expandvars(r"%APPDATA%\pdf-reader\config.json")
    cfg_all = json.load(open(cfg_path, encoding="utf-8"))
    ocr_cfg = cfg_all["ocr"]

    rows: list[dict] = []
    samples: list[str] = []
    sem = asyncio.Semaphore(3)

    async def guarded_parse(path, pno):
        async with sem:
            return await vlm_parse_page(path, pno, ocr_cfg)

    for path, pages in PAPERS:
        if not os.path.isfile(path):
            print(f"[skip] 文件不存在: {path}", flush=True)
            continue
        name = Path(path).stem[:28]
        print(f"== {name} pages={pages}", flush=True)

        image_dir = tempfile.mkdtemp(prefix="ab_vlm_")
        t0 = time.perf_counter()
        tl_mds = extract_pages(path, pages, image_dir)  # 现行生产管线（v18）
        tl_elapsed = time.perf_counter() - t0

        vlms = await asyncio.gather(*(guarded_parse(path, p) for p in pages))

        doc = pymupdf.open(path)
        for i, pno in enumerate(pages):
            page = doc[pno]
            truth_raw, raw_blocks = page_truth(page)
            truth = norm(truth_raw)
            cols = column_blocks(raw_blocks, page.rect.width)
            tl_md = tl_mds[i] or ""
            vlm_md = vlms[i]["text"]
            tl_snap_tab = len(re.findall(r"!\[Figure\]\([^)]*tab_[^)]*\)", tl_md))
            tl_clean = _IMG_REF.sub("", tl_md)
            tl, vlm = norm(tl_clean), norm(vlm_md)
            tl_f = count_formulas(tl_clean)
            vlm_f = count_formulas(vlm_md)
            row = {
                "paper": name,
                "page": pno + 1,
                "truth_chars": len(truth),
                "math_spans": math_span_count(page),
                "pdf_tables": len(page.find_tables().tables),
                "tl_sim": SequenceMatcher(None, tl, truth).ratio(),
                "vlm_sim": SequenceMatcher(None, vlm, truth).ratio(),
                "tl_bag": bag_f1(tl, truth),
                "vlm_bag": bag_f1(vlm, truth),
                "vlm_len": len(vlm) / max(len(truth), 1),
                "tl_order": order_score(tl_clean, cols) if cols else None,
                "vlm_order": order_score(vlm_md, cols) if cols else None,
                "tl_formula": tl_f,
                "vlm_formula": vlm_f,
                "tl_tab_snap": tl_snap_tab,
                "vlm_tables": count_tables(vlm_md),
                "vlm_trunc": vlms[i]["trunc"],
                "vlm_elapsed": vlms[i]["elapsed"],
            }
            rows.append(row)
            print(
                f"  p{pno+1}: sim A={row['tl_sim']:.3f} B={row['vlm_sim']:.3f} | "
                f"bag A={row['tl_bag']:.3f} B={row['vlm_bag']:.3f} | "
                f"order A={row['tl_order']} B={row['vlm_order']} | "
                f"formula B={row['vlm_formula'][0]}($)/{row['vlm_formula'][1]}(宏) "
                f"A={row['tl_formula'][0]}/${row['tl_formula'][1]}宏 "
                f"truth数学span={row['math_spans']} | "
                f"表 B={row['vlm_tables']} 快照A={row['tl_tab_snap']} "
                f"pdf检出={row['pdf_tables']} | 截断{row['vlm_trunc']} "
                f"{row['vlm_elapsed']:.1f}s",
                flush=True,
            )
            # 附录样本：首个公式密集页取 3 个公式样例供人工目检
            if row["math_spans"] >= 8 and len(samples) < 6:
                fx = re.findall(r"\\\[?.*?\\\]?", vlm_md, re.S)[:3]
                if fx:
                    samples.append(f"### {name} p{pno+1}\n" + "\n\n".join(f"> {s[:200]}" for s in fx))
        doc.close()
        print(f"  textlayer 提取耗时 {tl_elapsed:.1f}s（含快照/redact）", flush=True)

    # 报告
    def avg(k, rows_=rows):
        vals = [r[k] for r in rows_ if r[k] is not None]
        return sum(vals) / max(len(vals), 1)

    def fmtnum(v, fmt="{:.3f}"):
        return fmt.format(v) if isinstance(v, (int, float)) else "—"

    lines = [
        "# 文本层提取 vs PaddleOCR-VL 整页结构化解析：A/B 对比报告",
        "",
        f"- 日期：2026-09-14 ｜ 模型：{ocr_cfg.get('model')} ｜ 脚本：`backend/tools/ab_textlayer_vs_vlm_parse.py`",
        f"- 样本：{len(PAPERS)} 篇真实论文 × 共 {len(rows)} 页（含双栏/单栏、图表公式密集页）",
        "- 真值：PDF 内嵌字符流（get_text，y 序）；sim=NFKC 去空白 SequenceMatcher（与"
        " docs/archive/文本层vsOCR对比.md 同口径）；bag=顺序无关字符 F1（内容完整度）；"
        "order=双栏页「左块先于右块」配对正确率（1.0=列序正确，~0.5=y 带交错，"
        "单栏页不适用）。",
        "- Route A=textlayer 现行生产管线（pymupdf4llm classic + 全部启发式，v18）；"
        "Route B=PaddleOCR-VL「OCR:」整页结构化解析（与扫描页同通道；截断时按栏对半重发）。",
        "",
        "| 论文 | 页 | 真值字符 | sim A | sim B | bag A | bag B | B长度比 | order A | order B | 数学span | 公式$ B | 公式$ A | 表B | 表快照A | pdf表 | 截断 | B耗时 |",
        "|------|----|---------|-------|-------|-------|-------|--------|---------|---------|---------|---------|---------|-----|---------|------|------|-------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['paper']} | {r['page']} | {r['truth_chars']} "
            f"| {r['tl_sim']:.3f} | {r['vlm_sim']:.3f} "
            f"| {r['tl_bag']:.3f} | {r['vlm_bag']:.3f} | {r['vlm_len']:.2f} "
            f"| {fmtnum(r['tl_order'])} | {fmtnum(r['vlm_order'])} "
            f"| {r['math_spans']} | {r['vlm_formula'][0]} | {r['tl_formula'][0]} "
            f"| {r['vlm_tables']} | {r['tl_tab_snap']} | {r['pdf_tables']} "
            f"| {r['vlm_trunc']} | {r['vlm_elapsed']:.1f}s |"
        )
    n2col = [r for r in rows if r["tl_order"] is not None]
    lines += [
        "",
        "## 汇总",
        "",
        f"- 顺序敏感相似度：A 均 **{avg('tl_sim'):.4f}** vs B 均 **{avg('vlm_sim'):.4f}**"
        "（注意：该指标在双栏页惩罚列序正确的输出，真值本身 y 序交错）",
        f"- 内容完整度 bag F1：A 均 **{avg('tl_bag'):.4f}** vs B 均 **{avg('vlm_bag'):.4f}**",
        f"- 双栏阅读顺序得分（{len(n2col)} 个双栏页）：A 均 "
        f"**{avg('tl_order', n2col):.3f}** vs B 均 **{avg('vlm_order', n2col):.3f}**",
        f"- 公式：B 共产出 {sum(r['vlm_formula'][0] for r in rows)} 个 $..$ 段 / "
        f"{sum(r['vlm_formula'][1] for r in rows)} 种 LaTeX 宏；A 共 "
        f"{sum(r['tl_formula'][0] for r in rows)} 个 $..$ 段（真值数学字体 span 共 "
        f"{sum(r['math_spans'] for r in rows)}）",
        f"- 表格：B 共 {sum(r['vlm_tables'] for r in rows)} 个表格标记 vs "
        f"A 表格快照 {sum(r['tl_tab_snap'] for r in rows)} 张（pdf find_tables 检出 "
        f"{sum(r['pdf_tables'] for r in rows)}）",
        f"- 截断：{sum(1 for r in rows if r['vlm_trunc'])} 页触发对半重发"
        f"（共 {sum(r['vlm_trunc'] for r in rows)} 次）",
        f"- B 平均单页耗时 {avg('vlm_elapsed'):.1f}s",
    ]
    if samples:
        lines += ["", "## 公式样例（人工目检）", "", *samples]
    DOCS.mkdir(exist_ok=True)
    out = DOCS / "VLM结构化解析对比.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("REPORT:", out, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
