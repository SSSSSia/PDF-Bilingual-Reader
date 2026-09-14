"""PaddleOCR-VL 整页结构化解析：数字页提取主路线（阶段12-T1）。

决策依据（docs/VLM结构化解析对比.md，2026-09-14 A/B 实测，4 篇论文 12 页）：
现行 textlayer（pymupdf4llm classic + 启发式，v18）公式 0 段 LaTeX、复杂
版式页 bag 0.96~0.98 触及天花板；PaddleOCR-VL「OCR:」整页解析公式 40 段
定界 LaTeX（`\\(...\\)` 与前端 preprocessMath→KaTeX 链路天然兼容）、复杂
版式页 0.99+、免费档零成本——与扫描页视觉通道同模型同提示，行为统一。

本模块负责「一页 → 结构化 markdown」：
- 调用协议与 siliconflow._ocr_image 完全一致（同 payload），但保留
  finish_reason 以驱动截断重发——密集页可能超出 max_tokens 8192
  （总上下文 16384），命中时按栏对半裁剪重发拼接（双栏页左右半、
  单栏页上下半；A/B 实测 0/12 页触发，作为保险存在）；
- mask_regions：送 VLM 前对图表区域打白底遮罩（阶段12-T2 接入
  textlayer._snapshot_figures 的最终区域——表格维持 v5 快照决策，
  区域内文字已由快照"原模原样"承载，VLM 不再重复识别/丢弃）。

防幻觉兜底（交叉校验降级，T3）与快照插回（T2）在 parse_page_verified。
缓存：原始解析结果存 ocr_key(pdf_hash, page, VLM_PARSE_MODEL)——T5 调参
与回归重放时直接命中，免重打 API。
"""
import asyncio
import base64
import os
import time
import unicodedata
import re
from collections import Counter
from contextlib import nullcontext

import httpx
import pymupdf

from ocr import siliconflow
from ocr.textlayer import (
    _column_reading_order,
    _figure_inner_text_rects,
    _insert_figures,
    _snapshot_figures,
    extract_page_md,
    MIN_TEXT_CHARS,
)
from cache.file_cache import ocr_key, read_cache, write_cache

# 双栏判定的通栏块阈值：与 textlayer._column_reading_order 同口径
_COL_WIDE = 0.55

# 原始解析结果的缓存伪模型名（cache/file_cache.ocr_key 的 model 位）。
# 独立于 textlayer / 视觉 OCR 缓存；仅当解析协议（提示/重试策略）变化
# 时才 bump 版本后缀。
VLM_PARSE_MODEL = "vlm-parse-v1"

# 并发上限：与扫描页视觉通道共用免费档保守值
CONCURRENCY = siliconflow.OCR_CONCURRENCY


def norm(s: str) -> str:
    """归一化（A/B 工具同口径）：NFKC + 去全部空白。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def bag_f1(a: str, b: str) -> float:
    """顺序无关字符 F1（内容完整度）——交叉校验用，剥离阅读顺序影响。"""
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    return 2 * inter / max(len(a) + len(b), 1)


# 交叉校验专用剥离：只留字母/数字/CJK（T8 校准，2026-09-14 实测）。
# 原因：真值数学是 Unicode 碎屑（NFKC 后仍含 ∑≤∈ 等符号），VLM 是
# LaTeX 语法（\ { } ^ _），逐字符多重集在这两类表示间系统性稀释——
# FG-RAG p3 数学页 0.8985（假性不达标）→ 字母数字口径 0.9008；而真实
# 内容丢失（DALK p9 无框表被 VLM 丢弃）0.688→0.696 仍远低于阈值，
# 防幻觉能力不受影响。中文文档靠 CJK 保留参与校验。
_VERIFY_STRIP = re.compile(r"[^a-z0-9\u4e00-\u9fff]")


def verify_bag(vlm_md: str, truth: str) -> float:
    """交叉校验 bag：norm → 小写 → 剥离语法/符号字符后的多重集 F1。"""
    a = _VERIFY_STRIP.sub("", norm(vlm_md).lower())
    b = _VERIFY_STRIP.sub("", norm(truth).lower())
    return bag_f1(a, b)


def _render_png(
    file_path: str,
    pno: int,
    clip: tuple | None = None,
    mask_regions: list | None = None,
) -> bytes:
    """渲染页面为 PNG（2.5x 与视觉 OCR 一致）。mask_regions 非空时先在
    页面上画白底矩形——必须用快照的最终区域（光栅+矢量簇+表格合并后），
    实验已证 get_image_rects 遮不住矢量图标签（A/B 补充实验）。"""
    doc = pymupdf.open(file_path)
    try:
        page = doc[pno]
        for reg in mask_regions or []:
            page.draw_rect(pymupdf.Rect(reg), color=None, fill=(1, 1, 1))
        r = pymupdf.Rect(clip) & page.rect if clip else page.rect
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(siliconflow.RENDER_SCALE, siliconflow.RENDER_SCALE),
            clip=r,
            alpha=False,
        )
        return pix.tobytes("png")
    finally:
        doc.close()


def two_column(page) -> bool:
    """几何双栏判定（截断对半重发的切向选择 + T5 顺序兜底的触发条件）。
    与 textlayer._column_reading_order / A/B 脚本同口径：左右各 ≥3 个窄块
    （宽 ≤0.55 页宽、高超 200pt 的穿栏块不参与）且存在干净分栏沟。"""
    w = page.rect.width
    lefts, rights = [], []
    for b in page.get_text("blocks"):
        r = pymupdf.Rect(b[:4])
        if r.width > _COL_WIDE * w or r.height > 200:
            continue
        if r.x1 <= w / 2 + 5:
            lefts.append(r)
        elif r.x0 >= w / 2 - 5:
            rights.append(r)
    if len(lefts) < 3 or len(rights) < 3:
        return False
    return max(r.x1 for r in lefts) <= min(r.x0 for r in rights) + 5


def _page_geometry(file_path: str, pno: int) -> tuple[float, float, bool]:
    """(页宽, 页高, 是否双栏)——截断重发的切向选择用。"""
    doc = pymupdf.open(file_path)
    try:
        page = doc[pno]
        return page.rect.width, page.rect.height, two_column(page)
    finally:
        doc.close()


async def _vlm_call(png: bytes, config: dict) -> tuple[str, str]:
    """单次调用，返回 (text, finish_reason)。payload 与生产扫描页通道
    （siliconflow._ocr_image）完全一致，仅保留 finish_reason 不折叠。"""
    if not config.get("api_key"):
        raise ValueError("OCR API Key 未配置")
    b64 = base64.b64encode(png).decode("ascii")
    payload = {
        "model": config.get("model", siliconflow.DEFAULT_MODEL),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": siliconflow.OCR_PROMPT},
                ],
            }
        ],
        # PaddleOCR-VL 总上下文 16384，max_tokens 顶满会 400（实测），
        # 8192 是安全上限；超出由 finish_reason=length 驱动对半重发。
        "max_tokens": 8192,
        "temperature": 0.01,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{config.get('api_url', 'https://api.siliconflow.cn/v1')}"
            "/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"VLM 请求失败 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
    choice = data["choices"][0]
    return (choice["message"]["content"] or "").strip(), choice.get("finish_reason") or ""


async def parse_page(
    file_path: str,
    pno: int,
    config: dict,
    mask_regions: list | None = None,
) -> dict:
    """整页结构化解析；finish_reason=length 时按栏对半重发拼接。

    返回 {"md": str, "trunc": int, "elapsed": float}。trunc = 整页截断 1 次
    + 半页仍截断次数（A/B 实测 0/12 页，预期常态 0，进 stats 观测漂移）。"""
    t0 = time.perf_counter()
    png = await asyncio.to_thread(_render_png, file_path, pno, None, mask_regions)
    text, reason = await _vlm_call(png, config)
    trunc = 0
    if reason == "length":
        trunc = 1
        w, h, cols = await asyncio.to_thread(_page_geometry, file_path, pno)
        # 双栏页左右对半、单栏页上下对半，中缝外扩 2pt 防切字
        halves = (
            [(0, 0, w / 2 + 2, h), (w / 2 - 2, 0, w, h)]
            if cols
            else [(0, 0, w, h / 2 + 2), (0, h / 2 - 2, w, h)]
        )
        parts = []
        for clip in halves:
            half_png = await asyncio.to_thread(
                _render_png, file_path, pno, clip, mask_regions
            )
            t, r = await _vlm_call(half_png, config)
            parts.append(t)
            if r == "length":
                trunc += 1
        text = "\n\n".join(p for p in parts if p.strip())
    return {"md": text, "trunc": trunc, "elapsed": time.perf_counter() - t0}


# ── 快照/遮罩/truth 协同（阶段12-T2）─────────────────────────────────


def _prepare(file_path: str, pno: int, image_dir: str | None) -> dict:
    """页级准备（阻塞，调用方放线程）：快照 + truth + 几何信息。

    - 快照复用 textlayer._snapshot_figures（含 sidecar、v5 表格=快照
      决策）——VLM 路线的遮罩、truth 扣除与文本层降级路径必须同区域
      才自洽；
    - 快照必须先于遮罩渲染：_figure_regions 依赖 cluster_drawings，
      先画白底矩形会把遮罩本身当成绘图簇；
    - truth = 文本层字符流扣除快照区域内部文本（图注豁免，与 redact
      同判定）——交叉校验（T3）的比对基准，快照承载的内容不要求 VLM
      复述；
    - scanned 分类与 extract_pages 有效性判定同口径：短文本 + 无快照
      = 扫描页，维持既有视觉通道。
    """
    doc = pymupdf.open(file_path)
    try:
        if image_dir:
            # 目录必须存在：pix.save 对不存在的目录抛错（extract_pages
            # 同款兜底，曾把快照管线静默打空的实测踩坑）
            os.makedirs(image_dir, exist_ok=True)
        refs, snap_regions = (
            _snapshot_figures(doc, pno, image_dir) if image_dir else ([], [])
        )
        page = doc[pno]
        inner = _figure_inner_text_rects(page, snap_regions) if snap_regions else []
        parts = []
        for b in page.get_text("blocks"):
            r = pymupdf.Rect(b[:4])
            # inner 里的矩形就是被剔除块自身的 bbox（_figure_inner_text_rects
            # 按块主体 60% 落在区域内筛选），重叠 ≥60% 即同一块
            if any(
                (r & ir).get_area() >= 0.6 * max(r.get_area(), 1.0) for ir in inner
            ):
                continue
            parts.append(b[4] if len(b) > 4 else "")
        truth = "".join(parts)
        raw_text_len = len(page.get_text("text").strip())
        return {
            "scanned": raw_text_len < MIN_TEXT_CHARS and not refs,
            "refs": refs,
            "regions": snap_regions,
            "raw_blocks": page.get_text("blocks"),
            "truth": truth,
        }
    finally:
        doc.close()


def _finalize_md(prep: dict, md: str) -> str:
    """快照引用插回 + 双栏顺序几何兜底（阶段12-T5）。

    - 插回：几何配对用 PDF raw_blocks 的 caption 真实坐标（与 textlayer
      同款 _insert_figures），在 VLM 输出里按 caption 文本定位插入点——
      VLM 看得见快照区域外的 caption（遮罩只盖区域内），锚点天然存在；
    - 顺序兜底：VLM 偶发整栏交换（A/B 实测 DALK p2 反例：内容完整 bag
      0.99、双栏整栏互换）。_column_reading_order 自带高置信门槛（全部
      段落可定位坐标 + 左右各 ≥3 窄块 + 干净分栏沟），不满足即原样返回
      ——兜底只会纠正、不会搅乱。"""
    if prep["refs"]:
        md = _insert_figures(
            md, prep["refs"], snap_regions=prep["regions"], raw_blocks=prep["raw_blocks"]
        )
    return _column_reading_order(prep["raw_blocks"], md)


def _textlayer_fallback(file_path: str, pno: int, image_dir: str | None, prep: dict) -> str:
    """整页回退现行文本层提取（阶段12-T3）。快照复用 prep 的产物，
    不重复区域检测——降级路径与主路线同一份快照，行为自洽。"""
    doc = pymupdf.open(file_path)
    try:
        return extract_page_md(
            doc, file_path, pno, image_dir, prep["refs"], prep["regions"]
        )
    finally:
        doc.close()


async def parse_page_verified(
    file_path: str,
    pno: int,
    pdf_hash: str,
    image_dir: str | None,
    config: dict,
    cache_dir: str,
    bag_threshold: float = 0.90,
    sem: asyncio.Semaphore | None = None,
) -> dict:
    """数字页混合主路线（T1+T2+T3）：快照 → 遮罩解析 → 插回 → 交叉校验降级。

    返回：
      {"scanned": True}                          扫描页，调用方走视觉通道
      {"md", "source": "vlm", "bag", "trunc"}    VLM 主路线通过
      {"md", "source": "textlayer", "bag", "trunc", "fallback_reason"}

    最坏情况 = 现行 textlayer 输出：bag 低于阈值 / 空输出 / API 失败均整页
    回退（防幻觉兜底，任务绝不因 VLM 失败而失败）。truth 过短（近空页/
    纯图页）无从校验，直接接受 VLM 输出——无内容可损失。

    缓存两层：原始解析结果（未插快照引用）存 VLM_PARSE_MODEL 键，T5 调参
    与回归重放免重打 API；最终页产物由调用方按 TEXT_LAYER_MODEL 键存
    （熔断随管线版本走）。

    sem：VLM 调用阶段的并发闸（免费档 RPM 保守值）。必须由调用方在其
    事件循环内创建后传入（asyncio 原语跨 loop 复用会报错）；None = 不限。
    """
    prep = await asyncio.to_thread(_prepare, file_path, pno, image_dir)
    if prep["scanned"]:
        return {"scanned": True}

    raw_key = ocr_key(pdf_hash, pno, VLM_PARSE_MODEL)
    raw = read_cache(cache_dir, raw_key)
    md_raw, trunc = (raw or {}).get("md", ""), (raw or {}).get("trunc", 0)
    fresh = False
    if not md_raw:
        try:
            # nullcontext 支持 async（3.10+）：sem 缺省时不限并发
            async with (sem if sem is not None else nullcontext()):
                res = await parse_page(
                    file_path, pno, config, mask_regions=prep["regions"]
                )
            md_raw, trunc = res["md"], res["trunc"]
            fresh = True
        except Exception as e:
            print(f"[vlm] p{pno + 1}: 结构化解析失败，回退文本层: {e}")

    truth_n = norm(prep["truth"])
    bag = verify_bag(md_raw, prep["truth"]) if md_raw and truth_n else 0.0
    # 短 truth（近空页/纯图页）无从校验，接受 VLM 输出（无内容可损失）
    accept = bool(md_raw) and (
        len(_VERIFY_STRIP.sub("", truth_n.lower())) < 60
        or bag >= bag_threshold
    )
    # 原始解析结果只在通过校验时落缓存：低质输出（实测 hosted 模型对
    # 密集参考文献页偶发只回页码，bag≈0.002）不进缓存，下次运行自然
    # 重试该页——跨运行自愈，不额外花费重试请求
    if fresh and md_raw and accept:
        write_cache(cache_dir, raw_key, {"md": md_raw, "trunc": trunc})
    if accept:
        md = await asyncio.to_thread(_finalize_md, prep, md_raw)
        return {"md": md, "source": "vlm", "bag": round(bag, 4), "trunc": trunc}
    # 降级路径自身也失败（实测：pymupdf4llm 在链接注解损坏的页崩，
    # FG-RAG p6-12）→ 保留未校验的 VLM 输出——好过整页空掉，日志留痕
    try:
        md = await asyncio.to_thread(
            _textlayer_fallback, file_path, pno, image_dir, prep
        )
    except Exception as e:
        print(f"[vlm] p{pno + 1}: textlayer 降级亦失败（{e}），保留未校验 VLM 输出")
        md = (
            await asyncio.to_thread(_finalize_md, prep, md_raw) if md_raw else ""
        )
        return {
            "md": md,
            "source": "vlm",
            "bag": round(bag, 4),
            "trunc": trunc,
            "fallback_reason": "fallback_error",
        }
    reason = "empty" if not md_raw else ("no_truth" if not truth_n else "bag")
    return {
        "md": md,
        "source": "textlayer",
        "bag": round(bag, 4),
        "trunc": trunc,
        "fallback_reason": reason,
    }
