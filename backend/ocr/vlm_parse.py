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
  区域内文字已由快照"原模原样"承载，VLM 不再重复识别/丢弃）；
  T9.2 起遮罩扩展到版面模型 abandon 区域（版权/页眉/venue 佐料
  不进解析视野），truth 同步扣除，输出段落再按区域文本兜底剔除。

防幻觉兜底（交叉校验降级，T3；查全率/精确率双阈值，T10 反馈 5）与
快照插回（T2）在 parse_page_verified。
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
    _md_norm,
    _snapshot_figures,
    apply_font_evidence,
    collect_font_evidence,
    extract_page_md,
    MIN_TEXT_CHARS,
)
from cache.file_cache import ocr_key, read_cache, write_cache

# 双栏判定的通栏块阈值：与 textlayer._column_reading_order 同口径
_COL_WIDE = 0.55

# ── 版面模型区域消费（阶段12-T9.2）────────────────────────────────
# DocLayout-YOLO 区域按 label 分类四用：
#   table/figure → _figure_regions 额外候选（无框表快照补位）；
#   abandon     → 送 VLM 前遮罩 + truth 扣除 + 输出段落剔除（三用同区域）；
#   title       → 输出段落标题提升（页 0 最大 title→#，编号深度定级）；
#   plain text  → _column_reading_order 栏判定的区域级信号（整栏一个粗
#                 粒度文本块的页，块级左右计数判不出双栏，T10 反馈修复）。
# 坐标全部为 PDF 点（layout_worker.py 已除回渲染倍率）。
_FIG_LABELS = {"figure", "table"}
_ABANDON_LABEL = "abandon"
_TITLE_LABEL = "title"
_TEXT_LABEL = "plain text"
_CAPTION_LABELS = {"figure_caption", "table_caption"}

# 标题编号前缀："4"/"4.1"/"3.2.1"——点分段数决定层级（学界通用约定，
# 非个案特调）：单段号 → ##、两段 → ###、三段 → ####；页 0 最大 title
# 单独判 #（文档标题）；无编号（"Abstract"/"C. GraphRAG"）→ ##。
_TITLE_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)[\s.:)]?")


def _split_layout_regions(regions: list | None) -> dict:
    """版面区域按 label 分类。返回 {fig: [Rect], table: [Rect],
    abandon: [Rect], title: [{rect, conf}], text: [Rect]}——畸形 bbox/
    空区域丢弃。"""
    out = {"fig": [], "table": [], "abandon": [], "title": [], "text": [], "caption": []}
    for reg in regions or []:
        label = reg.get("label")
        bbox = reg.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            r = pymupdf.Rect(bbox)
        except Exception:
            continue
        if r.is_empty:
            continue
        if label in _FIG_LABELS:
            out["fig"].append(r)
            if label == "table":
                out["table"].append(r)
        elif label == _ABANDON_LABEL:
            out["abandon"].append(r)
        elif label == _TITLE_LABEL:
            out["title"].append({"rect": r, "conf": float(reg.get("conf") or 0.0)})
        elif label == _TEXT_LABEL:
            out["text"].append(r)
        elif label in _CAPTION_LABELS:
            out["caption"].append(r)
    return out


def _title_level(raw_text: str, is_doc_title: bool) -> int:
    """编号深度定级（通用约定）+ 页 0 最大 title → 文档标题级。"""
    if is_doc_title:
        return 1
    m = _TITLE_NUM.match(raw_text or "")
    if not m:
        return 2
    return min(len(m.group(1).split(".")) + 1, 4)


def _abandon_hit(para_norm: str, reg_norm: str) -> bool:
    """段落是否命中 abandon 区域文本。短区域（页码"2"）要求全等——
    前缀匹配会误杀所有以该字符开头的正文段；长区域用首窗口前缀互含
    + 段落全包含（VLM 输出把区域拆成多段时的截断片段）三通道。"""
    if not para_norm or not reg_norm:
        return False
    if para_norm == reg_norm:
        return True
    if len(reg_norm) < 8:
        return False
    if para_norm.startswith(reg_norm[:20]) or reg_norm.startswith(para_norm[:20]):
        return True
    # 段落 norm 完整出现在区域文本内（≥12 字符防短串误中）
    return len(para_norm) >= 12 and para_norm in reg_norm


def _drop_abandon_paragraphs(md: str, abandon_norms: list[str]) -> str:
    """剔除命中 abandon 区域文本的输出段落（T9.2 第三用）。

    遮罩与 truth 扣除之后仍可能有残留（遮罩边缘切半行、VLM 对遮罩边缘
    的幻觉补全、textlayer 降级路径的页眉页脚），这里按区域文本兜底剔除。
    纯函数幂等；无区域时原样返回。"""
    if not md or not abandon_norms:
        return md
    parts = re.split(r"(\n\s*\n)", md)
    out: list[str] = []
    for para in parts:
        if re.fullmatch(r"\n\s*\n", para or ""):
            out.append(para)
            continue
        body = (para or "").strip()
        if not body:
            out.append(para)
            continue
        n = _md_norm(body)
        if n and any(_abandon_hit(n, a) for a in abandon_norms):
            out.append("")  # 剔除（保留分隔符占位，join 前收敛空段）
            continue
        out.append(body)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _norm_split(body: str, tnorm: str) -> tuple[str, str] | None:
    """按归一化前缀把 body 切成 (标题原文, 余文)；对不上返回 None。

    逐字符对齐（_md_norm 逐字符 0/1 输出）：VLM 的空白/标点差异不影响
    对齐，字母数字逐位必须相等（措辞漂移即放弃，宁可不拆不拆错）。"""
    ti = 0
    for i in range(len(body) + 1):
        if ti >= len(tnorm):
            return body[:i].rstrip(), body[i:].lstrip()
        if i >= len(body):
            return None
        c = _md_norm(body[i])
        if not c:
            continue
        if tnorm[ti] != c:
            return None
        ti += 1
    return None


def _promote_titles(md: str, layout_titles: list) -> str:
    """版面 title 区域 → 输出段落标题提升（T9.2）+ run-in 拆分（T9.5）。

    layout_titles 元素为 (norm, raw, level)。匹配纪律：
    - 整段即标题（前缀互含 + 长度约束，apply_font_evidence 同款）→ 加 # 前缀；
    - **run-in 拆分**：VLM 常把标题行与后续正文粘成一段（FG-RAG p1 实测
      "Abstract Retrieval-Augmented..."）——段落归一化前缀与标题区域文本
      逐字符对齐（tnorm ≥6 字符）时，拆成「标题段 + 余文段」，几何证据
      保驾护航（模型在页面上真的看到了这行标题），非文本猜测；
    - 已是标题的段落跳过（幂等）；对齐失败原样返回。"""
    if not md or not layout_titles:
        return md
    parts = re.split(r"(\n\s*\n)", md)
    out: list[str] = []
    for para in parts:
        if re.fullmatch(r"\n\s*\n", para or ""):
            out.append(para)
            continue
        body = (para or "").strip()
        if not body:
            out.append(para)
            continue
        if not body.startswith("#"):
            n = _md_norm(body)
            if n:
                for tnorm, traw, level in layout_titles:
                    if not tnorm:
                        continue
                    if tnorm.startswith(n[:24]) or (
                        n.startswith(tnorm[:24]) and len(n) <= len(tnorm) + 20
                    ):
                        body = "#" * level + " " + body
                        break
                    if (
                        len(n) > len(tnorm) + 20
                        and len(tnorm) >= 6
                        and n.startswith(tnorm[:24])
                    ):
                        split = _norm_split(body, tnorm)
                        if split and split[1]:
                            head, rest = split
                            out.extend(
                                ["#" * level + " " + head, "\n\n", rest]
                            )
                            body = None
                            break
        out.append(body if body is not None else "")
    return "".join(out)

# 原始解析结果的缓存伪模型名（cache/file_cache.ocr_key 的 model 位）。
# 独立于 textlayer / 视觉 OCR 缓存；仅当解析协议（提示/重试策略）变化
# 时才 bump 版本后缀。
# v2（阶段12-T9.2）：送 VLM 前遮罩扩展到 abandon 区域（版权/页眉不再
# 进入解析视野），旧缓存是未遮罩产物，需整体失效重解析。
# v3（阶段12-T10 反馈 2）：快照区域受版面模型 plain text 区域仲裁——
# prompt 示例框不再遮罩/扣除，旧缓存缺这部分内容且 bag 失败后原始缓存
# 不会被重取（只在通过校验时落盘），必须整体失效重解析。
VLM_PARSE_MODEL = "vlm-parse-v3"

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


# 校验判定（T10 反馈 5 校准，2026-09-15 SubgraphRAG 29 页实测）：F1 单阈值
# 会误杀数学页——LaTeX 命令字母（\mathbb{P} → "mathbbP"）膨胀 VLM 侧字符
# 数，真值侧的 ℙ 却被字母数字口径剥空，实测公式页 F1 0.895 被拒、其查全率
# 0.9994（内容零丢失）纯口径稀释。改为双阈值：
#   查全率 ≥ _VERIFY_RECALL_MIN —— 真值侧内容覆盖（防丢内容的本质）
#   精确率 ≥ _VERIFY_PRECISION_MIN —— VLM 侧多余字符（防幻觉）
# 实测分布：25 个通过页 recall[0.912,1]×precision[0.927,1] 全部仍过；
# 误杀公式页 0.9994/0.811 救回；真欠输出（recall 0.80/0.55）与退化输出
# （只回页码 recall 0.0004）仍正确拒绝。
_VERIFY_RECALL_MIN = 0.90
_VERIFY_PRECISION_MIN = 0.75


def verify_recall_precision(vlm_md: str, truth: str) -> tuple[float, float]:
    """交叉校验查全率/精确率（字母数字+CJK 口径，同 verify_bag）。"""
    a = _VERIFY_STRIP.sub("", norm(vlm_md).lower())
    b = _VERIFY_STRIP.sub("", norm(truth).lower())
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    return inter / max(len(b), 1), inter / max(len(a), 1)


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


def _prepare(file_path: str, pno: int, image_dir: str | None, layout_regions=None) -> dict:
    """页级准备（阻塞，调用方放线程）：快照 + truth + 几何信息。

    - 快照复用 textlayer._snapshot_figures（含 sidecar、v5 表格=快照
      决策）——VLM 路线的遮罩、truth 扣除与文本层降级路径必须同区域
      才自洽；
    - 版面模型区域（T9.2）：table/figure 并入快照候选（无框表补位，
      DALK p9 回归用例）；abandon 区域三用数据在此采集（区域矩形 +
      区域文本 norm——遮罩渲染时由调用方拼接 regions+abandon）；
      title 区域文本提取 + 编号深度定级（页 0 最大 title → #）；
    - 快照必须先于遮罩渲染：_figure_regions 依赖 cluster_drawings，
      先画白底矩形会把遮罩本身当成绘图簇；
    - truth = 文本层字符流扣除快照区域与 abandon 区域内部文本（图注
      豁免，与 redact 同判定）——交叉校验（T3）的比对基准，快照承载
      与佐料区域的内容都不要求 VLM 复述；
    - scanned 分类与 extract_pages 有效性判定同口径：短文本 + 无快照
      = 扫描页，维持既有视觉通道。
    """
    doc = pymupdf.open(file_path)
    try:
        if image_dir:
            # 目录必须存在：pix.save 对不存在的目录抛错（extract_pages
            # 同款兜底，曾把快照管线静默打空的实测踩坑）
            os.makedirs(image_dir, exist_ok=True)
        lr = _split_layout_regions(layout_regions)
        refs, snap_regions = (
            _snapshot_figures(
                doc,
                pno,
                image_dir,
                extra_regions=lr["fig"],
                table_regions=lr["table"],
                text_regions=lr["text"],
                caption_regions=lr["caption"],
            )
            if image_dir
            else ([], [])
        )
        page = doc[pno]
        inner = _figure_inner_text_rects(page, snap_regions) if snap_regions else []
        # abandon 区域文本（输出剔除的匹配基准）
        abandon_norms = []
        for r in lr["abandon"]:
            a = _md_norm(page.get_text("text", clip=r))
            if a:
                abandon_norms.append(a)
        # title 区域 → (norm, raw, level)；页 0 最大 title 判文档标题级。
        # raw 保留区域原文（run-in 拆分需要真实字符做切分点）
        layout_titles: list[tuple[str, str, int]] = []
        if lr["title"]:
            largest = (
                max(lr["title"], key=lambda t: t["rect"].get_area())
                if pno == 0
                else None
            )
            for t in lr["title"]:
                raw = page.get_text("text", clip=t["rect"]).strip()
                tn = _md_norm(raw)
                if not tn:
                    continue
                layout_titles.append((tn, raw, _title_level(raw, t is largest)))
        parts = []
        for b in page.get_text("blocks"):
            r = pymupdf.Rect(b[:4])
            # inner 里的矩形就是被剔除块自身的 bbox（_figure_inner_text_rects
            # 按块主体 60% 落在区域内筛选），重叠 ≥60% 即同一块；abandon
            # 区域同口径扣除（佐料文本不进 truth，VLM 不复述不算丢内容）
            if any(
                (r & ir).get_area() >= 0.6 * max(r.get_area(), 1.0) for ir in inner
            ):
                continue
            if any(
                (r & ar).get_area() >= 0.6 * max(r.get_area(), 1.0)
                for ar in lr["abandon"]
            ):
                continue
            parts.append(b[4] if len(b) > 4 else "")
        truth = "".join(parts)
        raw_text_len = len(page.get_text("text").strip())
        # T9.4 兜底证据：版面信号缺席（运行时缺失/worker 失败/工具直调）
        # 才采集字号证据——layout_ok=True 时版面模型的「本页无标题」是
        # 可信判定，字号规则不得越权覆盖（fb182b5 冻结版，不新增规则）
        font_evidence = None
        if layout_regions is None:
            try:
                font_evidence = collect_font_evidence(page, is_first_page=(pno == 0))
            except Exception as e:  # noqa: BLE001 —— 证据采集失败=无兜底，不阻断
                print(f"[vlm] p{pno + 1}: 字号证据采集失败（跳过兜底）: {e}")
        return {
            "scanned": raw_text_len < MIN_TEXT_CHARS and not refs,
            "refs": refs,
            "regions": snap_regions,
            "abandon_rects": lr["abandon"],
            "abandon_norms": abandon_norms,
            "layout_titles": layout_titles,
            "text_regions": lr["text"],
            "layout_ok": layout_regions is not None,
            "font_evidence": font_evidence,
            "raw_blocks": page.get_text("blocks"),
            "truth": truth,
        }
    finally:
        doc.close()


def _finalize_md(prep: dict, md: str) -> str:
    """快照引用插回 + 双栏顺序几何兜底 + 版面区域消费（T9.2）。

    - 插回：几何配对用 PDF raw_blocks 的 caption 真实坐标（与 textlayer
      同款 _insert_figures），在 VLM 输出里按 caption 文本定位插入点——
      VLM 看得见快照区域外的 caption（遮罩只盖区域内），锚点天然存在；
    - 顺序兜底：VLM 偶发整栏交换（A/B 实测 DALK p2 反例：内容完整 bag
      0.99、双栏整栏互换）。_column_reading_order 自带高置信门槛（全部
      段落可定位坐标 + 左右各 ≥3 窄块 + 干净分栏沟），不满足即原样返回
      ——兜底只会纠正、不会搅乱；
    - abandon 段落剔除：遮罩/truth 扣除后的残留兜底（区域文本匹配）；
    - title 提升编号定级；版面信号缺席（layout_ok=False，T9.4）时落回
      字号证据（fb182b5 冻结版：粗体+字号判标题/首页小字印刷块剔除）。"""
    if prep["refs"]:
        md = _insert_figures(
            md, prep["refs"], snap_regions=prep["regions"], raw_blocks=prep["raw_blocks"]
        )
    md = _column_reading_order(
        prep["raw_blocks"], md, col_rects=prep.get("text_regions")
    )
    md = _drop_abandon_paragraphs(md, prep.get("abandon_norms") or [])
    if prep.get("layout_titles"):
        md = _promote_titles(md, prep["layout_titles"])
    elif not prep.get("layout_ok"):
        md = apply_font_evidence(md, prep.get("font_evidence") or {})
    return md


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
    recall_threshold: float = 0.90,
    sem: asyncio.Semaphore | None = None,
    layout=None,
) -> dict:
    """数字页混合主路线（T1+T2+T3+T9.2）：快照 → 遮罩解析 → 插回 → 交叉校验降级。

    layout：ocr.layout_model.LayoutProvider（T9.2 版面模型信号源）。
    None（回归工具/单页直调）或该页无产出（运行时缺失/失败/缓存空）时，
    等价于无版面信号的既有行为——快照不含无框表补位、无遮罩扩展、
    无标题提升，管线照常完成。

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
    layout_regions = (
        await layout.regions(pno) if layout is not None else None
    )
    prep = await asyncio.to_thread(_prepare, file_path, pno, image_dir, layout_regions)
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
                    file_path,
                    pno,
                    config,
                    mask_regions=prep["regions"] + prep["abandon_rects"],
                )
            md_raw, trunc = res["md"], res["trunc"]
            fresh = True
        except Exception as e:
            print(f"[vlm] p{pno + 1}: 结构化解析失败，回退文本层: {e}")

    truth_n = norm(prep["truth"])
    # 退化输出一次性重试（hosted 模型偶发只回页码，实测 bag≈0.002）：
    # 输出字母数字 <10% 真值且真值足够长 → 重打一次——退化多为服务端瞬态，
    # 非输入确定性问题（SubgraphRAG p7 实测整页只返回 "7"）
    if fresh and md_raw and truth_n:
        tn = _VERIFY_STRIP.sub("", truth_n.lower())
        vn = _VERIFY_STRIP.sub("", norm(md_raw).lower())
        if len(tn) >= 200 and len(vn) < 0.1 * len(tn):
            print(f"[vlm] p{pno + 1}: 退化输出（{len(vn)}/{len(tn)} 字符），重试一次")
            try:
                async with (sem if sem is not None else nullcontext()):
                    res = await parse_page(
                        file_path,
                        pno,
                        config,
                        mask_regions=prep["regions"] + prep["abandon_rects"],
                    )
                if res["md"]:
                    md_raw, trunc = res["md"], res["trunc"]
            except Exception as e:  # noqa: BLE001 —— 重试失败保留原输出进校验
                print(f"[vlm] p{pno + 1}: 退化重试失败（保留原输出）: {e}")

    bag = verify_bag(md_raw, prep["truth"]) if md_raw and truth_n else 0.0
    rec = prec = 0.0
    if md_raw and truth_n:
        rec, prec = verify_recall_precision(md_raw, prep["truth"])
    # 短 truth（近空页/纯图页）无从校验，接受 VLM 输出（无内容可损失）；
    # 正常页双阈值：查全率防丢内容、精确率防幻觉（见 _VERIFY_RECALL_MIN 注释）
    accept = bool(md_raw) and (
        len(_VERIFY_STRIP.sub("", truth_n.lower())) < 60
        or (rec >= recall_threshold and prec >= _VERIFY_PRECISION_MIN)
    )
    # 原始解析结果只在通过校验时落缓存：低质输出（实测 hosted 模型对
    # 密集参考文献页偶发只回页码，bag≈0.002）不进缓存，下次运行自然
    # 重试该页——跨运行自愈，不额外花费重试请求
    if fresh and md_raw and accept:
        write_cache(cache_dir, raw_key, {"md": md_raw, "trunc": trunc})
    if accept:
        md = await asyncio.to_thread(_finalize_md, prep, md_raw)
        return {
            "md": md,
            "source": "vlm",
            "bag": round(bag, 4),
            "rec": round(rec, 4),
            "prec": round(prec, 4),
            "trunc": trunc,
        }
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
            "rec": round(rec, 4),
            "prec": round(prec, 4),
            "trunc": trunc,
            "fallback_reason": "fallback_error",
        }
    reason = "empty" if not md_raw else ("no_truth" if not truth_n else "bag")
    # textlayer 降级输出同样消费 abandon 区域（其文本噪音规则追不上
    # 出版商套话形态，模型按区域判定与措辞无关）
    md = _drop_abandon_paragraphs(md, prep.get("abandon_norms") or [])
    return {
        "md": md,
        "source": "textlayer",
        "bag": round(bag, 4),
        "rec": round(rec, 4),
        "prec": round(prec, 4),
        "trunc": trunc,
        "fallback_reason": reason,
    }
