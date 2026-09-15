import asyncio
import hashlib
import os
import re
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from ocr import vlm_parse
from ocr.layout_model import LayoutProvider
from ocr.siliconflow import call_ocr, split_into_blocks
from ocr.textlayer import (
    extract_pages,
    count_pages,
    MIN_TEXT_CHARS,
    _GENERIC_HEADINGS,
)
from translate.base import translate_batch, translate_text
from translate.providers.openai_compat import PROMPT_VERSION
from translate import sanitize
from cache.file_cache import (
    file_hash,
    text_hash,
    ocr_key,
    translate_key,
    read_cache,
    write_cache,
)

# 最大并发翻译块组数（每组一次合并请求）。8 并发 × 10 段/组，
# 免费档实测可承载；失败自动逐条回退，不影响正确性。
MAX_CONCURRENCY = 8

# 文本层提取结果在 OCR 缓存中的伪模型名（与视觉模型缓存隔离）。
# v2：提取内容增加 HTML 清理（<br>/<sup>/注释），旧缓存含脏数据需失效。
# v3：图片策略改为图表区域快照（矢量图/碎栅格统一截图插回），markdown 内容变化。
# v4：图表内部文字 redact 剔除（不再与快照图重复），markdown 内容变化。
# v5：表格也按快照处理（文本表格转 markdown 必错位，用户决策），markdown 变化。
# v6：快照按类型命名（tab_*/fig_*），sidecar 记录 kind 与源 PDF，markdown 变化。
# v7：修复快照目录未创建导致 save 全部静默失败（首跑新文件无图无快照，TOG 实测）。
# v8：区域合并加空隙容差（图内文字行隔开的绘图簇不再把一张图拆成多条横带）。
# v9：清理 <u> 下划线标签（用户反馈"下划线还在"），markdown 内容变化。
# v10：快照区域外扩 3pt（表格右缘数字被裁，HippoRAG Table 5 实测），快照内容变化。
# v11：表格快照吸收上方表头行且 redact 与快照同区域（表头在正文残留被
#      pymupdf4llm 再识别成小 markdown 表格，用户看到"表头翻译两遍"）。
# v12：双栏页列感知阅读顺序重排（左栏→右栏；用户反馈"原文不全"：
#      pymupdf4llm 按 y 带交错输出，段落续文被排离原段且页末脚注挡住
#      旧版跨页合并），markdown 段落顺序变化。
# v13：插图锚定收紧（caption 须带标点，正文 "Table 4 illustrates" 不再
#      误当锚点）且改在列重排**前**执行（重排打乱第 k↔第 k 配对，实测
#      Figure 3 快照配到 Table 4 caption），图片段随 caption 一起重排。
# v15：伪标题降级（2026-09-08 用户反馈"这两句不是标题"）——大字号强调句
#      （研究问句等）被 pymupdf4llm 误判成 # 标题：巨字渲染+模型当标题
#      翻一半。句子型标题降级为粗体段落，旧缓存需失效重提。
# v16：斜体误判上标还原（ⁱⁿⁱtⁱal→initial 等）+ 跨栏粘连段拆分
#      （EMNLP 版首页"摘要混在单位行"实测）。
# v16→v17（2026-09-12 标题缺失根治）：textlayer 降级规则收窄（句法证据 +
# 页 0 首标题豁免）与跨栏拆分标题段豁免——两处变更都改变提取产物，
# 旧文本层缓存必须整体失效重建；翻译缓存按 text_hash 键控，未变化文本
# 命中旧译文，仅标题等变化块重译一次
# v17→v18（2026-09-13 提取栈钉扎）：pymupdf 1.28.2→1.26.4 / pymupdf4llm
# 1.28.2→0.0.27，提取产物回归 classic 基线，旧文本层缓存整体失效重建
# v18→v19（2026-09-14 阶段12-T4 识别引擎混合架构）：数字页主路线换
# 「快照+遮罩 → PaddleOCR-VL 整页结构化解析 → 交叉校验降级」（公式定界
# LaTeX/复杂版式完整度提升，见 docs/VLM结构化解析对比.md），textlayer
# 变为降级路径；翻译缓存按 text_hash 键控，未变化文本免重译
# v19→v20（2026-09-14 阶段12-T9.2 版面模型信号源）：DocLayout-YOLO 区域
# 接入——abandon 遮罩+truth 扣除+输出剔除（版权/venue 段不再混入）、
# title 提升编号定级（标题层级恢复）、table/figure 并入快照候选（无框表
# 不再碎行混正文）；提取产物结构性变化，旧文本层缓存整体失效重建
# v20→v21（2026-09-15 阶段12-T10 验收反馈）：①列感知重排三处修复——段落
# ↔块匹配锚定（表格残块子串垃圾命中）、少数失配段跟随邻段（单字符措辞
# 漂移不再令整页放弃重排）、版面 plain text 区域作栏判定兜底（右栏整栏
# 一块的首页判得出双栏），DALK 首页标题/引言乱序根治；②快照区域版面仲裁
# ——prompt 示例框（SubgraphRAG p22-29/DALK p17-18 实测）不再被误判图表
# 快照吞掉正文。翻译缓存按 text_hash 键控，未变化文本免重译
TEXT_LAYER_MODEL = "text-layer-v21"

# 视觉 OCR 缓存版本后缀。v2：OCR 结果顶部插入整页快照（扫描页图片/表格可见），
# 旧缓存无快照需失效——会使扫描页重跑一次视觉 OCR（产生一次 API 调用）。
VISION_CACHE_SUFFIX = "@v2"


def _single_block(page: int, text: str) -> dict:
    return {
        "block_id": 0,
        "page": page,
        "original": text,
        "translated": "",
        "position": {"y_start": 0, "y_end": 0},
    }

_jobs: dict[str, dict] = {}

# ── job 生命周期（阶段1-T4）──────────────────────────────────────────
# 旧实现 _jobs 只增不减：每处理一份 PDF 就多一条含全部页面文本的记录，
# 长期运行必然内存泄漏。策略：容量上限 + TTL 淘汰，且只淘汰已完成的 job
#（运行中/轮询中的一律保留，前端 30 分钟轮询窗口内的任务绝不被淘汰）。
MAX_JOBS = 50
JOB_TTL_SECONDS = 2 * 60 * 60  # 完成后保留 2 小时，供前端/调试复查

# 阶段11-T2 子集：全局翻译任务并发上限（前端 translationManager 本就收口
# 为 1，此守卫防多客户端/绕过前端场景叠加；超出后端直接拒绝并提示）
MAX_RUNNING_JOBS = 2


def running_job_count() -> int:
    return sum(1 for j in _jobs.values() if j.get("status") == "running")


def list_running_jobs() -> list[dict]:
    """运行中任务列表（阶段11-T5）：前端 F5 刷新丢失 job_id 后，
    据此发现后端仍在跑的任务并自动重接管（attach）。"""
    return [
        {
            "job_id": jid,
            "file_path": j.get("file_path") or "",
            "progress": j.get("progress", 0),
        }
        for jid, j in _jobs.items()
        if j.get("status") == "running"
    ]


def _evict_jobs() -> None:
    now = time.time()
    # TTL：超期且已完成
    expired = [
        jid
        for jid, j in _jobs.items()
        if j["status"] in ("done", "failed")
        and j.get("finished_at")
        and now - j["finished_at"] > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _jobs[jid]
    # 容量：超额时淘汰最旧已完成的
    if len(_jobs) > MAX_JOBS:
        finished = sorted(
            (jid for jid, j in _jobs.items() if j["status"] in ("done", "failed")),
            key=lambda jid: _jobs[jid].get("finished_at") or 0,
        )
        for jid in finished:
            if len(_jobs) <= MAX_JOBS:
                break
            del _jobs[jid]


async def run_pipeline(file_path: str) -> dict:
    """
    启动一次处理任务。
    - 同一文件（内容哈希 + mtime）重复提交时复用已有任务，避免重复扣费。
    - 返回 job_id，由前端轮询 get_pipeline_status 获取结果。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 无 Key fail-fast（2026-09-09 用户问询"默认没配 Key 有没有异常处理"）：
    # 缺任一 Key 直接拒绝启动，不让任务跑起来再失败——管线各环节的
    # per-block 容错（_translate_chunk 减半重试/单段落空、figtranslate
    # 保留原文）会把异常吞成空译文，表现为"翻译完成却没有译文"的静默错误。
    if not (settings.ocr_config.get("api_key") or "").strip():
        raise ValueError("OCR API Key 未配置，请先在设置中配置后再翻译")
    if not (settings.translate_config.get("api_key") or "").strip():
        raise ValueError("翻译 API Key 未配置，请先在设置中配置后再翻译")

    _evict_jobs()

    pdf_hash = file_hash(file_path)
    job_id = f"{pdf_hash[:16]}_{int(os.path.getmtime(file_path))}"

    existing = _jobs.get(job_id)
    if existing:
        # 已完成或仍在跑，直接复用，不重复计费
        return {
            "job_id": job_id,
            "status": existing["status"],
            "progress": existing["progress"],
            "reused": True,
        }

    _jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "pages": [],
        "error": None,
        # 阶段11-T5：F5 后前端丢 job_id，凭 file_path 在 running 列表里发现并重接管
        "file_path": file_path,
        "stats": {
            "ocr_cache_hit": 0,
            "ocr_total": 0,
            "tr_cache_hit": 0,
            "tr_total": 0,
            # 阶段12-T4：VLM 主路线页数 / 交叉校验降级页数（前端进度页可见）
            "vlm_pages": 0,
            "vlm_fallback": 0,
        },
    }

    asyncio.create_task(_process_pipeline(file_path, job_id, pdf_hash))

    # 阶段11-T5 扩展（用户方案）：上传即入库——此前翻译完成才登记，
    # 进行中的文档在文献库"消失"（F5/关开应用后无从发现）。早期登记的
    # doc_id 与完成时一致（pdf_hash[:16]），完成 upsert 按 doc_id 原地补全。
    try:
        import docs_index

        docs_index.upsert_doc(
            settings.data_dir,
            {
                "doc_id": pdf_hash[:16],
                "title": os.path.splitext(os.path.basename(file_path))[0],
                "file_path": os.path.abspath(file_path),
                "pdf_hash": pdf_hash,
                "file_mtime": int(os.path.getmtime(file_path)),
                "status": "translating",
            },
        )
    except Exception as e:
        print(f"[docs_index] 早期登记失败（不阻断主链路）: {e}")

    return {"job_id": job_id, "status": "running", "progress": 0, "reused": False}


def _split_page(page: dict) -> dict:
    """将一页 OCR 结果（整页 markdown 单个 block）切成段/句级多个 block。"""
    src_blocks = page.get("blocks", [])
    full_text = src_blocks[0]["original"] if src_blocks else ""
    parts = split_into_blocks(full_text)
    blocks = [
        {
            "block_id": i,
            "page": page["page"],
            "original": p,
            "translated": "",
            "position": {"y_start": 0, "y_end": 0},
        }
        for i, p in enumerate(parts)
    ]
    return {"page": page["page"], "blocks": blocks}


# ── 跨页/跨栏段落续接合并（阶段2-T3，2026-09-07 升级）────────────────
# 块切分按页/栏边界切断段落 → 残文两处显示。旧版只合并「前页末块+后页
# 首块」，但页末常是脚注/页眉等结构性块，真正被切断的段落在中间，永远
# 轮不到合并（DALK 首页实测）；跨栏切分（左栏底→右栏顶）同理。
# 升级为对整个文档块序列做续段合并：
#   A 未完结（末尾无终止符）时，向后跳过「可跳结构块」（图表快照/caption/
#   脚注/表格行/列表，≤8 个）找合并目标；章节标题（# ##）是硬边界——
#   跨过标题续接几乎必错，遇到即放弃。目标满足其一才合并：小写开头
#   （跨页续文最常见形态）；A 以连字符结尾且目标小写连读（断词跨边界）；
#   A 以虚词结尾且目标大写开头（"rich sources of | AD knowledge" 形态）。
#   合并归属 A 的页。
# caption（"Table 5: ..."）必须带标点才判结构块——"Table 4 illustrates"
# 是正文段落（DALK 实测），不能误伤。
_BLOCK_TERMINAL = tuple(".!?:。！？：；;\"')]）】")
_STRUCT_START = re.compile(
    r"^(#{1,6}\s|\||!\[|[-*+]\s|\d+[.)]\s|>"
    r"|(?:Figure|Fig\.?|Tab\.?|Table|图|表)\s*\d+\s*[:：.])",
    re.IGNORECASE,
)
_SKIP_STOP = re.compile(r"^#{1,2}\s")  # 章节标题：合并的硬边界
_MERGE_SKIP_MAX = 8  # 最多跳过的结构块数（图表密集页实测需 4+）
# 列表项可作为合并源（不能当合并目标）：Survey 实测列表项段落跨页续写
# （"- We delineate ... discussing both" + 次页 "the progress and ..."），
# 一刀切排除会让列表项永远残缺
_LIST_ITEM = re.compile(r"^[-*+]\s")
# 英文虚词结尾 = 句子被拦腰切断的强信号（介词/冠词/连词/限定词收尾）
_FUNCTION_TAIL = re.compile(
    r"\b(of|the|and|in|to|a|an|for|with|on|by|at|from|or|as|its|their|his|her|"
    r"our|these|those|this|that|which|who|whose|whom|also|be|is|are|was|were|"
    r"been|than|then|into|over|under|between|through|during|without|within|"
    r"along|across|after|before|above|below|about|when|while|but)\s*$",
    re.IGNORECASE,
)


def _is_plain(t: str) -> bool:
    """可参与续段合并的正文块：非图片、非结构块（标题/列表/表格/caption/脚注）。"""
    return bool(t) and not _PURE_IMAGE.match(t) and not _STRUCT_START.match(t)


def _merge_cross_page(pages: list) -> list:
    """全文续段合并（原地修改 pages，返回同一引用）。"""
    # 平铺序列持有引用：合并 = A 追加目标文本 + 目标从所属页移除
    seq: list[tuple[dict, dict]] = [
        (pg, b) for pg in pages for b in pg.get("blocks", [])
    ]
    i = 0
    while i < len(seq):
        o1 = (seq[i][1].get("original") or "").strip()
        if (
            (not _is_plain(o1) and not _LIST_ITEM.match(o1))
            or o1.endswith(_BLOCK_TERMINAL)
        ):
            i += 1
            continue
        # 向后找合并目标：可跳结构块（图表/caption/脚注等）透明越过，
        # 章节标题 = 硬边界（跨标题续接必错，放弃 A），最多跳 8 个
        j = i + 1
        while j < len(seq):
            oj = (seq[j][1].get("original") or "").strip()
            if _is_plain(oj):
                break
            if _SKIP_STOP.match(oj) or j - i > _MERGE_SKIP_MAX:
                j = len(seq)  # 硬边界或跳太远：放弃
                break
            j += 1
        if j >= len(seq):
            i += 1
            continue
        o2 = (seq[j][1].get("original") or "").strip()
        if o1.endswith("-"):
            joinable = bool(re.match(r"^[a-z]", o2))  # 断词只接小写连读
        elif re.match(r"^[a-z]", o2):
            joinable = True
        else:
            joinable = bool(re.match(r"^[A-Z]", o2)) and bool(
                _FUNCTION_TAIL.search(o1)
            )
        if not joinable:
            i += 1
            continue
        merged = (o1[:-1] + o2) if o1.endswith("-") else (o1 + " " + o2)
        seq[i][1]["original"] = merged
        pg_b, b_b = seq[j]
        pg_b["blocks"].remove(b_b)
        seq.pop(j)
        # 不 i+=1：合并后 A 仍可能未完结（连环续段），下一轮继续找
    return pages


# 纯图片块（图表快照插入产生的 ![Figure](path)）：
# 原文块=原图快照；译文块=译制图（原排版+图内文字译文，见 ocr/figtranslate.py）
_PURE_IMAGE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")
# 从图片引用中提取本地路径
_IMG_PATH = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")

# 图表"译制图"开关（2026-09-06 用户决策：先取消，图表全部用原图）。
# figtranslate.py 实现保留，置 True 可重新启用（左右对照=左原图右译图）。
FIGURE_TRANSLATION_ENABLED = False


async def _translate_figure_block(original_md: str, file_path: str, t_cfg: dict) -> str | None:
    """对图表快照块生成译制图。失败返回 None（前端回退显示原图）。"""
    from ocr.figtranslate import translate_figure

    m = _IMG_PATH.match(original_md)
    if not m:
        return None
    png = m.group(1)
    sidecar = png + ".json"
    if not os.path.isfile(sidecar):
        return None
    try:
        zh = await translate_figure(sidecar, file_path, t_cfg)
        if zh and os.path.isfile(zh):
            return f"![Figure]({zh.replace(os.sep, '/')})"
    except Exception as e:
        print(f"[figure] 译制图生成失败（回退原图）: {e}")
    return None


# 通用章节名（不区分大小写）：标题提取时跳过，防止「Abstract」这类
# 被 pymupdf4llm 误判成顶级标题的章节头混进文档标题（2026-09-09 用户反馈）
# 通用节名词汇（标题几何证据/作者抑制共用）已移至提取层 ocr/textlayer.py
# （阶段12 验收期修复），此处导入使用；_is_generic_heading 保留多级编号
# 章节头直接判非标题的文档标题语义。


def _extract_doc_title(pages: list, file_path: str | None = None) -> str:
    """论文标题（管线与缓存重建共用）。

    2026-09-12 标题根治：**几何证据优先**——第 1 页字号最大、最靠上的连续行
    就是标题（学术排版铁律，对任何版式成立；PDF metadata 互证），文本启发式
    降为兜底。历史上的两级优先级（首个 # 标题块 / 首个正文块）保留为
    file_path 缺失或几何检测失败时的回退。
    """
    if file_path:
        try:
            from ocr.title_detect import detect_title

            t = detect_title(file_path)
            if t:
                return t
        except Exception as e:  # noqa: BLE001 —— 几何检测失败回退文本启发式
            print(f"[title] 几何检测失败，回退文本启发式: {e}")
    if not pages:
        return ""
    first = pages[0]
    # 优先级 1：# 标题块（跳过通用章节名 / 编号章节头）
    for block in first["blocks"]:
        m = re.match(r"^#\s+(.{4,120})$", (block.get("original") or "").strip())
        if not m:
            continue
        text = m.group(1).strip()
        if _is_generic_heading(text):
            continue
        return text
    # 优先级 2：首页首个正文块（跳过标题块/图片/结构块/通用名）
    for block in first["blocks"]:
        text = (block.get("original") or "").strip()
        if not text or len(text) > 300 or text.startswith("#"):
            continue
        if _PURE_IMAGE.match(text) or not _is_plain(text):
            continue
        if _is_generic_heading(text):
            continue
        return text
    return ""


def _is_generic_heading(text: str) -> bool:
    """去强调符后比对通用章节名；多级编号章节头（"2.1.2 X"）直接判非标题。"""
    if re.match(r"^\d+(\.\d+)+\s", text.replace("*", "").strip()):
        return True
    norm = text.replace("*", "").replace("_", "").strip()
    norm = norm.rstrip(":：").strip()
    norm = re.sub(r"^\d+(\.\d+)*\s+", "", norm).lower()
    return norm in _GENERIC_HEADINGS


def _norm_title(s: str) -> str:
    """标题比对归一化：只留字母/数字/中日韩，忽略大小写、标点、空白与记号。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s).lower()


def _ensure_title_heading(pages: list, doc_title: str) -> None:
    """页 0 标题块矫正（2026-09-12 标题根治）：几何证据认定的标题若在首页
    以纯文本块存在（textlayer 降级/拆分历史误伤形态），提升为 "# " 标题块
    ——读者看到的与证据认定的一致。必须在页眉剔除之前调用（裸标题块
    与 doc_title 相同会被 _remove_running_header 当页眉吃掉）。"""
    if not doc_title:
        return
    nt = _norm_title(doc_title)
    if len(nt) < 8:
        return
    for b in pages[0]["blocks"]:
        text = (b.get("original") or "").strip()
        if not text or text.startswith("#"):
            continue
        nb = _norm_title(text)
        if not nb:
            continue
        matched = (
            nb == nt
            or (len(nt) >= 15 and nb.startswith(nt))
            or (len(nb) >= 15 and nt.startswith(nb))
        )
        if matched:
            b["original"] = "# " + text
            print(f"[title] 页0标题块矫正为 heading：{text[:60]!r}")
            return


def _remove_running_header(pages: list, doc_title: str) -> None:
    """剔除与文档标题相同的页眉运行标题块（原地修改，管线与缓存重建共用）。

    真标题块带 "# " 前缀不受影响；比对时去掉 markdown 强调符
    （标题块是 "# **Title**" 而页眉是裸文本）。
    """
    if not doc_title:
        return
    dt_cmp = doc_title.replace("*", "").strip()
    for page in pages:
        page["blocks"] = [
            b
            for b in page["blocks"]
            if (b.get("original") or "").strip().replace("*", "") != dt_cmp
        ]


async def _load_or_run_ocr(file_path: str, pdf_hash: str, config: dict, job: dict) -> list:
    """混合提取 + 页级流式挂载（阶段1-T5；阶段12-T4 数字页主路线换 VLM）：

    1. 数字页主路线：PaddleOCR-VL 整页结构化解析（快照→遮罩→解析→
       交叉校验，bag<阈值/空输出/API 失败整页回退 textlayer——最坏=现状，
       详见 ocr/vlm_parse.parse_page_verified）；
    2. 扫描页（无文本层）：先挂空占位（保持页序对齐），提取循环结束后
       统一走视觉 OCR（只调这些页），结果原地替换占位；
    3. 逐页挂载渐进呈现：缓存命中毫秒级；VLM 页并发受限（免费档保守值），
       任务一次性创建、按页序 await——首页就绪即挂载，前端页序稳定；
    4. 两类结果统一按页缓存（伪模型名隔离），重跑同一文件秒开。

    vlm 关闭开关（config.json 的 ocr.vlm.enabled=false，或未配 API Key）
    → 整体回退旧文本层路径，行为与 v18 一致。
    """
    cache_dir = settings.cache_dir
    vision_model = config.get("model", "")

    # 页数探测（毫秒级）
    total_pages = await asyncio.to_thread(count_pages, file_path)
    job["stats"]["ocr_total"] = total_pages
    # 兼容最小 job dict（smoke 工具直调本函数时不带新计数键）
    job["stats"].setdefault("vlm_pages", 0)
    job["stats"].setdefault("vlm_fallback", 0)

    # 渐进挂载：job_pages 与最终结果同一列表，前端轮询即见逐页增长
    job_pages: list = [{"page": i, "blocks": []} for i in range(total_pages)]
    job["pages"] = job_pages
    job["progress"] = 8

    image_dir = os.path.join(cache_dir, "images", pdf_hash[:16])
    os.makedirs(image_dir, exist_ok=True)  # 图表快照的落盘目录（曾漏建致快照全灭）
    pages: list = [None] * total_pages
    vision_pages: list[int] = []

    vlm_cfg = config.get("vlm") or {}
    vlm_enabled = vlm_cfg.get("enabled", True) and bool(config.get("api_key"))
    bag_threshold = float(vlm_cfg.get("bag_threshold", 0.90))
    sem = asyncio.Semaphore(vlm_parse.CONCURRENCY)
    pending: dict[int, asyncio.Task] = {}
    vlm_todo: list[int] = []

    for i in range(total_pages):
        # 最终产物缓存命中 → 直接挂载（VLM/降级来源随缓存记录，供 stats）
        cached = read_cache(cache_dir, ocr_key(pdf_hash, i, TEXT_LAYER_MODEL))
        if cached and cached.get("blocks"):
            pages[i] = {"page": i, "blocks": cached["blocks"]}
            job_pages[i] = pages[i]
            job["stats"]["ocr_cache_hit"] += 1
            if cached.get("source") == "textlayer":
                job["stats"]["vlm_fallback"] += 1
            else:
                job["stats"]["vlm_pages"] += 1
        elif vlm_enabled:
            vlm_todo.append(i)  # 先收集，provider 就绪后再建任务（见下）
        else:
            md = (await asyncio.to_thread(extract_pages, file_path, [i], image_dir))[0]
            if md:  # 有文本层
                blocks = [_single_block(i, md)]
                write_cache(
                    cache_dir,
                    ocr_key(pdf_hash, i, TEXT_LAYER_MODEL),
                    {"blocks": blocks, "source": "textlayer"},
                )
                pages[i] = {"page": i, "blocks": blocks}
                job_pages[i] = pages[i]
            else:  # 无文本层 → 视觉路线（先查缓存，未命中挂占位保持页序）
                vcached = read_cache(
                    cache_dir, ocr_key(pdf_hash, i, vision_model + VISION_CACHE_SUFFIX)
                )
                if vcached and vcached.get("blocks"):
                    pages[i] = {"page": i, "blocks": vcached["blocks"]}
                    job_pages[i] = pages[i]
                    job["stats"]["ocr_cache_hit"] += 1
                else:
                    vision_pages.append(i)

    # 版面模型信号源（阶段12-T9.2）：VLM 页统一取一次 DocLayout-YOLO 区域
    # （子进程跑随包 babeldoc 运行时，缓存优先）。必须在建页任务**之前**
    # start——regions() 依赖 start 创建的页级 future，早于 start 调用会
    # 恒 None（竞态防复发）。任何失败都只是退回无版面信号行为。
    layout = None
    job["stats"].setdefault("layout_pages", 0)
    if vlm_todo:
        layout = LayoutProvider(
            file_path,
            pdf_hash,
            cache_dir,
            data_dir=os.path.dirname(os.path.abspath(cache_dir)),
        )
        try:
            await layout.start(vlm_todo)
        except Exception as e:  # noqa: BLE001 —— 版面信号是增强，不是依赖
            print(f"[layout] 版面模型启动失败（降级为无版面信号）: {e}")
            layout = None
        if layout is not None:
            print(
                f"[layout] 版面模型 status={layout.status} "
                f"cached={layout.stats['cached']} model={layout.stats['model']}"
                + (f" reason={layout.fail_reason}" if layout.fail_reason else "")
            )
    for i in vlm_todo:
        # 全部页任务先行创建，第二循环按页序 await（并发由 sem 闸住）
        pending[i] = asyncio.create_task(
            vlm_parse.parse_page_verified(
                file_path,
                i,
                pdf_hash,
                image_dir,
                config,
                cache_dir,
                bag_threshold=bag_threshold,
                sem=sem,
                layout=layout,
            )
        )

    # 按页序等待 VLM 任务：首页就绪即挂载（页级流式），失败传播前先取消
    # 其余任务（防孤儿任务与「exception never retrieved」噪音）
    try:
        for i in range(total_pages):
            if pages[i] is not None or i not in pending:
                job["progress"] = 8 + int((i + 1) / total_pages * 22)
                continue
            res = await pending[i]
            if res.get("scanned"):
                vcached = read_cache(
                    cache_dir,
                    ocr_key(pdf_hash, i, vision_model + VISION_CACHE_SUFFIX),
                )
                if vcached and vcached.get("blocks"):
                    pages[i] = {"page": i, "blocks": vcached["blocks"]}
                    job_pages[i] = pages[i]
                    job["stats"]["ocr_cache_hit"] += 1
                else:
                    vision_pages.append(i)  # 保持占位，循环后统一视觉 OCR
            else:
                blocks = [_single_block(i, res["md"] or "")]
                write_cache(
                    cache_dir,
                    ocr_key(pdf_hash, i, TEXT_LAYER_MODEL),
                    {"blocks": blocks, "source": res["source"], "bag": res.get("bag")},
                )
                if res["source"] == "vlm":
                    job["stats"]["vlm_pages"] += 1
                else:
                    job["stats"]["vlm_fallback"] += 1
                    print(
                        f"[vlm] p{i + 1}: 交叉校验降级文本层 "
                        f"reason={res.get('fallback_reason')} bag={res.get('bag')}"
                    )
                pages[i] = {"page": i, "blocks": blocks}
                job_pages[i] = pages[i]
            job["progress"] = 8 + int((i + 1) / total_pages * 22)
    except Exception:
        for t in pending.values():
            t.cancel()
        if layout is not None:
            await layout.close()
        raise

    # 扫描页走视觉 OCR（只调缺的页），结果原地替换占位。
    # page_image_dir：扫描页整页快照存盘并插入正文顶部（原模原样展示）。
    if vision_pages:
        vis = await call_ocr(
            file_path, config, only_pages=vision_pages, page_image_dir=image_dir
        )
        for p in vis:
            pages[p["page"]] = p
            job_pages[p["page"]] = p  # 占位时序即页序，索引对齐
            write_cache(
                cache_dir,
                ocr_key(pdf_hash, p["page"], vision_model + VISION_CACHE_SUFFIX),
                {"blocks": p["blocks"]},
            )

    # 版面信号源收尾：统计进 job stats（前端进度页/日志可见降级状态）
    if layout is not None:
        job["stats"]["layout_pages"] = layout.stats["used"]
        job["stats"]["layout_status"] = layout.status
        await layout.close()

    result = [p for p in pages if p is not None]
    if not result:
        raise ValueError("PDF 未解析出任何页面内容")
    return result


async def _process_pipeline(file_path: str, job_id: str, pdf_hash: str):
    job = _jobs[job_id]
    stats = job["stats"]
    try:
        # 处理前刷新配置，使改 Key/模型后无需重启（R7）
        settings.refresh()

        # ---- 阶段 1：OCR（0% ~ 30%）----
        job["progress"] = 5
        pages = await _load_or_run_ocr(file_path, pdf_hash, settings.ocr_config, job)
        # 把每页「整块 markdown」切成段/句级 block（对照粒度，见决策），
        # 切块发生在 OCR 缓存读取之后，因此不动 OCR 缓存粒度。
        pages = [_split_page(p) for p in pages]
        # 论文标题（几何证据优先，文本启发式兜底）提前到合并前提取：页眉运行
        # 标题剔除必须发生在合并之前，防止裸标题行被当成续段合并目标误接
        doc_title = _extract_doc_title(pages, file_path)
        # 页0标题块矫正（提升为 # heading）必须在页眉剔除之前
        _ensure_title_heading(pages, doc_title)
        # 页眉运行标题（ACM/期刊版式每页重复的裸标题行，Survey 实测每页
        # 一块、还被模型回声成"假译文"）：与文档标题相同的块剔除。
        # 真标题块带 "# " 前缀不受影响；比对时去掉 markdown 强调符
        # （标题块是 "# **Title**" 而页眉是裸文本）
        _remove_running_header(pages, doc_title)
        # 跨页段落合并（阶段2-T3）：紧跟模式不再把跨页同段显示成两块残文
        pages = _merge_cross_page(pages)
        # 原版对照模式（阶段5-T1，D6）：为每个最终块标注页面坐标 bbox，
        # 前端 pdfjs 原版渲染后按坐标叠加高亮/译文浮层。前缀匹配失败
        # （公式碎块/图内文字/扫描页）→ None，只是不高亮，不损失内容。
        try:
            from pipeline.layout import attach_block_bboxes

            await asyncio.to_thread(attach_block_bboxes, file_path, pages)
        except Exception as e:  # 原版模式是增值能力，绝不阻断主链路
            print(f"[layout] bbox 标注失败（原版模式降级为无坐标）: {e}")
        job["progress"] = 30

        # ---- 阶段 2：翻译（30% ~ 100%）----
        t_cfg = dict(settings.translate_config)  # 拷贝：术语表等运行期注入不污染全局配置
        target_lang = t_cfg.get("target_language", "en")
        source_lang = t_cfg.get("source_language", "zh")
        model = t_cfg.get("model", "")

        # 术语表两遍法 Pass 0（阶段2-T4）：全文翻译前先抽术语，注入系统提示词。
        # 失败自动降级直译（glossary 返回 {}），绝不阻塞主链路。
        from translate.glossary import build_glossary

        glossary = await build_glossary(pages, t_cfg, settings.cache_dir, pdf_hash)
        if glossary:
            t_cfg["glossary"] = glossary
        # 论文标题随术语表一起注入提示词（合并前已提取）
        if doc_title:
            t_cfg["doc_title"] = doc_title

        # 先收集所有需要翻译的 block（跳过空白、纯图片与已缓存的）
        # 元组第 4 位 = 该块所处的当前小节标题（阶段12-T6：注入系统
        # 提示词做术语消歧的近端语境，随块顺序滚动更新）
        pending: list[tuple] = []  # (page, block, cache_key, section)
        fig_jobs: list[tuple] = []  # (block, cache_key, Task)——译制图与文本块并发
        cur_section = ""
        for page in pages:
            for block in page["blocks"]:
                original = (block.get("original") or "").strip()
                # 数学字母区规范化（2026-09-08）：𝒩→N、𝑥→x，翻译模型与
                # 公式保护都不再被怪字符干扰；在缓存键计算前做，全文管线
                # 与单块重翻键一致。改动会使旧缓存自然失效重翻（期望行为）
                original = sanitize.normalize_math_letters(original)
                block["original"] = original
                if not original:
                    block["translated"] = ""
                    continue
                if original.startswith("#"):
                    cur_section = original.lstrip("#").strip()[:80]
                if _PURE_IMAGE.match(original):
                    # 图表块：译文=原图（图表不做翻译）。
                    # 译制图功能默认关闭（FIGURE_TRANSLATION_ENABLED），开启时
                    # 走"译制图"管线并并发调度（串行 await 曾把进度堵在 30%）。
                    block["translated"] = original
                    if FIGURE_TRANSLATION_ENABLED:
                        key = translate_key(
                            text_hash(original), target_lang, model, PROMPT_VERSION
                        )
                        cached = read_cache(settings.cache_dir, key)
                        if cached and cached.get("translated"):
                            block["translated"] = cached["translated"]
                            stats["tr_cache_hit"] += 1
                            stats["tr_total"] += 1
                        else:
                            stats["tr_total"] += 1
                            fig_jobs.append(
                                (
                                    block,
                                    key,
                                    asyncio.create_task(
                                        _translate_figure_block(
                                            original, file_path, t_cfg
                                        )
                                    ),
                                )
                            )
                    continue
                # 公式密集块（阶段2-T5）：数学碎片翻译毫无意义，译文=原文。
                # formula_hint 标记（前端出「式」按钮按需 OCR 识别 LaTeX）；
                # 识别过的块直接回填 LaTeX（按需结果的持久化，重开不丢，2026-09-08）
                if sanitize.is_formula_block(original):
                    block["translated"] = original
                    block["formula_hint"] = True
                    bbox = block.get("bbox")
                    if bbox:
                        from ocr.formula import load_cached_formula

                        cached_latex = load_cached_formula(
                            pdf_hash,
                            page["page"],
                            bbox,
                            settings.ocr_config,
                            settings.cache_dir,
                        )
                        if cached_latex:
                            block["translated"] = cached_latex
                    continue
                # 数学密集混合块（2026-09-08）：散文+行内公式，行内数学在
                # 提取层已拍平（◆/𝑥/_x_^），照常翻译救不回结构——只打标，
                # 前端出「式」按钮走视觉重识别，识别结果替换原文后自动重译
                if sanitize.has_heavy_math(original):
                    block["formula_hint"] = True
                    block["math_mixed"] = True
                key = translate_key(
                    text_hash(original), target_lang, model, PROMPT_VERSION
                )
                cached = read_cache(settings.cache_dir, key)
                if (
                    cached
                    and cached.get("translated")
                    # 历史缓存中的回声条目视为未翻译：重新走翻译+补翻，
                    # 避免英文原文被当译文常年展示（2026-09-07 修复）
                    and not sanitize.is_echo(
                        original, cached["translated"], target_lang
                    )
                    # 融合条目（批量翻译时整批译文塞进单段）同样重翻：
                    # HippoRAG 标题块译文曾带摘要/引言/方法全文（2026-09-07）
                    and not sanitize.is_fused_translation(
                        original, cached["translated"]
                    )
                ):
                    block["translated"] = cached["translated"]
                    stats["tr_cache_hit"] += 1
                    stats["tr_total"] += 1
                else:
                    pending.append((page, block, key, cur_section))
                    stats["tr_total"] += 1

        total = max(len(pending), 1)
        done = 0
        # 并发按「块组」计：每组一次合并请求（6 段），请求数约为逐条模式的 1/6
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        # 提前挂载 pages：block 对象是引用，翻译过程中前端轮询即可渐进看到已完成的译文
        job["pages"] = pages

        async def _translate_chunk(chunk: list[tuple], advance: bool = True):
            nonlocal done
            async with sem:
                texts = [b["original"] for _, b, _, _ in chunk]
                # 小节语境注入（阶段12-T6）：取块组内最靠后的非空小节
                # （最接近本批结尾的阅读位置）；浅拷贝配置避免并发污染
                section = next(
                    (s for _, _, _, s in reversed(chunk) if s), ""
                )
                cfg = dict(t_cfg, section=section) if section else t_cfg
                # 公式保护（阶段2-T5）：LaTeX 定界式 + 数学碎片 token 占位后
                # 送翻（正文照常翻译），译文回来再原样还原
                protected = [sanitize.protect_formulas(t) for t in texts]
                try:
                    outs = await translate_batch(
                        [p for p, _ in protected],
                        source_lang,
                        target_lang,
                        cfg,
                    )
                    if len(outs) != len(texts):
                        raise ValueError(
                            f"译文数量不匹配: 期望 {len(texts)} 得到 {len(outs)}"
                        )
                except Exception:
                    # 批量失败：逐条回退，宁可慢不可丢
                    outs = []
                    for p, _restore in protected:
                        try:
                            outs.append(
                                await translate_text(
                                    p, source_lang, target_lang, cfg
                                )
                            )
                        except Exception as e:
                            print(f"[translate] 单段翻译失败: {e}")
                            outs.append("")
                for (_, block, key, _s), (p, restore), translated in zip(
                    chunk, protected, outs
                ):
                    block["translated"] = sanitize.strip_stray_emphasis(
                        restore(
                            sanitize.strip_prompt_echo(
                                translated or "", t_cfg.get("doc_title")
                            )
                        )
                    )
                    # 回声（模型原样照抄原文）不落缓存：否则下轮缓存命中
                    # 直接展示英文原文当译文，且永远绕过补翻（Survey 实测：
                    # 参考文献整节回声落缓存，2026-09-07）
                    # 融合译文同样不落缓存：标题块吞正文的单段膨胀形态
                    # 曾落缓存，此后每轮命中每轮展示（ToG 摘要实测
                    # 2026-09-07 晚）
                    _orig = block.get("original") or ""
                    if not sanitize.is_echo(
                        _orig,
                        block["translated"],
                        target_lang,
                    ) and not sanitize.is_fused_translation(
                        _orig,
                        block["translated"],
                    ):
                        write_cache(settings.cache_dir, key, {"translated": block["translated"]})
                    if advance:
                        done += 1
                        # 钳制在 99：补翻等后置阶段不应把进度顶过 100
                        # （用户实测"进度条卡出 100% 还在加"——补翻轮复用
                        # 本函数把 done 二次累加所致，2026-09-07）
                        job["progress"] = min(
                            99, 30 + int(done / total * 70)
                        )

        chunks = [
            pending[i : i + 10] for i in range(0, len(pending), 10)
        ]

        try:
            await asyncio.gather(*[_translate_chunk(c) for c in chunks])
        except Exception:
            # 单个 chunk 失败不应让整本书前功尽弃：已完成的保留，失败的留空
            pass

        # 失败块补翻一轮（用户截图反馈"待翻译…"残留）：瞬时故障（限流/
        # 网络抖动）导致的空译文，重试一次即可恢复；仍失败保持留空不阻塞。
        # 回声块（DualR 实测 29 个）同样补翻：模型原样返回原文（参考文献/
        # 表题等），用户看到英文即"没翻译"——清空译文后与新提示词重翻。
        # 融合块（ToG 实测：标题块吞正文单段膨胀）一并清空补翻。
        def _needs_retry(block: dict) -> bool:
            orig = block.get("original") or ""
            trans = block.get("translated") or ""
            return (
                not trans.strip()
                or sanitize.is_echo(orig, trans, target_lang)
                or sanitize.is_fused_translation(orig, trans)
            )

        retry = [
            (page, block, key, section)
            for page, block, key, section in pending
            if _needs_retry(block)
        ]
        for _, block, _, _ in retry:
            orig = block.get("original") or ""
            trans = block.get("translated") or ""
            if sanitize.is_echo(orig, trans, target_lang) or sanitize.is_fused_translation(orig, trans):
                block["translated"] = ""  # 清空回声/融合，让补翻重写并回写缓存
        if retry:
            print(
                f"[translate] {len(retry)} 个块译文为空/回声，补翻一轮"
            )
            retry_chunks = [retry[i : i + 10] for i in range(0, len(retry), 10)]
            try:
                # advance=False：补翻不计进度——否则 done 二次累加，
                # 进度条冲破 100% 还持续上涨（用户实测，2026-09-07）
                await asyncio.gather(
                    *[_translate_chunk(c, advance=False) for c in retry_chunks]
                )
            except Exception:
                pass
            # 补翻后仍是回声（参考文献等模型必然原样照抄的内容，重试也无解）：
            # 清空译文保持"待翻译"状态——比拿英文原文冒充译文更诚实，
            # 用户可用段落级手动翻译按钮重试
            for _, block, _, _ in retry:
                if sanitize.is_echo(
                    block.get("original") or "",
                    block.get("translated") or "",
                    target_lang,
                ):
                    block["translated"] = ""

        # 译制图任务收尾：与文本翻译并发执行，文本翻完这里等它们落盘。
        # 任何一个失败只影响那一张图（block 停留在回退原图），不阻断。
        if fig_jobs:
            print(f"[figure] 等待 {len(fig_jobs)} 张译制图生成…")
            fig_outs = await asyncio.gather(
                *[t for _, _, t in fig_jobs], return_exceptions=True
            )
            for (block, key), out in zip(fig_jobs, fig_outs):
                if isinstance(out, str) and out:
                    block["translated"] = out
                    write_cache(settings.cache_dir, key, {"translated": out})
                elif isinstance(out, Exception):
                    print(f"[figure] 译制图任务失败（回退原图）: {out}")

        # 公式自动后处理（用户建议的"二次翻译"，2026-09-08）：文本翻译完成后
        # 对 formula_hint 块自动跑视觉识别——纯公式块结果直接进译文位；数学
        # 密集混合块识别结果（英文正文+$..$）替换原文并自动单块重译。识别按
        # 内容寻址缓存幂等（重跑零成本）；失败块保留现状，可手动「式」重试。
        fx_targets = [
            (page, block)
            for page in pages
            for block in page["blocks"]
            if block.get("formula_hint")
            and (block.get("bboxes") or block.get("bbox"))
        ]
        if fx_targets:
            print(f"[formula] 公式后处理：{len(fx_targets)} 个公式块")
            from ocr.formula import recognize_block_formula

            fx_sem = asyncio.Semaphore(2)

            async def _fx_one(page: dict, block: dict) -> None:
                async with fx_sem:
                    segs = block.get("bboxes") or [
                        {"page": page["page"], "bbox": block["bbox"]}
                    ]
                    parts: list[str] = []
                    for seg in segs:
                        try:
                            r = await recognize_block_formula(
                                file_path,
                                pdf_hash,
                                seg["page"],
                                seg["bbox"],
                                settings.ocr_config,
                                settings.cache_dir,
                            )
                            parts.append(r["latex"].strip())
                        except Exception as e:
                            print(f"[formula] 段识别失败（块保留原状）: {e}")
                            return
                    md = "\n\n".join(p for p in parts if p)
                    if not md:
                        return
                    # 混合块判定与前端同阈值：识别结果含足量正文 → 重译
                    words = len(re.findall(r"[A-Za-z]{2,}", md))
                    if words < 10:
                        block["translated"] = md
                        return
                    block["original"] = md
                    protected_md, restore_md = sanitize.protect_formulas(md)
                    try:
                        translated = await translate_text(
                            protected_md, source_lang, target_lang, t_cfg
                        )
                        block["translated"] = sanitize.strip_stray_emphasis(
                            restore_md(
                                sanitize.strip_prompt_echo(
                                    translated or "", t_cfg.get("doc_title")
                                )
                            )
                        )
                        if not sanitize.is_echo(
                            md, block["translated"], target_lang
                        ):
                            write_cache(
                                settings.cache_dir,
                                translate_key(
                                    text_hash(md), target_lang, model, PROMPT_VERSION
                                ),
                                {"translated": block["translated"]},
                            )
                    except Exception as e:
                        print(f"[formula] 混合块重译失败（识别结果保留）: {e}")

            await asyncio.gather(
                *[_fx_one(p, b) for p, b in fx_targets], return_exceptions=True
            )

        job["pages"] = pages
        job["status"] = "done"
        job["progress"] = 100
        job["finished_at"] = time.time()

        # 阶段6-T2：持久化文档索引（主页"已翻译文章"列表的数据源）。
        # 结果此前只存内存 _jobs（TTL 淘汰），重启即失；索引写入失败
        # 只影响主页列表，绝不阻断主链路。
        try:
            import docs_index

            docs_index.upsert_doc(
                settings.data_dir,
                {
                    "doc_id": pdf_hash[:16],
                    # 2026-09-09 用户决策：索引标题直接用上传文件名（去 .pdf），
                    # 想改名就改文件名。提取的 doc_title 只用于术语表提示词
                    # 与页眉剔除，不再决定卡片标题。
                    "title": os.path.splitext(os.path.basename(file_path))[0],
                    "file_path": os.path.abspath(file_path),
                    "pdf_hash": pdf_hash,
                    "page_count": len(pages),
                    "file_mtime": int(os.path.getmtime(file_path)),
                    "status": "done",
                },
            )
        except Exception as e:
            print(f"[docs_index] 索引写入失败（不阻断主链路）: {e}")
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["progress"] = job.get("progress", 0)
        job["finished_at"] = time.time()


async def open_cached_doc(pdf_hash: str, page_count: int, file_path: str) -> dict:
    """阶段6-T3：从缓存重建已翻译文档（主页点卡片秒开）。

    - 不跑 OCR、不发任何翻译 API 请求：页面块来自页级 OCR 缓存
      （文本层伪模型名优先，扫描页视觉缓存兜底），译文按当前配置的
      translate_key 读缓存——翻译后没改模型/语言时全文命中；
    - 重建链路与管线同构：切块 → 标题提取/页眉剔除 → 跨页合并；
    - 个别页缓存缺失保留空占位（保持页序）；整篇提取缓存全缺时抛
      ValueError（端点转 404，引导重新翻译）；
    - 源文件存在时附坐标标注（原版模式可用）；缺失时跳过（对照/紧跟
      纯缓存 markdown 可用，原版模式由前端禁用）。

    返回 {pages, file_exists, doc_title}。
    """
    cache_dir = settings.cache_dir
    vision_model = settings.ocr_config.get("model", "")

    pages: list = []
    for i in range(max(0, page_count)):
        cached = read_cache(cache_dir, ocr_key(pdf_hash, i, TEXT_LAYER_MODEL))
        if cached and cached.get("blocks"):
            pages.append({"page": i, "blocks": cached["blocks"]})
            continue
        vcached = read_cache(
            cache_dir, ocr_key(pdf_hash, i, vision_model + VISION_CACHE_SUFFIX)
        )
        if vcached and vcached.get("blocks"):
            pages.append({"page": i, "blocks": vcached["blocks"]})
        else:
            pages.append({"page": i, "blocks": []})
    if not any(p["blocks"] for p in pages):
        raise ValueError("该文档的提取缓存已不存在，请重新翻译")

    pages = [_split_page(p) for p in pages]
    doc_title = _extract_doc_title(pages, file_path)
    _ensure_title_heading(pages, doc_title)
    _remove_running_header(pages, doc_title)
    pages = _merge_cross_page(pages)

    t_cfg = dict(settings.translate_config)
    target_lang = t_cfg.get("target_language", "en")
    model = t_cfg.get("model", "")

    for page in pages:
        for block in page["blocks"]:
            original = sanitize.normalize_math_letters(
                (block.get("original") or "").strip()
            )
            block["original"] = original
            if not original:
                block["translated"] = ""
                continue
            if _PURE_IMAGE.match(original):
                block["translated"] = original  # 图表块：译文=原图快照
                continue
            if sanitize.is_formula_block(original):
                block["translated"] = original
                block["formula_hint"] = True
                bbox = block.get("bbox")
                if bbox:
                    from ocr.formula import load_cached_formula

                    cached_latex = load_cached_formula(
                        pdf_hash, page["page"], bbox, settings.ocr_config, cache_dir
                    )
                    if cached_latex:
                        block["translated"] = cached_latex
                continue
            key = translate_key(text_hash(original), target_lang, model, PROMPT_VERSION)
            cached = read_cache(cache_dir, key)
            block["translated"] = (cached or {}).get("translated", "")

    file_exists = bool(file_path) and os.path.isfile(file_path)
    if file_exists:
        try:
            from pipeline.layout import attach_block_bboxes

            await asyncio.to_thread(attach_block_bboxes, file_path, pages)
        except Exception as e:  # 原版模式是增值能力，绝不阻断重开
            print(f"[docs_open] bbox 标注失败（原版模式降级为无坐标）: {e}")

    return {"pages": pages, "file_exists": file_exists, "doc_title": doc_title}


async def get_pipeline_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {
            "job_id": job_id,
            "status": "unknown",
            "progress": 0,
            "pages": [],
            "error": None,
        }
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "pages": job.get("pages", []),
        "error": job.get("error"),
        "stats": job.get("stats"),
    }
