"""标题几何检测。

历史教训（用户质疑："每次都是针对不同文章特调"）：此前从 markdown 文本
内容猜标题——词数/句法/重复特征，全是见一个补一个个案规则， GraphRAG-Bench
催生降级、催生 ≥14 词（又误杀）、栽在段尾截除。根因是
**信号源错了**：对另一个启发式的二手输出做字符串推理。

本模块改读一手证据，对没见过的版式同样成立：
1. 学术排版铁律——第 1 页字号最大、最靠上的连续行就是标题。直接读
   pymupdf 的 span 字号/坐标（出版模板千差万别，"标题比正文大"无一例外）；
2. PDF metadata 自带 title（arXiv/ACM/IEEE 出版 PDF 绝大多数内嵌）——
   与排版无关的独立信号，归一化后与几何结果互为核对。

文本启发式（processor._extract_doc_title 原两级优先级）降为兜底。
"""

import os
import re

import pymupdf

# 标题候选的字号容差（同一标题内行字号一致；作者行通常小 1pt 以上）
_SIZE_TOL = 0.6
# 相邻行基线距 < 1.8em 视为同一标题（两行大标题的合并）
_LINE_MERGE_EM = 1.8
_MIN_TITLE_CHARS = 8

_NORM_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def norm_text(s: str) -> str:
    """标题比对归一化：只留字母/数字/中日韩文字，忽略大小写与全部标点。"""
    return _NORM_RE.sub("", s).lower()


def detect_title(file_path: str) -> str | None:
    """几何检测第 1 页标题；拿不到返回 None（调用方回退文本启发式）。

    返回纯文本（无 markdown 记号）；metadata 互证时优先采用 metadata
    （出版者写入，通常比 OCR 拼接更干净）。"""
    if not file_path or not os.path.isfile(file_path):
        return None
    doc = pymupdf.open(file_path)
    try:
        meta = (doc.metadata or {}).get("title") or ""
        geo = _detect_by_geometry(doc[0])
        if not geo:
            return meta.strip() or None
        if meta and _overlap(norm_text(meta), norm_text(geo)):
            return meta.strip()
        return geo
    finally:
        doc.close()


def _overlap(a: str, b: str) -> bool:
    """归一化标题互证：一方包含另一方（短标题完整出现在长版本里即互证）。"""
    if not a or not b:
        return False
    return a in b or b in a


def _detect_by_geometry(page) -> str | None:
    """页 0 最大字号 + 最靠上的连续行 = 标题。"""
    info = page.get_text("dict")
    page_h = page.rect.height
    lines: list[dict] = []
    for block in info.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            # 竖排文字剔除（arXiv 左缘印章等，dir=(0,±1)）——只认横排行
            dx, _dy = line.get("dir", (1, 0))
            if abs(dx) < 0.9:
                continue
            spans = [
                s
                for s in line.get("spans", [])
                if s.get("text", "").strip() and s.get("size", 0) > 0
            ]
            if not spans:
                continue
            # 片段拼接：小写大写混排（小型大写标题 L|IGHT）按横向间距决定
            # 是否补空格——间距 <1.2pt 视为同词，直接相连
            parts = [spans[0]["text"].strip()]
            prev = spans[0]
            for s in spans[1:]:
                gap = s["bbox"][0] - prev["bbox"][2]
                parts.append((" " if gap > 1.2 else "") + s["text"].strip())
                prev = s
            text = "".join(parts).strip()
            # 纯页码/符号行剔除；arXiv 印章双保险
            if len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)) < 4:
                continue
            if re.match(r"arXiv:\S+", text):
                continue
            size = max(s["size"] for s in spans)
            bbox = line["bbox"]
            # 页脚/页码带剔除（版面底部 12%——标题从不出现那里）
            if bbox[1] > page_h * 0.88:
                continue
            lines.append(
                {
                    "text": text,
                    "size": size,
                    "y0": bbox[1],
                    "y1": bbox[3],
                    "x0": bbox[0],
                    "x1": bbox[2],
                }
            )
    if not lines:
        return None

    max_size = max(ln["size"] for ln in lines)
    cand = sorted(
        (ln for ln in lines if ln["size"] >= max_size - _SIZE_TOL),
        key=lambda ln: (ln["y0"], ln["x0"]),
    )
    if not cand:
        return None

    # 合并相邻行（两行大标题）：基线距 < 1.8em 即同块
    merged: list[list[dict]] = [[cand[0]]]
    for ln in cand[1:]:
        prev = merged[-1][-1]
        if ln["y0"] - prev["y0"] <= _LINE_MERGE_EM * max_size:
            merged[-1].append(ln)
        else:
            merged.append([ln])

    # 多个同字号块时取最靠上的；过短（<8 有效字符）视为装饰字，顺延下一块
    for group in merged:
        text = " ".join(ln["text"] for ln in group).strip()
        text = re.sub(r"\s+", " ", text)
        if len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)) >= _MIN_TITLE_CHARS:
            return text
    return None
