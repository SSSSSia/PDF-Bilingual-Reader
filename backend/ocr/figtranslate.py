"""图表"译制图"生成。

两类对象两条路径：

- 表格（tab_*，kind=table）：结构化重建。表格有真实行列结构
  （find_tables 可解析），抽取单元格 → 逐格批量翻译 → PIL 重画一张
  干净的网格表格（自动列宽、自动折行、表头底色加粗）。
  旧方案"redact 背景 + 原坐标叠回译文"对表格必然失败：中文译文比
  英文原文长好几倍，单行叠字互相穿插重叠（实测踩坑，用户截图）。

- 图（fig_*）：坐标叠回。保留图表的原始排版与图形，只把图内文字
  换成译文——图内文字多为短标签，叠回效果可接受。
  1. 读快照 sidecar JSON（<fig>.json：区域坐标 + 图内逐行文字 bbox/字号/颜色）；
  2. 在文档副本上 redact 区域内全部文字（图注在区域外/已豁免），
     渲染"无字背景图"（图形、线条、色块原样保留）；
  3. 逐行调翻译 API 得到译文；
  4. PIL 把译文按原坐标、原字号、原颜色叠回背景图（宽度超框自动缩字号）。

产物 <fig>.zh.png 与原图同位置存放，前端就地替换显示。
任一环节失败都回退原图（translated = original），绝不阻断主流程。
"""
import asyncio
import json
import os
import re

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from translate.base import translate_batch, translate_text

# 渲染倍率：与 textlayer._FIG_SCALE 保持一致，坐标换算共用
_FIG_SCALE = 2.5
# 译制图文字最小字号（pt，小于则不再缩小、允许溢出）
_MIN_FONT_SIZE = 5.0
# 单次批量翻译的行数上限（行多时分批）
_BATCH_SIZE = 20

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑（Win10+ 必有）
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_FONT_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",  # 微软雅黑 Bold
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

# 最近一次失败原因（供接口 detail 透传给前端显示，替代静默 500）
_last_error = ""


def last_error() -> str:
    return _last_error


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | None:
    """按字号加载 CJK 字体（带缓存）。找不到任何字体返回 None（用默认位图字体）。"""
    for path in _FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES:
        if os.path.isfile(path):
            key = (path, size)
            if key not in _font_cache:
                try:
                    _font_cache[key] = ImageFont.truetype(path, size)
                except Exception:
                    continue
            return _font_cache[key]
    return None


def _int_to_rgb(color: int) -> tuple[int, int, int]:
    """PyMuPDF 的 sRGB 整数色 → PIL RGB 元组。"""
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


# 纯标识符/数值标签：型号名（GPT-4o-mini）、数据集名（WebQSP）、分数等
# ——这类翻译只会产生截断乱码，本地化图表惯例也是原样保留。
# 注意：字符类里不能有空格！上一版把 \s 放进去，导致所有带空格的英文短语
#（Formation of one tree layer 等）被误判为标识符而整图跳过翻译（实测踩坑）
_PLAIN_TOKEN = re.compile(r"^[A-Za-z0-9_\-·.:/()%+≥≤±×—=|,]+$")


def _needs_translation(text: str, target_lang: str) -> bool:
    t = (text or "").strip()
    if len(t) <= 3:
        return False
    # 连字符短语（Fill-in-blank / Multi-choice / True-or-false）要翻译——
    # 必须放在 _PLAIN_TOKEN 之前：它们恰好全部落在标识符字符类里，
    # 先查标识符会提前放行（实测表头全英文的踩坑）。
    # 型号名（G-Retriever / GPT-4o-mini：首段非"大写+小写"形态）仍是标识符。
    if re.match(r"^[A-Z][a-z]+(-[a-z]+)+$", t):
        return True
    if _PLAIN_TOKEN.match(t):
        return False
    # 译中文：没有任何英文字母的行（纯数字/符号/已是中文）无需翻译
    if target_lang.lower().startswith("zh") and not re.search(r"[A-Za-z]", t):
        return False
    return True


def _render_textless_bg(file_path: str, page_num: int, region: list[float], out_path: str) -> bool:
    """渲染"去文字背景图"：redact 副本上去掉区域内所有文字后截图。

    images=NONE 保留位图、graphics=LINE_ART_NONE 保留矢量线条
    （图表的柱形/折线/坐标轴都是矢量——默认会被一起删掉，实测踩坑），
    只移除文字。失败返回 False（调用方回退原图）。"""
    try:
        with pymupdf.open(file_path) as doc2:
            page = doc2[page_num]
            r = pymupdf.Rect(*region)
            page.add_redact_annot(r)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )
            pix = page.get_pixmap(
                clip=r, matrix=pymupdf.Matrix(_FIG_SCALE, _FIG_SCALE)
            )
            pix.save(out_path)
            return True
    except Exception as e:
        print(f"[figtranslate] 背景渲染失败: {e}")
        return False


def _clean_translated(t: str) -> str:
    """剥掉翻译模型夹带的 markdown 装饰符（### 标题、**加粗、` 代码等）——
    叠字场景里它们只会变成乱码符号（实测踩坑）。"""
    t = re.sub(r"^[#\s`>*\-]+", "", t or "")
    t = t.replace("**", "").replace("`", "").replace("__", "")
    return t.strip()


# ---------- 表格：结构化重建 ----------

# 表格渲染参数（像素；2x 于页面渲染，观感与快照一致）
_TABLE_FONT_PX = 20
_TABLE_LINE_H = 24  # 行高（含行距）
_TABLE_PAD_X, _TABLE_PAD_Y = 10, 6
_TABLE_MIN_COL_W = 56  # 单列最小宽
_TABLE_MAX_COL_W = 560  # 单列最大宽（超出折行）
_TABLE_GAP = 20  # 同一快照区域内多张表之间的间距
_GRID_COLOR = (148, 163, 184)  # 网格线 slate-400
_HEADER_BG = (241, 245, 249)  # 表头底色 slate-100
_TEXT_COLOR = (15, 23, 42)  # 正文 slate-900

# 常用表头词表：单词表头被标识符规则跳过（不会发给模型），
# 命中词表直接本地翻译（Organization/Organization 之类无需消耗 API）
_GLOSSARY = {
    "method": "方法",
    "organization": "组织方式",
    "description": "描述",
    "accuracy": "准确率",
    "average": "平均",
    "dataset": "数据集",
    "question type": "问题类型",
    "type": "类型",
    "category": "类别",
    "cost": "成本",
    "total": "总计",
}

# 表格单元格专用提示：通用翻译提示下，Qwen3 对短术语常原样返回
_CELL_PROMPT = (
    "你是学术文献表格翻译助手。把给定的英文表格单元格文本翻译为简体中文。"
    "列名和表头短语（如 Multi-choice、True-or-false、Retrieval operators）"
    "必须译成对应的中文术语；方法名、数据集名、模型名、数字和单位原样保留；"
    "只输出译文本身，不要解释、不要加粗符号、不要附原文。"
)


_Y_TOL = 3.5  # 视觉行聚类容差（pt）
_COL_GAP_MIN = 7.0  # 词间隙超过该值视为列边界（pt）


def _region_tables(src_pdf: str, page_num: int, region: list[float]):
    """从词坐标重建快照区域内的表格。

    返回 (blocks, col_fracs)：
    - blocks: 按块（多张表）组织的 {"rows": 行列表, "spans": 跨列行下标集}；
    - col_fracs: 原表各列宽度占比（渲染时按比例缩放，与原图对齐）。

    不用 find_tables：实测这类表格线不全，它会把整个数据区并成一个
    大单元格。
    改用"表格列垂直对齐"特性：
    1. 词按 y 中心聚类成视觉行，行内按词间隙切段（记录每段 x 范围）；
    2. 取段数最多的视觉行作为列定义行（通常即表头/最宽行），
       列边界 = 其相邻段的中点；其余行的段按 x 中心归位。
       不用跨行聚类间隙中点：中点随左列文字长度浮动，
       同一列边界会被拆成多簇；
    3. 行分类：
       - 续行：首列空且所有内容段都是"窄段"（段宽 ≤1.3×所在列宽，
         即单元格内折行）→ 并入上一行；
       - 跨列行：含空单元格且非续行——多级表头（"Multi-Hop KBQA" 横跨
         数列）与分组分隔行（"With external knowledge" 段宽横跨数列）。
         此前无差别并入上一行，分隔行被吸进数据行中间（用户截图踩坑）；
       - 其余为普通数据行（整行满格）。
    4. 行间距显著变大处切分为多张表（分隔行保留后正常表不再被误切）；
    5. 丢弃混进区域的段落/图注行（单格超长）。"""
    try:
        with pymupdf.open(src_pdf) as doc2:
            page = doc2[page_num]
            r = pymupdf.Rect(*region)
            words = [
                w for w in page.get_text("words")
                if pymupdf.Rect(w[:4]).intersects(r)
            ]
            if not words:
                return []
            # 1) y 聚类成视觉行
            words.sort(key=lambda w: (w[1], w[0]))
            vlines: list[tuple[float, list]] = []
            for w in words:
                yc = (w[1] + w[3]) / 2
                if vlines and abs(yc - vlines[-1][0]) <= _Y_TOL:
                    vlines[-1][1].append(w)
                else:
                    vlines.append((yc, [w]))
            # 2) 行内按间隙切段，记录每段 x 范围
            seg_lines: list[tuple[float, list[tuple[float, float, str]]]] = []
            for yc, ws in vlines:
                ws.sort(key=lambda w: w[0])
                segs, cur = [], [ws[0]]
                for a, b in zip(ws, ws[1:]):
                    if b[0] - a[2] > _COL_GAP_MIN:
                        segs.append(cur)
                        cur = [b]
                    else:
                        cur.append(b)
                segs.append(cur)
                seg_lines.append(
                    (yc, [(s[0][0], s[-1][2], " ".join(w[4] for w in s)) for s in segs])
                )
            # 3) 取段数最多的视觉行作为列定义行（通常即表头/最宽行），
            #    列边界 = 其相邻段的中点；其余行的段按 x 中心归位。
            #    不用跨行聚类间隙中点：中点随左列文字长度浮动，
            #    同一列边界会被拆成多簇
            ref = max(seg_lines, key=lambda t: len(t[1]))[1]
            ncol = len(ref)
            bounds = [(ref[i][1] + ref[i + 1][0]) / 2 for i in range(ncol - 1)]

            # 3) 组装行并分类（续行 / 跨列行 / 普通行）。
            #    续行判定用双条件：
            #    - 段宽窄（单元格内折行不会超出所在列宽）；
            #    - 行距近（≤1.25×中位行距）——分组分隔行（"With external
            #      knowledge"）上下留空、行距 1.35×，且小体大写字母间距
            #      会被切成多个窄段，段宽条件拦不住，行距才能拦住。
            gaps_raw = sorted(b[0] - a[0] for a, b in zip(seg_lines, seg_lines[1:]))
            med_gap = gaps_raw[len(gaps_raw) // 2] if gaps_raw else 0.0
            col_pt_w = [r.x0, *bounds, r.x1]
            col_pt_w = [col_pt_w[i + 1] - col_pt_w[i] for i in range(len(col_pt_w) - 1)]
            # [y, cells, 跨列行]（列表为了合并续行后能更新跨列标记）
            assembled: list[list] = []
            for yc, segs in seg_lines:
                cells = [""] * ncol
                widths = [0.0] * ncol
                for x0, x1, txt in segs:
                    ci = min(sum(1 for b in bounds if (x0 + x1) / 2 > b), ncol - 1)
                    cells[ci] = (cells[ci] + " " + txt).strip()
                    widths[ci] = max(widths[ci], x1 - x0)
                # 丢弃混入的段落/图注行（单格超长；表头跨列行较短不受影响）
                if sum(1 for c in cells if c) == 1 and max(map(len, cells)) > 60:
                    continue
                nonempty = [i for i, c in enumerate(cells) if c]
                gap_ok = (
                    bool(assembled)
                    and yc - assembled[-1][0] <= max(med_gap * 1.25, _Y_TOL * 2)
                )
                narrow = nonempty and all(
                    widths[i] <= 1.3 * col_pt_w[i] for i in nonempty
                )
                if assembled and cells[0] == "" and gap_ok and narrow:
                    # 续行 → 并入上一行
                    prev = assembled[-1]
                    for ci in nonempty:
                        prev[1][ci] = (prev[1][ci] + " " + cells[ci]).strip()
                    if all(prev[1]):  # 合并后满格 → 不再是跨列行
                        prev[2] = False
                    continue
                # 含空格单元格的独立行 → 跨列行（多级表头/分组分隔行）
                assembled.append([yc, cells, any(c == "" for c in cells)])
            if not assembled:
                return []
            # 4) 行间距显著变大处切分为多张表
            gaps = [b[0] - a[0] for a, b in zip(assembled, assembled[1:])]
            med = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
            thr = max(med * 2.5, 15.0)
            blocks: list[dict] = []
            cur: dict = {"rows": [assembled[0][1]], "spans": set()}
            if assembled[0][2]:
                cur["spans"].add(0)
            for (ya, ra, sa), (yb, rb, sb) in zip(assembled, assembled[1:]):
                if yb - ya > thr:
                    blocks.append(cur)
                    cur = {"rows": [], "spans": set()}
                cur["rows"].append(rb)
                if sb:
                    cur["spans"].add(len(cur["rows"]) - 1)
            blocks.append(cur)
            # 5) 原表列宽占比（渲染时等比缩放，与原图对齐）
            edges = [r.x0, *bounds, r.x1]
            fracs = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
            tot = sum(fracs) or 1.0
            return blocks, [f / tot for f in fracs]
    except Exception as e:
        print(f"[figtranslate] 表格结构解析失败: {e}")
        return []


async def _translate_cells(
    tables: list[list[list[str]]],
    source_lang: str,
    target_lang: str,
    t_cfg: dict,
) -> bool:
    """逐格并发翻译（每格独立请求），原地回填。始终返回 True：
    单格失败只影响那一格（保留原文），不让整图失败。

    不走 translate_batch 的合并协议：表格单元格是短文本，实测合并翻译
    会诱发模型幻觉——给 "Retrieval operators" 返回两千字的跑题示例
    。并发单发既快又稳。"""
    global _last_error
    is_zh = target_lang.lower().startswith("zh")
    uniq: dict[str, str] = {}
    for rows in tables:
        for row in rows:
            for c in row:
                # 词表命中的单词表头即使被标识符规则跳过也收进来，
                # 这样下方词表回填才能覆盖到它（如 Organization/Average）
                if c and (
                    _needs_translation(c, target_lang)
                    or (is_zh and c.strip().lower() in _GLOSSARY)
                ):
                    uniq.setdefault(c, c)
    items = list(uniq)

    async def _one(t: str) -> str:
        try:
            cell_cfg = {**t_cfg, "system_prompt": _CELL_PROMPT}
            return _clean_translated(
                await translate_text(t, source_lang, target_lang, cell_cfg)
            )
        except Exception as e:
            print(f"[figtranslate] 单元格翻译失败（保留原文）: {t[:20]}…: {e}")
            return ""

    outs = await asyncio.gather(*(_one(t) for t in items))
    for k, o in zip(items, outs):
        # 译文为空或长度异常（模型幻觉跑题的典型特征）→ 保留原文。
        # 上限放宽到 8 倍：模型习惯"译文（附原文）"，正常译文也不短
        uniq[k] = o if o and len(o) <= max(8 * len(k) + 40, 120) else k
        # 单词表头（Organization/Average 等）模型可能原样返回 → 查词表
        if is_zh and uniq[k] == k:
            g = _GLOSSARY.get(k.strip().lower())
            if g:
                uniq[k] = g
    for rows in tables:
        for ri, row in enumerate(rows):
            rows[ri] = [uniq.get(c, c) for c in row]
    return True


def _wrap_cell(text: str, font, maxw: int, measure) -> list[str]:
    """单元格文本折行：优先按空格断词，超长单词按字符硬断。"""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return [""]
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        cand = f"{cur} {word}" if cur else word
        if measure.textlength(cand, font=font) <= maxw:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while measure.textlength(word, font=font) > maxw and len(word) > 1:
            keep = 1
            for k in range(2, len(word) + 1):
                if measure.textlength(word[:k], font=font) <= maxw:
                    keep = k
                else:
                    break
            lines.append(word[:keep])
            word = word[keep:]
        cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _render_tables_png(
    blocks: list[dict],
    out_path: str,
    col_fracs: list[float] | None = None,
    width_hint: float = 0.0,
) -> None:
    """把（已翻译的）表格块渲染为干净网格 PNG。

    - 列宽：有 col_fracs（原表列宽占比） + width_hint（原快照像素宽）时
      等比缩放，与原图布局对齐；
      否则按内容自动列宽。
    - 跨列行（多级表头/分组分隔行）：空单元格向右并入内容格（colspan），
      分隔行居中显示——与原表视觉层级一致。
    - 对齐：首列左对齐，其余列水平居中（学术表格惯例，与原图一致）。"""
    font = _load_font(_TABLE_FONT_PX)
    bold = _load_font(_TABLE_FONT_PX, bold=True) or font
    font = font or bold
    measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def _fit_lines(text: str, maxw: float, base, bold: bool):
        """优先单行：字号从基准逐级缩到 12px，放得下就不折行——
        行高与原图一致。"""
        text = (text or "").replace("\n", " ").strip()
        if not text:
            return [""], base
        for size in range(_TABLE_FONT_PX, 11, -1):
            f = _load_font(size, bold=bold)
            if f is None:
                break
            if measure.textlength(text, font=f) <= maxw:
                return [text], f
        return _wrap_cell(text, base, maxw, measure), base

    def _layout(block: dict):
        rows = block["rows"]
        spans = block["spans"]
        ncol = max(len(r) for r in rows)
        rows = [list(r) + [""] * (ncol - len(r)) for r in rows]
        if col_fracs and len(col_fracs) == ncol and width_hint:
            col_w = [
                min(max(_TABLE_MIN_COL_W, int(f * width_hint)), _TABLE_MAX_COL_W)
                for f in col_fracs
            ]
        else:
            col_w = []
            for c in range(ncol):
                w = _TABLE_MIN_COL_W
                for r in rows:
                    for seg in str(r[c]).split("\n"):
                        w = max(w, measure.textlength(seg, font=font))
                col_w.append(min(int(w) + _TABLE_PAD_X * 2, _TABLE_MAX_COL_W))
        # 每行渲染单元：(lines, 起列, 止列, 字体)；跨列行空格向右并入内容格
        grid: list[list[tuple[list[str], int, int, object]]] = []
        for ri, r in enumerate(rows):
            is_header = ri == 0
            if ri in spans:
                cells: list[tuple[list[str], int, int, object]] = []
                ci = 0
                while ci < ncol:
                    if not r[ci]:
                        cells.append(([""], ci, ci, font))  # 前导空组
                        ci += 1
                        continue
                    cj = ci
                    while cj + 1 < ncol and not r[cj + 1]:
                        cj += 1
                    w = sum(col_w[ci : cj + 1]) - _TABLE_PAD_X * 2
                    lines, f = _fit_lines(r[ci], w, font if not is_header else bold, is_header)
                    cells.append((lines, ci, cj, f))
                    ci = cj + 1
                grid.append(cells)
            else:
                cells = []
                for ci, c in enumerate(r):
                    w = col_w[ci] - _TABLE_PAD_X * 2
                    lines, f = _fit_lines(c, w, bold if is_header else font, is_header)
                    cells.append((lines, ci, ci, f))
                grid.append(cells)
        row_h = [
            max(len(c[0]) for c in cells) * _TABLE_LINE_H + _TABLE_PAD_Y * 2
            for cells in grid
        ]
        return col_w, grid, row_h, ncol

    layouts = [_layout(b) for b in blocks]
    W = max(sum(l[0]) for l in layouts) + 2
    H = sum(sum(l[2]) for l in layouts) + _TABLE_GAP * (len(layouts) - 1) + 2
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    y = 1
    for col_w, grid, row_h, ncol in layouts:
        xs = [1]
        for w in col_w:
            xs.append(xs[-1] + w)
        top = y
        for ri, cells in enumerate(grid):
            rh = row_h[ri]
            y_end = y + rh
            is_header = ri == 0
            if is_header:
                d.rectangle([xs[0], y, xs[-1], y_end], fill=_HEADER_BG)
            for lines, c0, c1, f in cells:
                gw = xs[c1 + 1] - xs[c0]
                text_w = max((measure.textlength(ln, font=f) for ln in lines), default=0)
                if c0 == 0:
                    # 首列（含跨到首列的组）左对齐
                    tx = xs[c0] + _TABLE_PAD_X
                else:
                    # 其余列/分组表头/分隔行居中
                    tx = xs[c0] + max((gw - text_w) / 2, _TABLE_PAD_X)
                ty = y + _TABLE_PAD_Y
                for ln in lines:
                    d.text((tx, ty), ln, fill=_TEXT_COLOR, font=f)
                    ty += _TABLE_LINE_H
            # 横线（整行宽）
            d.line([xs[0], y_end, xs[-1], y_end], fill=_GRID_COLOR, width=1)
            # 竖线：逐列边界画本行高度，跨列组内部不画
            spanned = set()
            for _, c0, c1, _f in cells:
                spanned.update(range(c0 + 1, c1 + 1))
            for ci in range(1, ncol):
                if ci not in spanned:
                    d.line([xs[ci], y, xs[ci], y_end], fill=_GRID_COLOR, width=1)
            y = y_end
        for x in (xs[0], xs[-1]):
            d.line([x, top, x, y], fill=_GRID_COLOR, width=1)
        y += _TABLE_GAP
    img.save(out_path)


def _overlay_text(bg_path: str, lines: list[dict], region: list[float], out_path: str) -> None:
    """把译文按原坐标叠到背景图上。bbox 为页面坐标，×倍率映射到像素。"""
    img = Image.open(bg_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    for ln in lines:
        translated = _clean_translated(ln.get("translated") or "")
        if not translated:
            continue
        x0, y0, x1, y1 = ln["bbox"]
        # 相对区域坐标 × 倍率 = 像素坐标
        px0 = (x0 - region[0]) * _FIG_SCALE
        py0 = (y0 - region[1]) * _FIG_SCALE
        box_w = max((x1 - x0) * _FIG_SCALE, 8.0)
        size_pt = max(ln.get("size", 10.0), _MIN_FONT_SIZE)
        px_size = max(int(round(size_pt * _FIG_SCALE)), 8)
        font = _load_font(px_size)
        color = _int_to_rgb(ln.get("color", 0))
        if font is not None:
            # 宽度超框自动缩字号（中文译文常比原文长）
            while px_size > 8:
                try:
                    w = draw.textlength(translated, font=font)
                except Exception:
                    break
                if w <= box_w * 1.08:
                    break
                px_size -= 1
                font = _load_font(px_size)
                if font is None:
                    break
        # 缩到最小仍超框 → 截断加省略号（重叠的乱图比截断更不可读）
        if font is not None:
            try:
                while len(translated) > 2 and draw.textlength(translated, font=font) > box_w * 1.08:
                    translated = translated[:-2].rstrip() + "…"
            except Exception:
                pass
        try:
            draw.text((px0, py0), translated, fill=color, font=font)
        except Exception:
            continue  # 单行失败不影响整图
    img.save(out_path)


# 译制图版本号：改动生成逻辑时 +1，使旧译制图自动失效重生成
# v3：表格改结构化重建
# v4：列边界跨行聚类 + 续行合并
# v5：跨列行（多级表头/分组分隔行） + 原比例列宽对齐原图
OVERLAY_VERSION = "v5"


async def translate_figure(
    sidecar_path: str, file_path: str | None = None, t_cfg: dict | None = None
) -> str | None:
    """生成一张译制图，返回译制图路径；失败返回 None（调用方回退原图）。

    sidecar_path 为 <fig>.json；译制图写在旁边 <fig>.zh<版本>.png。
    file_path 不传时用 sidecar 里记录的源 PDF（按需触发场景）。
    表格走结构化重建（_region_tables 解析成功时），图走坐标叠回。"""
    global _last_error
    _last_error = ""
    try:
        with open(sidecar_path, encoding="utf-8") as f:
            sidecar = json.load(f)
    except Exception as e:
        _last_error = f"无法读取表格元数据: {e}"
        return None
    png = sidecar.get("png") or ""
    region = sidecar.get("region") or []
    page_num = sidecar.get("page")
    if not png or not region or not os.path.isfile(png) or page_num is None:
        _last_error = "快照元数据不完整（png/region/page 缺失或快照文件不存在）"
        return None
    src_pdf = file_path or sidecar.get("pdf") or ""
    if not src_pdf or not os.path.isfile(src_pdf):
        _last_error = f"源 PDF 不可用: {src_pdf or '(空)'}"
        print(f"[figtranslate] 源 PDF 不可用，无法渲染无字背景")
        return None
    if t_cfg is None:
        t_cfg = {}
    source_lang = t_cfg.get("source_language", "en")
    target_lang = t_cfg.get("target_language", "zh")

    zh_png = png[:-4] + f".zh.{OVERLAY_VERSION}.png"
    if os.path.isfile(zh_png):
        return zh_png  # 已生成过（重复点击幂等）

    # ---------- 表格：结构化重建（解析出真实行列才能走这条路） ----------
    if sidecar.get("kind") == "table":
        parsed = await asyncio.to_thread(_region_tables, src_pdf, page_num, region)
        blocks: list[dict] = []
        col_fracs: list[float] | None = None
        if isinstance(parsed, tuple):
            blocks, col_fracs = parsed
        if blocks:
            if not await _translate_cells(
                [b["rows"] for b in blocks], source_lang, target_lang, t_cfg
            ):
                return None

            def _render_tables() -> str | None:
                try:
                    _render_tables_png(
                        blocks,
                        zh_png,
                        col_fracs=col_fracs,
                        width_hint=(region[2] - region[0]) * _FIG_SCALE,
                    )
                    return zh_png
                except Exception as e:
                    global _last_error
                    _last_error = f"表格重绘失败: {e}"
                    print(f"[figtranslate] 表格重绘失败: {e}")
                    return None

            return await asyncio.to_thread(_render_tables)
        # 解析不出结构（如扫描版表格）→ 落到下面的叠回路径兜底
        print("[figtranslate] 表格结构解析为空，回退坐标叠回")

    # ---------- 图（fig_*）/ 兜底：坐标叠回 ----------
    lines = sidecar.get("lines") or []
    if not lines:
        _last_error = "图内没有可翻译的文字（纯图形或无文本层）"
        return None  # 图里没文字（纯图形），无需译制

    # 1) 批量翻译图内文字。
    #    标识符/数值行原样叠回（不浪费 API 也不产生截断乱码）；
    #    已翻过的行持久化在 sidecar 里，重渲染不重复调 API。
    #    译文与原文相同（历史错误缓存，如标识符误判版过滤的产物）→ 重翻。
    for ln in lines:
        if not _needs_translation(ln["text"], target_lang):
            ln["translated"] = ln["text"]
    missing = [
        ln
        for ln in lines
        if "translated" not in ln
        or not ln["translated"].strip()
        or ln["translated"].strip() == ln["text"].strip()
    ]
    if missing:
        texts = [ln["text"] for ln in missing]
        translated: list[str] = []
        try:
            for i in range(0, len(texts), _BATCH_SIZE):
                batch = texts[i : i + _BATCH_SIZE]
                outs = await translate_batch(batch, source_lang, target_lang, t_cfg)
                if len(outs) != len(batch):
                    return None
                translated.extend(outs)
        except Exception as e:
            _last_error = f"图内文字翻译失败: {e}"
            print(f"[figtranslate] 图内文字翻译失败: {e}")
            return None
        for ln, tr in zip(missing, translated):
            ln["translated"] = tr
        try:
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar, f, ensure_ascii=False)
        except Exception:
            pass

    # 2) 无字背景 + 叠字（PIL 渲染是 CPU 密集，放线程）
    def _render() -> str | None:
        global _last_error
        bg = os.path.splitext(png)[0] + ".bg.png"
        if not _render_textless_bg(src_pdf, page_num, region, bg):
            _last_error = "无字背景渲染失败（见后端日志）"
            return None
        try:
            _overlay_text(bg, lines, region, zh_png)
            return zh_png
        finally:
            try:
                os.remove(bg)
            except OSError:
                pass

    return await asyncio.to_thread(_render)
