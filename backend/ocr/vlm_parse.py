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
import time
import unicodedata
import re
from collections import Counter

import httpx
import pymupdf

from ocr import siliconflow

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
