"""块级公式识别：裁剪块区域 → 视觉模型 → LaTeX。

复用 siliconflow._ocr_image 通道（PaddleOCR-VL 已实测调优：max_tokens 8192、
temperature 0.01），裁剪坐标来自 pipeline.layout 的多段 bbox。识别结果按
(pdf_hash, page, bbox, model) 内容寻址缓存——同一公式块重复点按/重开文档
幂等零成本；流水线侧在公式块标记处回填缓存（processor），重跑不丢。

缓存三件套（段融合事件的教训）：写侧拒绝空结果；命中侧校验非空且含数学
记号（防跑题长文污染）；FORMULA_VERSION 随提示词/渲染参数升级整体失效。
行内公式混排段不在目标内——对照模式用原版模式兜底（D6 决策）。
"""

import asyncio
import hashlib
import re

import pymupdf

from cache.file_cache import read_cache, write_cache

# v2: 强化提示词（下标质量更好，实测 \mu_k vs \mu k） + 裸 LaTeX 包裹后处理
# （PaddleOCR-VL 实测从不输出 $ 定界符，结果需包裹后 KaTeX 才能渲染）
FORMULA_VERSION = "v2"

FORMULA_PROMPT = (
    "You are a math OCR engine. Transcribe EVERY mathematical expression in this "
    "image as LaTeX source code. Wrap inline math in $...$ and display math in "
    "$$...$$. Example: a Gaussian N(x; mu_k, Sigma_k) must become "
    "$\\mathcal{N}(x;\\mu_k,\\Sigma_k)$. Never write math as plain text. "
    "Non-math words stay as plain text. Output markdown only."
)

# 渲染倍率：与全文 OCR 的 RENDER_SCALE 同级，保证小字号上下标可辨认
_RENDER_SCALE = 2.5

# 裸 LaTeX 片段：\宏（含可选的 _下标 / ^上标）。裸脚本体不含逗号（否则
# "N(x; \mu_k, \Sigma_k)" 的 \mu_k, 会把分隔逗号卷进公式），逗号只在
# 花括号体内合法（\mu_{k,n} 多下标形态）
_BARE_LATEX = re.compile(
    r"\\[a-zA-Z]+"
    r"(?:_(?:\{[A-Za-z0-9,+\-]{1,12}\}|[A-Za-z0-9+\-]))?"
    r"(?:\^(?:\{[A-Za-z0-9,+\-]{1,12}\}|[A-Za-z0-9+\-]))?"
)


def wrap_bare_latex(text: str) -> str:
    """识别结果无任何 $ 定界时，把裸 LaTeX 宏片段包进 $...$。

    实测：PaddleOCR-VL 即便按提示词要求也**从不输出 $ 定界**，
    返回形如 "N(x; \\mu_k, \\Sigma_k)"——不包裹则 KaTeX 无法渲染，宏原样
    露在正文里。只处理无 $ 的结果（有定界说明模型这次听话了，不动）；
    普通单词不含 \\ 不会误包，残渣片段（\\x）包了也只影响残渣本身。
    """
    if not text or "$" in text:
        return text
    return _BARE_LATEX.sub(lambda m: f"${m.group(0)}$", text)


def _valid_latex(text: str) -> bool:
    """命中侧合法性：非空、长度合理、含数学记号（$ 定界 / LaTeX 命令 / 等式）。"""
    if not text or len(text) > 8000:
        return False
    return any(tok in text for tok in ("$", "\\", "="))


def formula_cache_key(pdf_hash: str, page: int, bbox: list, model: str) -> str:
    h = hashlib.sha1()
    for part in (
        "fx",
        FORMULA_VERSION,
        pdf_hash,
        str(int(page)),
        model or "",
        ",".join(str(round(float(v), 1)) for v in bbox),
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load_cached_formula(
    pdf_hash: str, page: int, bbox: list, ocr_cfg: dict, cache_dir: str
) -> str | None:
    """流水线回填用：该公式块识别过则返回 LaTeX，否则 None（绝不触发 API）。"""
    model = (ocr_cfg or {}).get("model", "")
    key = formula_cache_key(pdf_hash, page, bbox, model)
    hit = read_cache(cache_dir, key)
    if hit and _valid_latex(hit.get("latex") or ""):
        return hit["latex"]
    return None


def _render_block_png(file_path: str, page: int, bbox: list) -> bytes:
    """按 bbox 裁剪页面并渲染为 PNG（坐标越界部分裁剪到页矩形）。"""
    doc = pymupdf.open(file_path)
    try:
        p = doc[page]
        clip = pymupdf.Rect(bbox) & p.rect
        if clip.is_empty:
            raise RuntimeError("公式区域为空（bbox 越界）")
        pix = p.get_pixmap(
            matrix=pymupdf.Matrix(_RENDER_SCALE, _RENDER_SCALE), clip=clip, alpha=False
        )
        return pix.tobytes("png")
    finally:
        doc.close()


async def recognize_block_formula(
    file_path: str,
    pdf_hash: str,
    page: int,
    bbox: list,
    ocr_cfg: dict,
    cache_dir: str,
) -> dict:
    """按需识别：返回 {latex, cached}；失败抛异常（端点转错误响应）。"""
    ocr_cfg = ocr_cfg or {}
    model = ocr_cfg.get("model", "PaddlePaddle/PaddleOCR-VL-1.5")
    bbox = [round(float(v), 1) for v in bbox]
    key = formula_cache_key(pdf_hash, page, bbox, model)
    hit = read_cache(cache_dir, key)
    if hit and _valid_latex(hit.get("latex") or ""):
        return {"latex": hit["latex"], "cached": True}

    from ocr.siliconflow import _ocr_image

    png = await asyncio.to_thread(_render_block_png, file_path, page, bbox)
    text = (
        await _ocr_image(
            png,
            ocr_cfg.get("api_url", ""),
            ocr_cfg.get("api_key", ""),
            model,
            FORMULA_PROMPT,
        )
        or ""
    ).strip()
    if not _valid_latex(text):
        raise RuntimeError("识别结果为空或不含数学记号")
    text = wrap_bare_latex(text)
    write_cache(cache_dir, key, {"latex": text})
    return {"latex": text, "cached": False}
