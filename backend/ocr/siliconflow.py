import asyncio
import base64
import os
import re
import httpx
import pymupdf

# PyMuPDF 在 1.24+ 将 `import fitz` 标记为弃用，统一用 `import pymupdf` 并别名，
# 后续所有 fitz.* 调用保持不变。它单 pip 包、跨平台、无外部依赖，用于 PDF → 每页 PNG。
fitz = pymupdf

# PaddleOCR-VL 在 SiliconFlow 上通过标准 OpenAI 兼容接口提供，
# 仅接受「图片」+ 自然语言指令（"OCR:" 即触发版面解析）。
# 注意：SiliconFlow 云 API 没有 /generate 端点，旧实现调不通（见《开发总纲》第 11 章）。
OCR_PROMPT = "OCR:"
DEFAULT_MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"

# PDF 渲染分辨率（矩阵缩放系数）：学术论文正文字号小，1.8x 下小字模糊导致
# 漏识别（实测问题）；2.5x 是识别完整度与 token 成本的平衡点。
RENDER_SCALE = 2.5# OCR 并发上限，免费额度下保守取值
OCR_CONCURRENCY = 3


async def call_ocr(
    file_path: str,
    config: dict,
    only_pages: list[int] | None = None,
    page_image_dir: str | None = None,
) -> list:
    """视觉 OCR。only_pages 指定仅识别这些页（文本层提取后仍缺内容的扫描页）。

    page_image_dir 非空时：
    把每页渲染图同时存盘，并在该页 OCR markdown 顶部插入 `![Page](路径)`
    引用——扫描页的表格/产品图都是整页位图的一部分，本地无法定位子区域，
    整页快照是"原模原样"的兜底方案；切块后成为独立纯图片块，不送翻译。"""
    api_url = config.get("api_url", "https://api.siliconflow.cn/v1")
    api_key = config.get("api_key", "")
    model = config.get("model", DEFAULT_MODEL)

    if not api_key:
        raise ValueError("OCR API Key 未配置")

    # 1) PDF → 每页 PNG（内存中，不落盘）。fitz 是阻塞 IO，放到线程避免卡事件循环。
    page_images = await asyncio.to_thread(_pdf_to_page_images, file_path, only_pages)
    if not page_images:
        raise ValueError("PDF 未解析出任何页面")

    # 2) 逐页 OCR（受限并发）
    sem = asyncio.Semaphore(OCR_CONCURRENCY)

    async def ocr_one(idx: int, img_bytes: bytes) -> dict:
        async with sem:
            markdown = await _ocr_image(img_bytes, api_url, api_key, model)
            return {
                "page": idx,
                "blocks": [
                    {
                        "block_id": 0,
                        "page": idx,
                        "original": markdown or "",
                        "translated": "",
                        "position": {"y_start": 0, "y_end": 0},
                    }
                ],
            }

    results = await asyncio.gather(
        *[ocr_one(page_no, b) for page_no, b in page_images]
    )

    # 3) 整页快照：存盘 + 在 OCR 文本前插入页面图引用（见 docstring）。
    #    存 JPEG（实测体积为 PNG 的 1/3~1/4，扫描型手册一页 PNG 可达 2MB+）。
    if page_image_dir:
        os.makedirs(page_image_dir, exist_ok=True)
        by_page = {p["page"]: p for p in results}
        try:
            doc = fitz.open(file_path)
        except Exception:
            doc = None
        for page_no, _ in page_images:
            path = os.path.join(page_image_dir, f"page_p{page_no + 1:03d}.jpg")
            try:
                if doc is None:
                    continue
                pix = doc[page_no].get_pixmap(matrix=fitz.Matrix(2, 2))
                pix.save(path)
                blk = by_page[page_no]["blocks"][0]
                blk["original"] = (
                    f"![Page]({path.replace(os.sep, '/')})\n\n{blk['original']}"
                )
            except Exception:
                continue  # 快照失败不阻断 OCR 结果
        if doc is not None:
            doc.close()

    return list(results)


def _pdf_to_page_images(file_path: str, only_pages: list[int] | None = None) -> list[tuple[int, bytes]]:
    """渲染指定页（缺省全部页）为 PNG。返回 (页号, 图片字节) 列表。"""
    doc = fitz.open(file_path)
    try:
        out = []
        mat = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)
        page_nums = only_pages if only_pages is not None else range(len(doc))
        for i in page_nums:
            pix = doc[i].get_pixmap(matrix=mat)
            out.append((i, pix.tobytes("png")))
        return out
    finally:
        doc.close()


async def _ocr_image(
    img_bytes: bytes,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str = OCR_PROMPT,
) -> str:
    b64 = base64.b64encode(img_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        # PaddleOCR-VL 总上下文 16384 tokens，max_tokens 顶满会 400（实测），
        # 8192 是安全上限；超出部分由下方 finish_reason 提示。
        "max_tokens": 8192,
        "temperature": 0.01,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{api_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            # 保留响应体，4xx 的具体原因（如 max_tokens 超限）都在 body 里
            raise RuntimeError(
                f"OCR 请求失败 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()

    choice = data["choices"][0]
    text = (choice["message"]["content"] or "").strip()
    # 密集学术页可能超出单次输出上限：显式提示截断，避免"内容不全"却无感知
    if choice.get("finish_reason") == "length":
        text += "\n\n> ⚠️ 本页内容超出单次识别上限，末尾部分被截断"
    return text


# ── 段级切块──────────────────────────────────
# 按空行切段，一段一块。不做句级切分——句级切分会把连贯论述拆成
# 一句一句的碎片（实测反馈"排版都是一句一句的"），且打断表格/标题结构。
# 切块发生在 OCR 缓存读取之后，因此改切块策略不影响缓存命中。

# 页脚噪音：纯页码/罗马页码/Page N of N
# 增补 ACM 期刊页码 "111:2"（Graph RAG 实测每页一块）
_NOISE_BLOCK = re.compile(
    r"^(?:\d{1,4}|[ivxlcdm]{1,8}|page\s*\d+(?:\s*(?:of|/)\s*\d+)?|\d{1,4}:\d{1,3})$",
    re.IGNORECASE,
)

# 页眉作者行：
# 仅"单姓 + et al."的极短行才算——带逗号/缩写的引用条目（"Smith J, et al."）
# 不匹配，防止误杀参考文献
_RUNNING_HEAD = re.compile(r"^[A-Z][a-zA-Z\-']{1,20}\s+et\s+al\.?$")

# 页眉/页脚固定文案（实测反馈"跨页合并把 'Published as a conference paper
# at ICLR 2024' 吸进正文"）：论文模板每页重复的出版声明/arXiv
# 标识。短块 + 特征短语才判页脚——正文里讨论这些短语的整段不受影响。
# 增补 ACM 期刊页脚（"J. ACM, Vol. 37, No. 4, Article 111.
# Publication date: September 2024."，每页重复，实测挡住跨页续段合并）
_FOOTER_PAT = re.compile(
    r"published\s+as\s+a\s+conference\s+paper"
    r"|arxiv[:\s]*\d{4}\.\d{4,5}"
    r"|proceedings\s+of\s+the\s+\d{2,}"
    r"|all\s+rights\s+reserved"
    r"|©\s*\d{4}"
    r"|j\.\s*[a-z]{2,10},\s*vol\.\s*\d+"
    r"|publication\s+date\s*:",
    re.IGNORECASE,
)
_FOOTER_MAX_CHARS = 140

# run-in 引导标题拆分（实测反馈"只有一个标题，但输出了一大段话"，
# ）：术语定义段以斜体/粗体引导词开头
# （"_Graph-Enhanced Generation (G-Generation)._ The graph-enhanced ..."，
# LaTeX \paragraph{} 惯例），标题与定义正文拆成两块——标题单独成块
# 展示/翻译，正文独立参与续段合并。终结符必须在强调符内侧才拆
# （句中普通强调 "_is_" 无终结符不匹配）。
_RUN_IN_LEAD = re.compile(
    r"^(?P<lead>(_|\*\*)[^*_\n]{2,120}?[.。:：;；](_|\*\*))[ \t]*(?P<rest>\S.*)$",
    re.DOTALL,
)


def split_into_blocks(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    blocks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # run-in 引导标题拆分（标题/正文各成一块，见 _RUN_IN_LEAD 注释）
    expanded: list[str] = []
    for b in blocks:
        m = _RUN_IN_LEAD.match(b)
        if m:
            expanded.append(m.group("lead"))
            expanded.append(m.group("rest").strip())
        else:
            expanded.append(b)
    # 过滤页码/页脚噪音块（去空格后匹配，兼容 "1 2" 之类异常排版）
    return [
        b
        for b in expanded
        if not _NOISE_BLOCK.match(re.sub(r"\s+", "", b))
        and not (len(b) < 60 and _RUNNING_HEAD.match(b))
        and not (len(b) < _FOOTER_MAX_CHARS and _FOOTER_PAT.search(b))
    ]
