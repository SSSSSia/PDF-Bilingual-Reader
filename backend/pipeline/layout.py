"""块级 bbox 标注。

把流水线最终块（跨页合并后）通过文本前缀匹配映射回 PyMuPDF 原始块坐标，
供前端原版模式在 pdfjs 渲染的页面上按坐标叠加高亮/译文浮层。

设计要点：
- 复用 ocr/textlayer.py 的 _md_norm/_match_block（与 _column_reading_order
  同一机制，含字母级回退——上/下标转换会让数字级匹配必败，作者行实测）；
- 多段匹配：一个逻辑块常由多个物理块拼成
  （断栏续接/跨页合并/作者行多段），锚点命中后继续在「本文本页 + 后两页」
  的原始块里按归一化包含关系消耗后续片段，产出
  block["bboxes"] = [{"page": int, "bbox": [x0,y0,x1,y1]}, ...]；
  bbox 恒等于首段（兼容旧消费方）。消耗标记（used）防止两个逻辑块
  争夺同一原始块；续段最短 8 个归一化字符防页码/单词误吸。
- 匹配不上（公式碎块/图内文字/扫描页 OCR 文本）→ bbox=None / bboxes=[]：
  原版模式的核心优势就是页面本身由 pdfjs 原样渲染，匹配失败只是「不高亮」，
  零内容损失；
- PyMuPDF 坐标系 = top-left 原点 y 向下，旋转 0° 页与 pdfjs viewport 的
  scale 为直接乘法关系（前端 overlay 用 bbox × scale 定位）；
- 每次流水线运行重算（约 10 ms/页，Spike 实测量级），不新增缓存 key。
"""

import re

import pymupdf

from ocr.textlayer import _MD_NORM_ALPHA, _match_block, _md_norm

# 纯图片引用块（图表快照锚点）：渲染层已有原图，无需高亮
_IMG_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")

# 续段消耗的最短归一化长度（防页码/短词误吸）
_MIN_CONT_NORM = 8
# 单块最大分段数（防御性上限，避免病态循环）
_MAX_SEGS = 12


def _norms_of(raw: list) -> tuple[list[str], list[str]]:
    """原始块的数字级/字母级归一化文本（与 textlayer 同规则）。"""
    norms = [_md_norm(b[4]) for b in raw]
    alphas = [_MD_NORM_ALPHA.sub("", (b[4] or "").lower()) for b in raw]
    return norms, alphas


def attach_block_bboxes(file_path: str, pages: list) -> None:
    """为 pages（processor 最终结构）的每个 block 原地标注 bbox/bboxes。

    pages: [{"page": int, "blocks": [{"original": str, ...}]}]
    bboxes 为全部命中分段（跨页合并块含后页续段），bbox 恒为首段坐标；
    坐标超出页面的原始块会被裁剪到页矩形；空页/越界页安全跳过。
    """
    doc = pymupdf.open(file_path)
    try:
        needed = sorted(
            {
                p.get("page")
                for p in pages
                if isinstance(p, dict) and isinstance(p.get("page"), int)
            }
        )
        raws: dict[int, list] = {}
        rects: dict[int, pymupdf.Rect] = {}
        norms: dict[int, list[str]] = {}
        alphas: dict[int, list[str]] = {}
        for pno in needed:
            if 0 <= pno < len(doc):
                raw = doc[pno].get_text("blocks")
                raws[pno] = raw
                rects[pno] = doc[pno].rect
                norms[pno], alphas[pno] = _norms_of(raw)
        used: set[tuple[int, int]] = set()  # 续段消耗标记 (页号, 原始块下标)

        def _seg(pno: int, i: int) -> dict | None:
            r = pymupdf.Rect(raws[pno][i][:4]) & rects[pno]
            if r.is_empty:
                return None
            return {
                "page": pno,
                "bbox": [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)],
            }

        for page in pages:
            if not isinstance(page, dict):
                continue
            pno = page.get("page")
            blocks = page.get("blocks") or []
            if not isinstance(pno, int) or pno not in raws or not blocks:
                continue
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                b["bbox"] = None
                b["bboxes"] = []
                o = (b.get("original") or "").strip()
                if not o or _IMG_ONLY.match(o):
                    continue
                O = _md_norm(o)
                if not O:
                    continue
                hit = _match_block(O[:24], norms[pno], alphas[pno])
                if hit is None:
                    continue
                # 锚点段：定位锚点原始块在逻辑块归一化文本中的位置
                t0 = norms[pno][hit]
                k = O.find(t0)
                if k < 0:
                    k = 0
                pos = k + len(t0)
                segs = []
                s = _seg(pno, hit)
                if s:
                    segs.append(s)
                # 续段消耗：本文本页与后两页中查找逻辑块剩余文本的片段
                # （跨页合并的续文挂在下一页页首；find 容忍列重排导致的顺序差）
                for q in (pno, pno + 1, pno + 2):
                    if q not in raws or pos >= len(O) or len(segs) >= _MAX_SEGS:
                        continue
                    for j, t in enumerate(norms[q]):
                        if pos >= len(O) or len(segs) >= _MAX_SEGS:
                            break
                        if len(t) < _MIN_CONT_NORM or (q, j) in used:
                            continue
                        idx = O.find(t, pos)
                        if idx < 0:
                            continue
                        used.add((q, j))
                        pos = idx + len(t)
                        s = _seg(q, j)
                        if s:
                            segs.append(s)
                if segs:
                    b["bboxes"] = segs
                    b["bbox"] = segs[0]["bbox"]
    finally:
        doc.close()
