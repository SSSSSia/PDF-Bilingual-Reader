"""文本层直接提取（修复"OCR 内容不全"的核心）。

大量 PDF 是电子版（Word/LaTeX 导出），自带完整文本层——直接提取可拿到
100% 完整的文字与结构，零 API 成本、秒级完成，远优于视觉模型"看图识字"。
只有无文本层的扫描页/图片页才需要回退视觉 OCR（见 pipeline/processor.py）。

pymupdf4llm 基于 PyMuPDF，输出带标题/加粗/表格结构的 Markdown，
并可把页内嵌入图片导出为文件、在 Markdown 中保留引用（对标 Scholaread
的"图片随排版流展示"效果）。
"""
import os
import re
import unicodedata

import pymupdf

# pymupdf/pymupdf4llm 版本钉扎在 1.26.4/0.0.27（2026-09-13 决策）：
# pymupdf4llm≥1.28 引入的 Layout 引擎（onnx 版面模型）有两个致命问题——
# ① helper 链无条件硬导入 onnxruntime，PyInstaller onefile 冻结环境原生
#    段错误（msvcp140 运行库冲突类，collect-all 也压不住），打包版后端
#    启动即崩（白屏+门禁卡死事故根因）；② 输出结构与全部 textlayer 清洗
#    规则的调优基线（classic 管线）不同。0.0.27 天然 classic、无 onnxruntime
#    依赖。sys.modules 桩防 pymupdf 侧未来自动导入 layout。
import sys as _sys

_sys.modules.setdefault("pymupdf.layout", None)

import pymupdf4llm  # noqa: E402

# 文本层字符数低于该阈值视为"无有效文本层"（扫描页/纯图片页），回退视觉 OCR
MIN_TEXT_CHARS = 120

_IMG_REF = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SUP_TAG = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)
_SUB_TAG = re.compile(r"<sub>(.*?)</sub>", re.IGNORECASE | re.DOTALL)
# 下划线标签（阶段2-T5 用户反馈"下划线还在"）：markdown 无下划线语法，
# 裸 HTML 会原样露出 → 只剥标签保留内容
_U_TAG = re.compile(r"</?u>", re.IGNORECASE)

# ── 双栏页列感知阅读顺序重排（2026-09-07 用户反馈"原文不全"）──────────
# pymupdf4llm 按 y 带交错输出左右栏（DALK 首页实测：右栏顶部的段落续文
# 被排到摘要之前，还与 Abstract 头粘连）。标准双栏页的真实阅读顺序是
# 左栏自上而下 → 右栏自上而下。判定保守：段落→原始块文本前缀匹配须
# 命中大多数（少数措辞漂移段跟随邻段，见函数内）、左右两侧各 ≥3 个
# 窄块、x 范围有干净分栏沟，否则原样返回。
_MD_NORM = re.compile(r"[^a-z0-9]+")
_MD_NORM_ALPHA = re.compile(r"[^a-z]+")
_COL_WIDE = 0.55  # 块宽超过页宽此比例视为通栏（标题/摘要横排）
# 纯图片引用段（快照锚定插入的 ![Figure](path) 单段）
_IMG_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")


def _md_norm(s: str) -> str:
    return _MD_NORM.sub("", (s or "").lower())


def _match_block(h: str, norms: list[str], norms_alpha: list[str]) -> int | None:
    """markdown 段落头 → 原始块下标。先按字母数字匹配，失败退字母级
    （上/下标转换会把 1 变 ¹、n 变 ⁿ，数字级匹配必失败，作者行实测）。

    匹配语义锚定：段落头包含于块内（h in t），或段落以块文本开头
    （h.startswith(t[:24])，VLM 把短块与后继块粘成一段的实测形态）。
    反例禁入：无锚定的子串包含会让表格残块（"C %"→norm 后单字符"c"）
    匹配几乎所有段落（DALK p8 实测 6 段全配到一个 w=17 的残块上，
    排序 key 全为垃圾坐标）；<4 字符的头/块过短，宁可不配（跟随邻段）。"""
    if len(h) < 4:
        return None
    ha = _MD_NORM_ALPHA.sub("", h.lower())
    for i, t in enumerate(norms):
        if t and (h in t or (len(t) >= 4 and h.startswith(t[:24]))):
            return i
    if len(ha) >= 12:
        for i, t in enumerate(norms_alpha):
            if t and (ha in t or (len(t) >= 4 and ha.startswith(t[:24]))):
                return i
    return None


def _column_reading_order(raw_blocks: list, md: str, col_rects: list | None = None) -> str:
    """双栏页 markdown 段落重排为列感知顺序（raw_blocks: page.get_text('blocks')）。

    col_rects：版面模型 plain text 区域（阶段12-T10 反馈修复）——块级栏判定
    （左右各 ≥3 窄块）失败的页（右栏整栏一个粗粒度文本块，DALK 首页实测
    VLM 输出整页乱序而重排被拦）用区域级判定兜底：剔除通栏区域后左右
    各 ≥2 且栏沟干净即确证双栏。None（textlayer 降级路径）= 仅块级判定。"""
    paras = [p for p in re.split(r"\n\s*\n", md) if p.strip()]
    if len(paras) < 5:
        return md
    rects = [pymupdf.Rect(b[:4]) for b in raw_blocks]
    norms = [_md_norm(b[4]) for b in raw_blocks]
    norms_alpha = [_MD_NORM_ALPHA.sub("", (b[4] or "").lower()) for b in raw_blocks]
    page_w = max(r.x1 for r in rects) if rects else 0
    keys: list[tuple[int, float, int] | None] = []
    matched: list[tuple[pymupdf.Rect, bool, bool] | None] = []  # (rect, right, wide)
    for idx, p in enumerate(paras):
        h = _md_norm(p)[:24]
        hit = _match_block(h, norms, norms_alpha) if h else None
        if hit is None:
            keys.append(None)
            matched.append(None)
            continue
        r = rects[hit]
        wide = r.width > _COL_WIDE * page_w
        right = (not wide) and r.x0 >= page_w / 2
        keys.append((1 if right else 0, r.y0, idx))
        matched.append((r, right, wide))
    # 文本段定位不到坐标：少数（≤1/3，措辞漂移/行合并实测形态）时跟随
    # 邻段排序键重排（保持原顺序局部性，不跨页乱跳）；大面积对不上
    # （数学页 LaTeX 与文本层逐字符两套表示、参考文献切段差异）说明
    # 版式未被理解，仍整体放弃（防未知版式被搅乱）。纯图片引用段
    # 无对应文本块，不计入失配统计（DALK p7 实测：有快照的页全部
    # 跳过重排，换栏断词的续文永远排在其段头前面）。
    miss = [
        idx
        for idx, k in enumerate(keys)
        if k is None and not _IMG_ONLY.match(paras[idx])
    ]
    if len(miss) > max(1, len(paras) // 3):
        return md
    # 纯图片引用段（快照插在 caption 前的 ![Figure](...)）：坐标匹配不到
    # 文本，跟随其后第一个有 key 的段落（同 key + idx 更小 → 排在其前），
    # 重排时图片与 caption 不拆散
    for idx in range(len(paras)):
        if keys[idx] is None and _IMG_ONLY.match(paras[idx]):
            jdx = idx + 1
            while jdx < len(paras) and _IMG_ONLY.match(paras[jdx]):
                jdx += 1
            if jdx < len(paras) and keys[jdx] is not None:
                keys[idx] = keys[jdx]
    # 定位失败段与页末追加快照（没配到 caption 的）：跟随前一段（局部性）；
    # 页首连续失配段反向跟随后继——两者都不会留在原地挡重排
    for idx in range(1, len(paras)):
        if keys[idx] is None and keys[idx - 1] is not None:
            keys[idx] = keys[idx - 1]
    for idx in range(len(paras) - 2, -1, -1):
        if keys[idx] is None and keys[idx + 1] is not None:
            keys[idx] = keys[idx + 1]
    if any(k is None for k in keys):
        return md
    lefts = [m[0] for m in matched if m and not m[1] and not m[2]]
    rights = [m[0] for m in matched if m and m[1]]
    if (
        len(lefts) < 3
        or len(rights) < 3
        or max(r.x1 for r in lefts) > min(r.x0 for r in rights) + 5
    ):
        # 块级栏判定失败：区域级兜底（剔除通栏区域后左右各 ≥2 + 干净栏沟）。
        # 版面模型按语义切分，右栏整栏一个文本块的粗粒度 PDF 也能正确分栏
        cols = [r for r in (col_rects or []) if r.width <= _COL_WIDE * page_w]
        cl = [r for r in cols if r.x0 < page_w / 2]
        cr = [r for r in cols if r.x0 >= page_w / 2]
        if (
            not cols
            or len(cl) < 2
            or len(cr) < 2
            or max(r.x1 for r in cl) > min(r.x0 for r in cr) + 5
        ):
            return md  # 不像双栏（块级/区域级均未确证）
    ordered = sorted(range(len(paras)), key=lambda i: keys[i])
    return "\n\n".join(paras[i] for i in ordered)

# 上/下标字符映射：把 <sup>12</sup> 转成 ¹²，语义不丢失且不再是裸 HTML
_SUP_MAP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB_MAP = str.maketrans("0123456789+-=()n", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙ")

# caption 行锚点（图/表快照插回 markdown 的定位依据）。
# 表格也走快照（2026-09-06 用户决策：文本表格转 markdown 必错位，
# 统一按图片处理），所以 Table/表 注同样是有效锚点
_FIG_CAPTION = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Table\.?|图|表)\s*\d+", re.IGNORECASE | re.MULTILINE
)
# 单行图/表注匹配（redact 时豁免注文本块用）
_FIG_CAPTION_LINE = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Table\.?|图|表)\s*\d+", re.IGNORECASE
)
# 图表区域判定阈值
_MIN_FIG_RATIO = 0.015     # 面积占页面比例下限（过滤图标/装饰线；
                           # 0.03 实测会漏小图片表格——A4 上 150x80pt 的表约 2.5%）
_MAX_FIG_RATIO = 0.92      # 上限（过滤整页背景）
_MAX_FIG_TEXT_CHARS = 2000 # 区域内文本字符上限（兜底：防整页文本框误判；
                           # 矢量图表的轴标签/图例是真实文本，实测可达 1500+）
_FIG_TEXT_COVER_MIN = 0.5  # 版面仲裁（T10 反馈 2）：区域内文本被模型 plain
                           # text 区域覆盖 ≥ 此比例判文本框，快照否决——
                           # 实测全语料真图真表 ≤14%、prompt 示例框 ≥60%，
                           # 0.5 落在空档中点（SubgraphRAG p5 实测 0.596）
_FIG_TEXT_COVER_CHARS = 200  # 仲裁参评的区域内文本下限（小区域覆盖噪声大）
_FIG_SCALE = 2.5           # 快照渲染倍率（与视觉 OCR 一致）
_MERGE_GAP = 12.0          # 区域合并空隙容差（pt）：图表内文字行把绘图簇
                           # 隔开 0~10pt，只并相交矩形会把一张图拆成多条横带
                           # （实测 9.2pt 空隙拆成 4 份）；双栏正文列距 >18pt，
                           # 12pt 不会跨栏误并


def _clean_html(md: str) -> str:
    """数据清理（实测问题：正文中残留 <br>/<sup> 等 HTML 杂质，
    react-markdown 不渲染原始 HTML，会原样露出）。
    - HTML 注释（pymupdf4llm 对隐藏文本会输出 <!-- -->）整段删除；
    - <br> 转空格（表格行内换新行会破坏表格结构，空格最安全）；
    - <sup>/<sub> 转对应 Unicode 上/下标字符；
    - 实体转义还原。"""
    md = _HTML_COMMENT.sub("", md)
    md = _BR_TAG.sub(" ", md)
    md = _U_TAG.sub("", md)
    md = _SUP_TAG.sub(lambda m: m.group(1).translate(_SUP_MAP), md)
    md = _SUB_TAG.sub(lambda m: m.group(1).translate(_SUB_MAP), md)
    md = md.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return md


# ── 伪标题降级（2026-09-08 用户反馈"这两句不是标题"）────────────────────
# pymupdf4llm 的字号启发式会把论文里的大字号强调句（研究问题、引导问句——
# 排版上大号粗斜体但语义是正文）误判成 markdown 标题。双重后果：
# ① 前端 prose h1 渲染出突兀巨字；② 模型把问句当标题处理，引号内保留
# 英文只翻一半（GraphRAG-Bench 实测："Does…retrieval?"原样保留+外面拼
# "的推理能力"）。句子特征明确的"标题"降级为粗体段落：保留强调语义，
# 真标题（编号式/短语式）不受影响。
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
# 去掉强调符号/引号/括号后的"裸文本"（保留空格供词数统计），用于句子特征判定
_HEADING_STRIP = re.compile(r"[*_`\"'\u201c\u201d\u2018\u2019()\[\]]")
# 句子结尾（允许尾随引号/括号）
_SENT_END = re.compile(r"[?！？!.\u3002][\"'\u201d\u2019)]*$")
# 数字编号开头的真标题（"1. Introduction"、"4.2 Method"）
_NUM_HEADING = re.compile(r"^\d+(\.\d+)*[.:）)]?\s")
# 句子证据词（代词/系动词/情态动词/关系与指示词），小写词面精确匹配——
# 学术标题内这些词几乎总以大写出现（"Attention Is All You Need"），
# 正文句子里则是小写（阶段11-T6 后登记的标题缺失根治，2026-09-12）
_SENT_EVIDENCE = re.compile(
    r"\b(we|our|ours|us|i|me|my|you|your|they|their|them|it|its|he|she|his|her"
    r"|is|are|was|were|be|been|being|am|has|have|had|do|does|did"
    r"|will|would|shall|should|can|could|may|might|must"
    r"|that|this|these|those|which|who|whose|whom|there|here)\b"
)


def _demote_sentence_headings(md: str, protect_first: bool = False) -> str:
    """把句子特征明确的 markdown 标题行降级为粗体段落。

    判定（对去符号裸文本）：
    - 以 ?/！/。 类句末标点结尾（允许尾随引号）→ 问句/感叹句，降级；
      但句号结尾需同时 ≥6 词且非数字编号开头（真标题偶带句号，
      如 "1. Introduction."——编号开头或过短的保留）；
    - ≥14 词且带句子证据（小写开头，或含小写句法证据词）才降级。
      2026-09-12 收窄：原先 ≥14 词一律降级，把 DALK 等长学术真标题
      （普遍 >14 词）误降为粗体段——下游续段合并把无终止符的降级标题
      当未完结段链式吞掉后续块（首页粘成一整块），页眉剔除又按
      "与文档标题相同"把降级标题块整体吃掉，标题彻底丢失；
    - protect_first：首页（页 0）首个标题是论文标题的位置证据，
      豁免 ≥14 词规则（句末标点规则仍适用——真标题不以问号结尾）。
    降级产物保留原行内部的全部强调标记，只去掉行首 #。"""

    first = _HEADING_LINE.search(md) if protect_first else None

    def repl(m: re.Match) -> str:
        body = m.group(2)
        plain = _HEADING_STRIP.sub("", body)
        words = len(plain.split())
        demote = False
        if _SENT_END.search(plain):
            # 句号结尾的降级门槛更高：真标题偶带句号（如 "1. Introduction."），
            # 编号开头或 <6 词的保留
            demote = not (
                plain[-1] in ".。\u3002"
                and (_NUM_HEADING.match(plain) or words < 6)
            )
        elif words >= 14:
            is_first = first is not None and m.start() == first.start()
            demote = not is_first and (
                plain[:1].islower() or bool(_SENT_EVIDENCE.search(plain))
            )
        if not demote:
            return m.group(0)
        # body 已被 **..** 整体包裹时直接去 #（再包一层会出 **** 四星）
        if body.startswith("**") and body.endswith("**"):
            return body
        return "**" + body + "**"

    return _HEADING_LINE.sub(repl, md)


# ── 斜体误判上标还原（2026-09-08 用户反馈"公式还是有点问题"）──────────
# DALK EMNLP 版实测：斜体排版的正文单词被 pymupdf4llm 的上标检测误判，
# 输出 Unicode 上标字符——initial→ⁱⁿⁱtⁱal、node→ⁿode、new→ⁿew、
# post-processing→post⁻processⁱⁿg、prune→pruⁿe。渲染后形如
# "post^process^ng"，既难看又毁翻译。判据：词内嵌入上标字符必是误判
# （真上标如 10¹² 跟在数字后、xⁿ 为单变量短 token），按词还原普通字符。
_SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹ⁱⁿ⁻⁺⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"
_SUP_WORD = re.compile(r"[A-Za-z]*[" + _SUP_CHARS + r"]+[A-Za-z" + _SUP_CHARS + r"]*")
_SUP_ANY = re.compile("[" + _SUP_CHARS + "]")


def _fix_italic_superscripts(md: str) -> str:
    """把词内嵌入的 Unicode 上标字符还原为普通字符（NFKC）。

    只处理「长度 ≥4、含 ≥1 个上标字符、含 ≥2 个普通字母」的词 token；
    真上标（10¹²、xⁿ、e⁰ 等短/纯数字形态）保持原样。"""

    def repl(m: re.Match) -> str:
        w = m.group(0)
        n_sup = sum(1 for ch in w if ch in _SUP_CHARS)
        letters = sum(1 for ch in w if ch.isascii() and ch.isalpha())
        if len(w) < 4 or n_sup < 1 or letters < 2:
            return w
        fixed = _SUP_ANY.sub(
            lambda c: unicodedata.normalize("NFKC", c.group(0)), w
        )
        # NFKC 把 ⁻ 映射为数学减号 −，统一回 ASCII 连字符
        return fixed.replace("\u2212", "-")

    return _SUP_WORD.sub(repl, md)


# ── 跨栏粘连段拆分（2026-09-08 用户反馈"摘要混在上一行"）──────────────
# ACL/EMNLP 双栏版首页实测：作者单位区跨栏排版，pymupdf4llm 按 y 带分组
# 会把「左栏单位行 + 行中孤立节标题（**Abstract**）+ 右栏片段」合并成
# 单段，且吸进的右栏片段在页面其它位置还有完整段落（内容重复）。
# 修复：① 段中孤立已知节标题 → 拆成独立标题段（##）；② 段尾片段若在
# 其它段中完整出现（归一化 ≥45 字符）→ 截除（内容已在别处，无损失）。
_KNOWN_SECTIONS = (
    "Abstract", "Introduction", "Related Work", "Background", "Motivation",
    "Methods", "Methodology", "Approach", "Results", "Experiments",
    "Evaluation", "Discussion", "Analysis", "Conclusion", "Conclusions",
    "Limitations", "Acknowledgments", "Acknowledgements", "References",
    "Appendix", "Preliminaries",
)
_INLINE_HEADING = re.compile(
    r"^(.{40,}?)\s+(\*\*(?:" + "|".join(_KNOWN_SECTIONS) + r")\*\*)\s+(\S.*)$",
    re.S,
)
_MD_NORM_S = re.compile(r"[*_`#\s]+")
_LEADING_NUM = re.compile(r"^\d+\s*[.、)．]")
_MIN_TAIL_PLAIN = 30   # 段尾重复片段的最小归一化长度（实测案例 34/41 字符）
_MIN_TAIL_WORDS = 5
_MAX_TAIL_WORDS = 60
_MIN_FRAG_PLAIN = 30   # 独立碎片段（其它段前缀）的最小归一化长度


def _md_plain(s: str) -> str:
    return _MD_NORM_S.sub("", s).lower()


def _split_glued_columns(md: str) -> str:
    paras = md.split("\n\n")
    plains = [_md_plain(p) for p in paras]
    result: list[str] = []
    for i, para in enumerate(paras):
        others = "\n".join(plains[:i] + plains[i + 1:])
        # 1) 行中孤立节标题拆分（标题前 ≥40 字符视为粘连前文）；
        #    拆出的标题升级为 ## 与正常提取产物一致
        m = _INLINE_HEADING.match(para)
        segs = (
            [m.group(1).rstrip(), "## " + m.group(2), m.group(3)]
            if m
            else [para]
        )
        for seg in segs:
            # 结构性标题段（# 开头）豁免规则②③（2026-09-12 FG-RAG 实测）：
            # ACM 引用段/页眉行里合法含有标题全文，标题段的"尾部窗口在其它
            # 段落出现"并非跨栏吸入而是正常引用——据此截除会把整段标题截空；
            # 规则③同理（标题是后续段的自然前缀情形不适用）。标题去留交给
            # 规则①（粘连拆分升级）与 processor 页眉剔除（# 前缀块豁免）。
            is_heading = seg.lstrip().startswith("#")
            # 2) 段尾跨栏重复片段截除：尾部窗口（词数从多到少）归一化后
            #    在其它段落中完整出现 → 该片段是吸进来的右栏内容，
            #    截除（内容已在别处，无损失）；截除后前文过短则整段丢弃
            words = seg.split()
            if not is_heading and len(words) > _MIN_TAIL_WORDS:
                for w in range(min(len(words) - 1, _MAX_TAIL_WORDS),
                               _MIN_TAIL_WORDS - 1, -1):
                    tail_plain = _md_plain(" ".join(words[-w:]))
                    if len(tail_plain) < _MIN_TAIL_PLAIN:
                        continue
                    if tail_plain in others:
                        kept = " ".join(words[:-w]).rstrip()
                        seg = kept if len(_md_plain(kept)) >= 20 else ""
                        break
            plain = _md_plain(seg)
            if not plain:
                continue
            # 3) 独立碎片段去重：剥掉列表标记后是其它段落的更长前缀
            #    （y 带交错截出的残缺重复块，EMNLP 版 DALK p4 实测）
            frag = _LEADING_NUM.sub("", plain)
            if not is_heading and len(frag) >= _MIN_FRAG_PLAIN:
                for j, pl in enumerate(plains):
                    if j != i and pl.startswith(frag) and len(pl) > len(frag) + 10:
                        plain = ""
                        break
            if plain and plain not in [_md_plain(x) for x in result[-3:]]:
                result.append(seg)
    return "\n\n".join(result)


def _merge_rects(rects: list, gap: float = 0.0) -> list:
    """合并相交矩形（迭代至稳定）。区域数量少，O(n²) 可接受。

    gap > 0 时把「空隙不超过 gap」的相邻矩形也并到一起（膨胀探测、
    取原矩形并集，不裁边）。必须有：图表内部的文字行不产生绘图簇，
    簇与簇之间留有几 pt 空隙——只合并相交矩形会把同一张图拆成
    多条横带（2026-09-07 TOG 论文 Figure 被拆 4 份踩坑）。"""
    rects = [pymupdf.Rect(r) for r in rects if r and r.width > 1 and r.height > 1]
    changed = True
    while changed:
        changed = False
        out: list = []
        while rects:
            r = rects.pop()
            probe = (
                pymupdf.Rect(r.x0 - gap, r.y0 - gap, r.x1 + gap, r.y1 + gap)
                if gap > 0
                else r
            )
            for i, o in enumerate(out):
                if probe.intersects(o):
                    out[i] = o | r  # 并集（不膨胀，保住真实边界）
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


def _figure_regions(
    page,
    debug: bool = False,
    extra_regions: list | None = None,
    text_regions: list | None = None,
    caption_regions: list | None = None,
) -> list:
    """检测页面的图/表区域（栅格图 + 矢量绘图簇 + 表格统一处理）。

    表格也按图片快照（2026-09-06 用户决策：文本表格转 markdown 必错位；
    find_tables 的 bbox 直接作为候选区域，borderless/booktabs 表也能命中）。

    extra_regions（阶段12-T9.2）：版面模型（DocLayout-YOLO）的 table/figure
    区域矩形，作为额外候选并入同一条漏斗（合并/面积/文本密度过滤）——
    无框表 find_tables 检不出，靠模型区域补位；与既有候选重叠时由
    _merge_rects 的空隙合并天然去重。

    text_regions（阶段12-T10 反馈 2）：版面模型 plain text 区域，作为最终
    区域的仲裁信号——带边框的文本示例框（ICLR 附录 prompt 案例框，
    SubgraphRAG p22-29 / DALK p17-18 实测）的矢量矩形会被 cluster_drawings
    （有时连模型自己也低置信误判 figure/table，SubgraphRAG p5）当图表，
    快照会吞掉整块正文（truth 扣空 → 交叉校验失明、遮罩搅乱 VLM、降级
    路径 redact 挖空文本层）。仲裁口径 = 区域内文本字符被 plain text
    区域覆盖的比例（块主体 ≥50% 在区域内即计入）——模型检测碎片化的
    附录页上矩形并集盖不满框内留白，字符覆盖才是内容口径。实测全语料
    分离度：真图真表 0~14%，prompt 框 60~100%（见 docs/阶段12 §7.4）。

    caption_regions（阶段12-T10 反馈 7）：版面模型 figure_caption/table_caption
    区域 + 严格注释行文本块（Figure/Table N 开头、块高 ≤45pt）——注释边界
    收夹：注释被卷进快照会被遮罩（VLM 看不见 → 输出缺注释 → 快照插回无
    锚点被甩页尾，SubgraphRAG p2 实测），且注释后的正文跟着被吞。收夹规则
    = 注释贴近哪条边就把那条边收到注释外沿（图注在下方收底、表注在上方
    收顶，同侧多条取最保守）；守卫：收夹不得切割模型 figure/table 区域
    主体，违反即放弃收夹（DALK p8 跨栏合并大块此类，维持现状）。

    过滤规则：
    - 面积占比 [_MIN_FIG_RATIO, _MAX_FIG_RATIO]；
    - 区域内部文本超 _MAX_FIG_TEXT_CHARS 兜底排除（防整页文本框误判；
      注意矢量图表轴标签是真实文本，正常图表可达 1500+ 字符）；
    - text_regions 供仲裁时：内部文本 ≥_FIG_TEXT_COVER_CHARS 且覆盖
      ≥_FIG_TEXT_COVER_MIN 的区域否决（模型说这是正文）。
    返回按 y0 排序的 Rect 列表。debug=True 时打印各环节计数与跳过原因。"""
    page_area = page.rect.width * page.rect.height
    rects: list = []
    try:
        for img in page.get_images(full=True):
            rects.extend(page.get_image_rects(img[0]))
    except Exception:
        pass
    try:
        rects.extend(page.cluster_drawings())
    except Exception:
        pass
    # 表格 bbox 直接作为候选（一次检测；失败不阻断）
    try:
        rects.extend(pymupdf.Rect(t.bbox) for t in page.find_tables().tables)
    except Exception:
        pass
    if extra_regions:
        rects.extend(pymupdf.Rect(r) for r in extra_regions)
    merged = _merge_rects(rects, gap=_MERGE_GAP)
    # 版面仲裁用的 plain text 区域（一次性预计算；空区域剔除）
    trs = (
        [t for t in (pymupdf.Rect(x) for x in text_regions) if not t.is_empty]
        if text_regions
        else []
    )
    if debug:
        print(
            f"[figure] p{page.number + 1}: 候选rect={len(rects)} "
            f"合并后={len(merged)}"
        )
    figs = []
    for r in merged:
        r = r & page.rect  # 裁剪到页面内
        if r.is_empty:
            continue
        ratio = r.width * r.height / page_area
        if ratio < _MIN_FIG_RATIO or ratio > _MAX_FIG_RATIO:
            if debug:
                print(f"[figure] p{page.number + 1}: 跳过(面积比{ratio:.3f}) {r}")
            continue
        try:
            text_chars = len(page.get_text("text", clip=r).strip())
        except Exception:
            text_chars = 0
        if text_chars > _MAX_FIG_TEXT_CHARS:
            if debug:
                print(f"[figure] p{page.number + 1}: 跳过(文本{text_chars}过密) {r}")
            continue  # 文本过密（整页文本框），兜底排除
        if text_regions and text_chars >= _FIG_TEXT_COVER_CHARS:
            covered = 0
            for b in page.get_text("blocks", clip=r):
                n = len((b[4] if len(b) > 4 else "").strip())
                if not n:
                    continue
                br = pymupdf.Rect(b[:4])
                inter = sum((br & t).get_area() for t in trs)
                if inter >= 0.5 * max(br.get_area(), 1.0):
                    covered += n
            if covered >= _FIG_TEXT_COVER_MIN * text_chars:
                if debug:
                    print(
                        f"[figure] p{page.number + 1}: 仲裁否决(正文文本覆盖"
                        f"{covered / text_chars:.0%}) {r}"
                    )
                continue
        figs.append(r)
    figs = _clamp_by_captions(page, figs, extra_regions, caption_regions, debug)
    figs.sort(key=lambda r: (r.y0, r.x0))
    return figs


# 注释行文本块的高度上限（≈3 行）：区分真注释与以 Table N 开头的正文段
_CAPTION_BLOCK_MAX_H = 45.0


def _clamp_by_captions(
    page,
    figs: list,
    model_regions: list | None,
    caption_regions: list | None,
    debug: bool = False,
) -> list:
    """注释边界收夹（T10 反馈 7）：把卷进快照的注释及其后的正文放出
    遮罩/扣除——注释留在文本流里，快照插回才有锚点、注释后才翻得着。

    边界来源：模型 caption 区域（几何可靠）+ 严格注释行文本块（关键词
    Figure/Table N 几何化，模型漏检注释的页兜底）。规则：注释与区域横向
    重叠 ≥50%（同栏）且贴近某条边（在该边 65% 范围内）→ 该边收到注释
    外沿 2pt；同侧多条注释取最保守（收得最少的）边界。守卫：任一模型
    figure/table 区域会被切断（主体越出新边界 >4pt）→ 该区域整体放弃
    收夹（宁可维持现状也不切错）。"""
    caps = [pymupdf.Rect(r) for r in (caption_regions or []) if not pymupdf.Rect(r).is_empty]
    for b in page.get_text("blocks"):
        t = (b[4] if len(b) > 4 else "").strip()
        r = pymupdf.Rect(b[:4])
        if t and _FIG_CAPTION_LINE.match(t) and r.height <= _CAPTION_BLOCK_MAX_H:
            caps.append(r)
    if not caps or not figs:
        return figs
    models = [pymupdf.Rect(r) for r in (model_regions or [])]
    out = []
    for r in figs:
        rw = max(r.width, 1.0)
        bots, tops = [], []
        for c in caps:
            inter = r & c
            if inter.is_empty or inter.width < 0.5 * min(rw, c.width):
                continue  # 不同栏
            if c.y0 >= r.y0 + 0.35 * r.height:
                bots.append(c.y0)  # 贴底 → 底边收到注释上方
            if c.y1 <= r.y0 + 0.65 * r.height:
                tops.append(c.y1)  # 贴顶 → 顶边收到注释下方
        y1 = min(r.y1, max(bots) - 2) if bots else r.y1
        y0 = max(r.y0, min(tops) + 2) if tops else r.y0
        if y1 - y0 <= 0.5 * r.height:
            continue  # 收得过狠（>50%），异常形态放弃
        nr = pymupdf.Rect(r.x0, y0, r.x1, y1)
        # 守卫：与区域重叠 ≥30% 的模型图表区域不得被切断
        cut = any(
            (m & r).get_area() >= 0.3 * max(m.get_area(), 1.0)
            and (m.y1 > y1 + 4 or m.y0 < y0 - 4)
            for m in models
        )
        if cut:
            if debug:
                print(f"[figure] p{page.number + 1}: 注释收夹守卫触发（会切模型区域）{r}")
            out.append(r)
        else:
            if nr != r and debug:
                print(f"[figure] p{page.number + 1}: 注释收夹 {r} -> {nr}")
            out.append(nr)
    return out


def _figure_inner_text_rects(page, regions: list) -> list:
    """返回图表区域内部的文本块 bbox（用于 redact 剔除，2026-09-06 用户反馈）。

    图/表内部的文字（表格数字、轴标签、图例）若按正文提取会变成乱码段落
    且被重复翻译——快照图已"原模原样"包含它们，文本层必须剔除。
    图注文本块（Figure N/图 N 开头）豁免，保住 _insert_figures 的锚点。"""
    out: list = []
    for b in page.get_text("blocks"):
        r = pymupdf.Rect(b[0], b[1], b[2], b[3])
        if r.is_empty:
            continue
        text = (b[4] if len(b) > 4 else "").strip()
        for reg in regions:
            inter = r & reg
            if inter.is_empty:
                continue
            # 块主体（>60% 面积）落在区域内才算图表内部文字
            if inter.get_area() < 0.6 * max(r.get_area(), 1.0):
                continue
            if _FIG_CAPTION_LINE.match(text):
                break  # 图注豁免，保留文本与锚点
            out.append(r)
            break
    return out


def _region_lines(page, region) -> list[dict]:
    """提取区域内逐行文字（bbox/字号/颜色），供"译制图"叠字用。

    坐标为页面坐标（与 region 同系）；图注行豁免——它由正文文本层
    负责翻译，不进译制图。"""
    import json

    lines: list[dict] = []
    try:
        d = page.get_text("dict", clip=region)
    except Exception:
        return lines
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue  # 只取文本块
        for line in blk.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text or _FIG_CAPTION_LINE.match(text):
                continue
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            size = max(s.get("size", 10.0) for s in spans)
            color = spans[0].get("color", 0)
            lines.append(
                {
                    "text": text,
                    "bbox": [round(v, 2) for v in line["bbox"]],
                    "size": round(size, 1),
                    "color": int(color),
                }
            )
    return lines


def _absorb_header_lines(page, r) -> None:
    """把紧邻区域上方的表头文本行并入区域（原地修改 r）。

    实测（HippoRAG Table 2）：表格绘图簇从第一条横线开始，表头
    （MuSiQue | 2Wiki | HotpotQA | Average 及 R@2/R@5 行）悬在簇上方
    不在区域内——redact 漏掉它们，pymupdf4llm 把残余表头识别成小
    markdown 表格残留正文，用户看到表头被"翻译了两遍"。
    判定：区域上方 25pt 内的文本行，按词间隙切分 ≥2 段（表头是多列
    特征，正文行是连续长句）即并入，逐行向上直到不满足。
    """
    words = [w for w in page.get_text("words") if w[0] >= r.x0 - 6 and w[2] <= r.x1 + 6]
    for _ in range(4):  # 最多吸收 4 行（双层表头够了），防失控
        band_top = r.y0 - 25
        cands = [
            w for w in words
            if band_top <= (w[1] + w[3]) / 2 < r.y0
        ]
        if not cands:
            return
        top = min(w[1] for w in cands)
        # 该行按词间隙切段，≥2 段才算表头行
        line = sorted((w for w in cands if w[1] <= top + 3), key=lambda w: w[0])
        segs, cur = 1, line[0]
        for a, b in zip(line, line[1:]):
            if b[0] - a[2] > 10:
                segs += 1
            cur = b
        if segs < 2:
            return
        r.y0 = top - 2


def _snapshot_figures(
    doc,
    page_num: int,
    image_dir: str,
    debug: bool = True,
    extra_regions: list | None = None,
    table_regions: list | None = None,
    text_regions: list | None = None,
    caption_regions: list | None = None,
) -> tuple[list[str], list]:
    """把页面图表区域截图为 PNG，返回 (图片引用列表, 最终区域列表)。

    区域分类（2026-09-06 用户决策）：与 find_tables bbox 重叠 >50% 的判为
    表格，快照命名 tab_*；其余为图，命名 fig_*。表格的译制图改为"全文翻译
    完成后用户点按触发"（按需，不拖慢全文），sidecar 记录 kind 与源 PDF。

    extra_regions/table_regions（阶段12-T9.2）：版面模型的 table/figure 区域
    矩形——前者并入 _figure_regions 候选（无框表补位），后者并入 table_boxes
    参与分类（模型判 table 的区域即使 find_tables 不认也命名 tab_*，
    译制图按钮的语义才成立）。text_regions（T10 反馈 2）：版面模型
    plain text 区域，仲裁几何候选（见 _figure_regions docstring）。

    同时落盘 sidecar JSON（<fig>.json：区域坐标 + 图内逐行文字元数据），
    供译制图叠字使用。

    返回最终区域（外扩+表头吸收后）供 redact 使用——redact 必须与快照
    同一区域，否则表头等悬在簇外的文字只进快照不被抹除，正文残留
    （pymupdf4llm 会把残余表头再识别成小 markdown 表格，用户看到
    "表头翻译了两遍"，HippoRAG 实测）。"""
    import json

    page = doc[page_num]
    try:
        table_boxes = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
    except Exception:
        table_boxes = []
    if table_regions:
        table_boxes.extend(pymupdf.Rect(r) for r in table_regions)
    refs: list[str] = []
    final_regions: list = []
    pad = 3.0  # 快照外扩（pt）：实测 find_tables/绘图簇 bbox 会裁掉表格右缘
    # 最后一个数字（HippoRAG Table 5 "77.4" 只剩半个 "5"），小外扩零风险
    for k, r0 in enumerate(
        _figure_regions(
            page,
            debug=debug,
            extra_regions=extra_regions,
            text_regions=text_regions,
            caption_regions=caption_regions,
        )
    ):
        r = (r0 + (-pad, -pad, pad, pad)) & page.rect
        # 表头吸收：表格绘图簇从第一条横线开始，表头文本行悬在簇上方
        # （见 _absorb_header_lines docstring），并入区域统一截图+redact
        _absorb_header_lines(page, r)
        kind = "figure"
        for tb in table_boxes:
            inter = r & tb
            if not inter.is_empty and inter.get_area() > 0.5 * max(r.get_area(), 1.0):
                kind = "table"
                break
        prefix = "tab" if kind == "table" else "fig"
        path = os.path.join(image_dir, f"{prefix}_p{page_num + 1:03d}_{k:02d}.png")
        try:
            pix = page.get_pixmap(
                clip=r, matrix=pymupdf.Matrix(_FIG_SCALE, _FIG_SCALE)
            )
            pix.save(path)
        except Exception as e:
            # 静默 continue 曾把整条快照管线打空且无任何日志（目录不存在
            # 时 save 必炸，2026-09-07 TOG 论文全页无图踩坑）——必须留痕
            print(f"[figure] p{page_num + 1}: 快照保存失败 {os.path.basename(path)}: {e}")
            continue  # 单个快照失败不阻断整页
        if debug:
            print(f"[figure] p{page_num + 1}: 快照#{k}({kind}) -> {os.path.basename(path)} {r}")
        # sidecar：译制图的原料（区域 + 图内逐行文字 + 分类 + 源 PDF）
        try:
            sidecar = {
                "png": path.replace(os.sep, "/"),
                "page": page_num,
                "region": [round(v, 2) for v in list(r)],
                "lines": _region_lines(page, r),
                "kind": kind,
                "pdf": doc.name,
            }
            with open(path + ".json", "w", encoding="utf-8") as f:
                json.dump(sidecar, f, ensure_ascii=False)
        except Exception:
            pass  # sidecar 失败只影响译制图，不影响快照
        refs.append(f"![Figure]({path.replace(os.sep, '/')})")
        final_regions.append(r)
    return refs, final_regions


# 插图锚点专用（2026-09-07 收紧）：caption 在编号后必带冒号/句点。
# _FIG_CAPTION 不带标点要求，正文里 "Table 4 illustrates ..." 这类普通
# 段落也会命中，快照被插进正文中间（DALK p7 实测：Table 4 快照插在
# "Table 4 illustrates" 段前、Figure 3 快照配错 caption）
_INSERT_ANCHOR = re.compile(
    r"^\s*(?:Figure|Fig\.?|Tab\.?|Table|图|表)\s*\d+\s*[:：.]",
    re.IGNORECASE | re.MULTILINE,
)


def _insert_figures(
    md: str, refs: list[str], snap_regions: list | None = None,
    raw_blocks: list | None = None,
) -> str:
    """把图快照引用插回 markdown（caption 锚定）。

    v2（2026-09-07）：几何配对。序号配对「第 k 快照（页面 y 序）↔ 第 k
    caption（markdown 序）」在两栏页上必错——caption 可能在表格上方，
    且 y 带交错输出会让 caption 的 markdown 序与快照 y 序不一致
    （DALK p7 实测：快照序 tab→fig→tab，caption 序 Table3→Table4→Figure3，
    Figure 3 的图配到了 Table 4 caption 旁）。改为用 raw_blocks 里 caption
    块的坐标与快照区域做「垂直贴近 + 水平重叠」就近匹配，再按 caption
    文本在 markdown 中定位插入点。无几何信息时退回 v1 序号配对。
    """
    if not refs:
        return md
    if snap_regions and raw_blocks:
        out = _insert_figures_geo(md, refs, snap_regions, raw_blocks)
        if out is not None:
            return out
        # 几何配对完全失败（找不到任何 caption 块）→ 退回 v1 序号配对
    marks = [m.start() for m in _INSERT_ANCHOR.finditer(md)]
    # 从后往前插，避免位移
    for k in range(len(refs) - 1, -1, -1):
        ref = f"\n\n{refs[k]}\n\n"
        if k < len(marks):
            pos = marks[k]
            md = md[:pos] + ref + md[pos:]
        else:
            md = md.rstrip("\n") + "\n\n" + refs[k] + "\n"
    return md


# 快照与 caption 的最大垂直距离（pt）：caption 紧贴其图表（上/下），
# 超过这个距离基本是别的栏/别的图表的 caption
_FIG_CAP_GAP = 80.0


def _insert_figures_geo(
    md: str, refs: list[str], snap_regions: list, raw_blocks: list
) -> str | None:
    """几何配对插入。返回 None 表示一个 caption 块都没找到（调用方退回 v1）。"""
    # 1) caption 候选（raw_blocks 中命中锚点正则的块，带坐标）
    caps: list[tuple[pymupdf.Rect, str]] = []
    for b in raw_blocks:
        text = (b[4] or "").strip() if len(b) > 4 else ""
        if text and _INSERT_ANCHOR.match(text):
            caps.append((pymupdf.Rect(b[:4]), text))
    if not caps:
        return None
    # 2) 每个快照找最近 caption（垂直贴近 + 水平重叠，一对一贪心）
    pairs: list[tuple[int, int]] = []  # (快照序 k, caption 序 c)
    used: set[int] = set()
    for k, r in enumerate(snap_regions[: len(refs)]):
        best, best_gap = None, None
        for c, (cr, _) in enumerate(caps):
            if c in used:
                continue
            overlap = min(r.x1, cr.x1) - max(r.x0, cr.x0)
            if overlap <= 0.3 * max(min(cr.width, r.width), 1.0):
                continue  # 不同栏，垂直再近也不配
            if cr.y1 <= r.y0:  # caption 在区域上方（表）
                gap = r.y0 - cr.y1
            elif cr.y0 >= r.y1:  # caption 在区域下方（图）
                gap = cr.y0 - r.y1
            else:
                gap = 0.0  # 有纵向重叠（caption 被区域包住）
            if gap > _FIG_CAP_GAP:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = c, gap
        if best is not None:
            used.add(best)
            pairs.append((k, best))
    # 3) caption 文本 → markdown 段落定位（归一化前缀互为前缀）
    inserts: list[tuple[int, str]] = []  # (md 偏移, ref)
    matched_ks: set[int] = set()
    for k, c in pairs:
        cnorm = _md_norm(caps[c][1])
        if not cnorm:
            continue
        pos = _find_para_pos(md, cnorm)
        if pos is not None:
            inserts.append((pos, refs[k]))
            matched_ks.add(k)
    # 4) 未配到 caption 的快照：追加页末（v1 行为）
    for k in range(len(refs)):
        if k not in matched_ks:
            md = md.rstrip("\n") + "\n\n" + refs[k] + "\n"
    # 5) 从后往前插，避免位移
    for pos, ref in sorted(inserts, key=lambda t: -t[0]):
        md = md[:pos] + f"\n\n{ref}\n\n" + md[pos:]
    return md


def _find_para_pos(md: str, cnorm: str) -> int | None:
    """在 markdown 中找与 caption 文本归一化后互含的段落，返回段首偏移。

    切段方式与 _column_reading_order 一致（\\n\\s*\\n），24 字符前缀互含
    容忍粗体标记/换行差异，同时不会误配 "Table 4 illustrates ..." 正文。"""
    pos = 0
    for para in re.split(r"(\n\s*\n)", md):
        if not re.fullmatch(r"\n\s*\n", para or ""):
            body = para.strip()
            if body:
                pn = _md_norm(body)
                if pn and (
                    pn.startswith(cnorm[:24]) or cnorm.startswith(pn[:24])
                ):
                    return pos + (len(para) - len(para.lstrip()))
        pos += len(para)
    return None


# ── 字号几何证据（阶段12 验收期修复，2026-09-14）─────────────────────
# VLM「OCR:」输出是平文本：无 # 标题层级、首页小字版权块与正文同权
# （FG-RAG 重翻验收实测：全篇零标题、ACM 权限声明混在作者信息里）。
# 修法沿用 title_detect 哲学——回读 PDF 一手字号证据，类级通用。
#
# 判定规则（4 版式语料标定：ACL 双栏/ACM 会议/NeurIPS/ACM 期刊）：
# - 标题行 = 整行粗体（≥80% 字符在粗体 span）且字号 ≥ 正文字号且
#   含 ≥2 个字母（剔纯数字表行）且非图表注（_FIG_CAPTION）。
#   粗体是关键区分器：FG-RAG 作者名比节标题更大但不粗、Attention 的
#   arXiv 侧章与 Abstract 同号但不粗——均被排除；
# - 正文字号基准取**第 0 页众数**（出版模板全文恒定；逐页取会被
#   参考文献页整页小字带偏，FG-RAG p5 实测 body 塌到 7pt）；
# - 字号量化到 0.5pt 网格再比较（PDF span 有 ±0.2 渲染抖动，8.9/9.0/
#   9.1 是同一名义字号，不量化会把众数和比例全搅乱）；
# - 层级：节编号深度定级（"4"→##、"3.4"/"3.2.1"→###，学界通用约定）；
#   无编号时页 0 最大粗体行（≥1.15×）= # 文档标题；非首页的本页最大
#   粗体档 = ##（该页主标题档）；其余按字号档（≥1.45×→#，≥1.15×→##，
#   否则 ###）；
# - 页 0 抑制：只认文档标题/通用节名词汇（Abstract 等，跨论文稳定
#   词汇非单篇特调）/编号节标题——作者行（粗体短行的整簇）被整类剔除；
# - 首页小字印刷（≤0.85×正文且成块 ≥60 字符）= 出版商版权/权限声明
#   （ACM/IEEE/Springer 惯例首页脚注字号；NeurIPS/arXiv 无此块则
#   零候选零误伤）。
_HEAD_TITLE_RATIO = 1.15   # 页 0 文档标题的最小粗体字号比
_HEAD_L1_RATIO = 1.45     # 无编号时 # 档
_HEAD_L2_RATIO = 1.15     # 无编号时 ## 档
_SMALLPRINT_RATIO = 0.85  # 首页小字印刷判定
_SMALLPRINT_MIN_CHARS = 60
_SIZE_QUANT = 2.0         # 量化到 0.5pt 网格（round(size*2)/2）
_BOLD_LINE_RATIO = 0.8    # 整行粗体的字符占比阈值
_HEAD_MAX_LEN = 110       # 标题行长度上限（粗体问题条目/段首引导不提升）
_NUM_DEPTH = re.compile(r"^(\d+(?:\.\d+)*)\s")

# 通用节名词汇（跨论文稳定的结构性标签，从 processor 移入提取层）：
# 页 0 作者抑制的白名单 + 文档标题提取的排除表共用
_GENERIC_HEADINGS = {
    "abstract", "summary", "keywords", "introduction", "related work",
    "background", "motivation", "preliminaries", "preliminary", "methods",
    "methodology", "method", "approach", "experiments", "experimental setup",
    "results", "evaluation", "discussion", "conclusion", "conclusions",
    "future work", "references", "acknowledgments", "acknowledgements",
    "appendix", "contributions", "overview", "contents", "摘要", "关键词",
    "引言", "背景", "方法", "实验", "结果", "讨论", "结论", "参考文献",
    "附录", "目录",
}


def _is_generic_heading_text(text: str) -> bool:
    """去编号/强调符后是否通用节名。"""
    t = re.sub(r"^\d+(\.\d+)*\s+", "", text.replace("*", "").strip())
    return t.rstrip(":：").strip().lower() in _GENERIC_HEADINGS


def _quant(size: float) -> float:
    return round(size * _SIZE_QUANT) / _SIZE_QUANT


def _line_bold_ratio(spans: list) -> float:
    """行的粗体字符占比（整行粗体才算标题候选，run-in 引导词只有局部粗体）。"""
    total = bold = 0
    for s in spans:
        n = len(s.get("text", "").strip())
        if n <= 0:
            continue
        total += n
        if (s.get("flags", 0) & 16) or "bold" in (s.get("font") or "").lower():
            bold += n
    return bold / max(total, 1)


def collect_font_evidence(
    page, is_first_page: bool = False, body_size: float | None = None
) -> dict:
    """收集页面的字号证据：正文字号、标题行（含层级）、首页小字印刷块。

    body_size 传入文档级正文基准（第 0 页众数，逐页调用时由调用方算好
    传入）；缺省时用本页众数（单页调用/测试用）。返回
    {"body_size": float, "headings": [(norm, level)], "smallprint": [norm]}。"""
    info = page.get_text("dict")
    lines: list[dict] = []
    for block in info.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, _dy = line.get("dir", (1, 0))
            if abs(dx) < 0.9:
                continue  # 竖排行剔除（arXiv 印章等）
            spans = [
                s
                for s in line.get("spans", [])
                if s.get("text", "").strip() and s.get("size", 0) > 0
            ]
            if not spans:
                continue
            text = " ".join(s["text"].strip() for s in spans).strip()
            norm = _md_norm(text)
            if not norm:
                continue  # 纯符号行；短行（含编号片段 '4'）保留参与同行合并
            lines.append(
                {
                    "text": text,
                    "norm": norm,
                    "size": _quant(max(s["size"] for s in spans)),
                    "bold": _line_bold_ratio(spans) >= _BOLD_LINE_RATIO,
                    "y0": line["bbox"][1],
                    "x0": line["bbox"][0],
                }
            )
    if not lines:
        return {"body_size": 0.0, "headings": [], "smallprint": []}

    if body_size is None:
        weight: dict[float, int] = {}
        for ln in lines:
            weight[ln["size"]] = weight.get(ln["size"], 0) + len(ln["norm"])
        body = max(weight, key=weight.get)
    else:
        body = _quant(body_size)

    # 标题候选：整行粗体 + 字号 ≥ 正文 + 非图表注 + 长度克制
    # （纯编号行 '4.1' 允许入选——它要和同行标题文本合并还原编号，
    # 合并后仍纯数字的组再丢弃）
    def _heading_line(ln: dict) -> bool:
        return (
            ln["bold"]
            and ln["size"] >= body
            and not _FIG_CAPTION.match(ln["text"])
            and len(ln["text"]) <= _HEAD_MAX_LEN
            # 标题不以句号结尾：句号收尾的粗体行是表内强调句/run-in 引导
            # 词（"Datasets." / "LLMs when solving ..." 实测）；问号/叹号
            # 标题（问题式节标题）不受影响
            and not ln["text"].rstrip().endswith((".", "。"))
        )

    cands = sorted(
        (ln for ln in lines if _heading_line(ln)),
        key=lambda ln: (ln["y0"], ln["x0"]),
    )
    # 排版惯例：标题首字符必为大写/数字/CJK——小写开头的粗体行是
    # 表格单元格/正文片段（FG-RAG p3 表行实测）。纯数字行（编号片段）
    # 允许进入下一步与同行标题文本合并。
    cands = [
        ln
        for ln in cands
        if (
            ln["text"][0].isupper()
            or ln["text"][0].isdigit()
            or "\u4e00" <= ln["text"][0] <= "\u9fff"
        )
    ]
    # 同行合并（编号与标题文本常被 span 拆成两段：'4' + 'Experiments'）
    rows: list[list[dict]] = []
    for ln in cands:
        if rows and abs(ln["y0"] - rows[-1][-1]["y0"]) <= 2.0:
            rows[-1].append(ln)
            continue
        rows.append([ln])
    # 表列头剔除：同一行 ≥3 个并排粗体组 = 表格列头（真实标题不会
    # 同行并排三个；双栏页左右各一标题只有 2 个，不受影响）
    rows = [r for r in rows if len(r) < 3]
    # 换行合并（两行标题）：下组不以编号开头才并（否则是下一个标题）
    merged: list[list[dict]] = []
    for r in rows:
        head = sorted(r, key=lambda ln: ln["x0"])
        if merged:
            prev = merged[-1][-1]
            nxt_starts_num = bool(_NUM_DEPTH.match(head[0]["text"]) or re.match(r"^\d+(\.\d+)*$", head[0]["text"]))
            if (
                abs(head[0]["size"] - prev["size"]) <= 0.5
                and 0 < head[0]["y0"] - prev["y0"] <= 1.8 * max(head[0]["size"], prev["size"])
                and not nxt_starts_num
            ):
                merged[-1].extend(head)
                continue
        merged.append(head)
    max_bold_size = max((g[0]["size"] for g in merged), default=0.0)
    # 合并后仍纯数字/符号的组（孤立编号行，没找到同行标题文本）丢弃
    merged = [
        g
        for g in merged
        if len(re.sub(r"[^a-zA-Z\u4e00-\u9fff]", "", " ".join(ln["text"] for ln in g))) >= 2
    ]
    headings = []
    for group in merged:
        size = max(ln["size"] for ln in group)
        text = " ".join(ln["text"] for ln in group).strip()
        norm = _md_norm(text)
        if len(norm) < 4:
            continue
        m = _NUM_DEPTH.match(text)
        if m:  # 编号深度定级（学界通用约定）
            level = 2 if m.group(1).count(".") == 0 else 3
        elif is_first_page and size >= body * _HEAD_TITLE_RATIO and size >= max_bold_size - 0.5:
            level = 1  # 页 0 最大粗体行 = 文档标题
        elif not is_first_page and size >= max_bold_size - 0.5:
            level = 2  # 非首页的本页主标题档（如 ACL 的节标题 1.09×）
        elif size >= body * _HEAD_L1_RATIO:
            level = 1
        elif size >= body * _HEAD_L2_RATIO:
            level = 2
        else:
            level = 3
        # 页 0 抑制：除文档标题/通用节名/编号节外，粗体短行整类是作者信息
        if (
            is_first_page
            and level != 1
            and not _NUM_DEPTH.match(text)
            and not _is_generic_heading_text(text)
        ):
            continue
        headings.append((norm, level))

    # 首页小字印刷块：版权/权限声明区（合并相邻小字行为块）
    smallprint: list[str] = []
    if is_first_page:
        smalls = sorted(
            (ln for ln in lines if ln["size"] <= body * _SMALLPRINT_RATIO),
            key=lambda ln: (ln["y0"], ln["x0"]),
        )
        blocks: list[list[dict]] = []
        for ln in smalls:
            if blocks:
                prev = blocks[-1][-1]
                if ln["y0"] - prev["y0"] <= 2.2 * max(ln["size"], prev["size"]):
                    blocks[-1].append(ln)
                    continue
            blocks.append([ln])
        for blk in blocks:
            norm = _md_norm(" ".join(ln["text"] for ln in blk))
            if len(norm) >= _SMALLPRINT_MIN_CHARS:
                smallprint.append(norm)
    return {"body_size": body, "headings": headings, "smallprint": smallprint}


def apply_font_evidence(md: str, evidence: dict) -> str:
    """把字号证据套到 VLM 平文本输出上（幂等，纯函数）：

    - 命中标题行（归一化前缀互含 + 段落足够短防误提升正文）→ 加 #/##/###；
    - 命中首页小字印刷块 → 整段剔除（版权/权限声明不翻译不展示）。"""
    headings = evidence.get("headings") or []
    smallprint = evidence.get("smallprint") or []
    if not headings and not smallprint:
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
        norm = _md_norm(body)
        if norm and any(
            norm.startswith(s[:24]) or s.startswith(norm[:24]) for s in smallprint
        ):
            out.append("")  # 小字印刷块剔除（保留分隔符占位，join 前过滤）
            continue
        if not body.startswith("#"):
            for hnorm, level in headings:
                if not hnorm:
                    continue
                # 双向前缀匹配 + 长度约束：段落要么是标题的前缀（更短），
                # 要么只比标题长极少（防「以标题词开头的长正文段」误提升）
                if hnorm.startswith(norm[:24]) or (
                    norm.startswith(hnorm[:24]) and len(norm) <= len(hnorm) + 20
                ):
                    body = "#" * level + " " + body
                    break
        out.append(body)
    md = "".join(out)
    # 剔除段留下的空段收敛（三个以上连续换行压回两个）
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _normalize_image_refs(md: str, image_dir: str) -> str:
    """把 pymupdf4llm 生成的图片引用规整为「正斜杠绝对路径」，
    前端据此构造资源 URL（Tauri convertFileSrc / 浏览器 file/raw 接口）。"""
    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        base = os.path.basename(src.replace("\\", "/"))
        path = os.path.join(image_dir, base)
        if os.path.isfile(path):
            return f"![{alt}]({path.replace(os.sep, '/')})"
        return m.group(0)

    return _IMG_REF.sub(repl, md)


def count_pages(file_path: str) -> int:
    """返回 PDF 页数（纯本地毫秒级）。"""
    doc = pymupdf.open(file_path)
    try:
        return len(doc)
    finally:
        doc.close()


def _strip_broken_links(page) -> None:
    """get_links 崩溃时清空页 /Annots（2026-09-14 阶段12 实测）。

    症状：apply_redactions 删除重叠注解后可能留下悬空链接项，随后
    pymupdf4llm 遍历 page.get_links() 提超链接时迭代器越界
    （IndexError），整页提取直接崩（Attention p10 / Survey p5 实测，
    均发生在 redact 副本分支；原本注解就损坏的 PDF 同理）。
    处理：get_links 正常的页不动（保留超链接提取）；崩溃的页清空
    /Annots——只丢该提取副本上的超链接/批注，正文不受影响。
    redact 分支必须在 apply_redactions 之后调用（崩溃源即其副作用）。"""
    try:
        page.get_links()
        return
    except Exception:
        pass
    try:
        page.parent.xref_set_key(page.xref, "Annots", "[]")
    except Exception:
        pass  # 仍失败则交给上层异常处理


def extract_page_md(
    doc,
    file_path: str,
    pno: int,
    image_dir: str | None,
    refs: list[str],
    snap_regions: list,
) -> str:
    """单页文本层提取（pymupdf4llm + 全部清洗/插图锚定/列序链）。

    从 extract_pages 拆出（阶段12-T2）：快照由调用方先行完成并传入
    （refs/snap_regions）——VLM 主路线的降级路径与文本层路线必须共用
    同一份 _snapshot_figures 产物，遮罩、truth 扣除、redact 三者同区域
    才自洽。返回 markdown（可能为空，页有效性判定归调用方）。"""
    # 1) 提取文本：有图表区域的页在 redact 副本上提取
    page = doc[pno]
    # 链接注解损坏的页先剥离（否则 pymupdf4llm 的 get_links 崩，见
    # _strip_broken_links；正文不受影响，只丢超链接）
    _strip_broken_links(page)
    regions = snap_regions if refs else []
    inner = _figure_inner_text_rects(page, regions) if regions else []
    raw_blocks: list = []
    if inner and refs:
        with pymupdf.open(file_path) as doc2:
            page2 = doc2[pno]
            for r in inner:
                page2.add_redact_annot(r)
            try:
                page2.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
            except TypeError:
                page2.apply_redactions()
            # redaction 副作用可能留下悬空链接项（见 _strip_broken_links），
            # 必须在 apply_redactions 之后、to_markdown 之前剥
            _strip_broken_links(page2)
            chunks = pymupdf4llm.to_markdown(doc2, page_chunks=True, pages=[pno])
            # 阅读顺序重排的坐标基准必须与提取源一致（redact 后的
            # 副本），且必须在 doc2 存活期内取块（close 后 page 失效）
            raw_blocks = page2.get_text("blocks")
        print(f"[figure] p{pno + 1}: redact 图内文本块 {len(inner)} 个")
    else:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, pages=[pno])
        raw_blocks = page.get_text("blocks")
    c = chunks[0] if chunks else {}
    md = (c.get("text") or "").strip()
    if not md:
        return ""
    md = _clean_html(md)
    # 斜体误判上标还原（DALK 实测 ⁱⁿⁱtⁱal/ⁿode/post⁻processⁱⁿg）
    md = _fix_italic_superscripts(md)
    # 伪标题降级：大字号强调句被误判成标题会巨字渲染+翻译劣化。
    # 页 0 首个标题是论文标题的位置证据，豁免长标题降级
    # （DALK/FG-RAG 标题丢失根治，2026-09-12）
    md = _demote_sentence_headings(md, protect_first=(pno == 0))
    # 跨栏粘连段拆分：单位行+行中 Abstract 标题+右栏片段合成一段
    md = _split_glued_columns(md)
    if image_dir:
        md = _normalize_image_refs(md, image_dir)
        # 先锚定插图（此时 markdown 仍是页面 y 带顺序），再列重排。
        # v2 几何配对：用 caption 块坐标就近匹配快照区域，
        # 序号配对在双栏页上会把图配错 caption（DALK p7 实测）
        md = _insert_figures(md, refs, snap_regions=snap_regions, raw_blocks=raw_blocks)
    # 列重排：图片引用段跟随其 caption 同进同退
    md = _column_reading_order(raw_blocks, md)
    return md


def extract_pages(
    file_path: str, page_nums: list[int], image_dir: str | None = None
) -> list[str | None]:
    """提取指定页的文本层 Markdown（页级流式提取的基础，阶段1-T5）。

    page_nums 按序提取，返回等长列表；无有效文本层的页为 None。
    图片策略（用户反馈"图片原模原样显示"，2026-09-06）：
    - 不再使用 pymupdf4llm 的 write_images（栅格图常被拆成几十张碎片）；
    - 统一走 _snapshot_figures：检测「栅格图区域 + 矢量绘图簇」（合并、
      面积/文本密度过滤），高清截图后按 caption 锚定插回 markdown——
      矢量图表（LaTeX/绘图导出）也能原样显示。
    - 图表内部文字 redact 剔除（用户反馈"图片被转成文字重复翻译"，同日）：
      在文档副本上对区域内的文本块做 redaction（图注豁免），pymupdf4llm
      改在副本上提取——图内文字只存在于快照图中，不再变成乱码段落。
    单页提取约 0.5-1s（表格检测+redact 副本），上层逐页调用实现首页秒开。
    """
    doc = pymupdf.open(file_path)
    try:
        # 规整为真实长路径：调用方传入 8.3 短路径（如 TEMP 目录的 ADMINI~1）
        # 时，生成的引用在部分系统上无法解析（实测踩坑）
        if image_dir:
            image_dir = os.path.realpath(image_dir)
            # 目录必须存在：pix.save 对不存在的目录抛错，曾把整个图表
            # 快照管线静默打空（目录由调用方建时新文件首跑必炸，实测踩坑）
            os.makedirs(image_dir, exist_ok=True)
        out: list[str | None] = []
        for pno in page_nums:
            # 快照先行（渲染含图内文字，所见即所得）。最终区域（外扩+
            # 表头吸收）传给 extract_page_md——redact 必须用同一区域，
            # 否则快照里有的文字在正文残留（表头翻译两遍，实测踩坑）
            refs, snap_regions = (
                _snapshot_figures(doc, pno, image_dir) if image_dir else ([], [])
            )
            md = extract_page_md(doc, file_path, pno, image_dir, refs, snap_regions)
            # 有快照的页即使文字变短也算有效文本层（文字进了图，不回退 OCR）
            out.append(md if (len(md) >= MIN_TEXT_CHARS or refs) else None)
        return out
    finally:
        doc.close()
