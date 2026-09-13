from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import asyncio
import base64
import logging
import struct
import threading
import time
import zlib
import uvicorn
import os
import sys
import json
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.processor import (
    run_pipeline,
    get_pipeline_status,
    running_job_count,
    list_running_jobs,
    MAX_RUNNING_JOBS,
)
from config import settings

# uvicorn 的默认 logger 不覆盖端点内 except 的异常细节；
# 统一经 root logger 输出（dev 由 dev-start.ps1 重定向到 logs/backend-dev.log），
# 端点失败原因不再只存在于 HTTP 响应里（2026-09-08 用户反馈"后端没有日志"）。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pdf-reader")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await settings.init()
    # 阶段6-T4：启动即打印实际生效的数据目录，兜底轨激活时显式告警
    logger.info("数据目录: %s（config.json / cache/ / docs_index.json 统一在此）", settings.data_dir)
    logger.info("配置文件: %s", settings.config_path)
    logger.info("缓存目录: %s", settings.cache_dir)
    if settings.using_fallback_dir():
        logger.warning(
            "未检测到 APPDATA 环境变量，已退回 ~/.pdf-reader 兜底目录"
            "（Windows 打包版不应出现；可用 PDF_READER_CONFIG 显式指定）"
        )
    yield

app = FastAPI(title="PDF Bilingual Reader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def api_health():
    """健康检查：供 Tauri 侧确认内嵌 sidecar 后端已就绪"""
    return {"status": "ok"}


@app.post("/api/config/reload")
async def api_reload_config():
    """显式刷新配置（R7）。日常处理已自动按 mtime 热更新，此端点用于保存配置后立即生效。"""
    changed = settings.refresh()
    return {"status": "ok", "changed": changed}


@app.get("/api/asset")
async def api_asset(path: str):
    """按绝对路径返回缓存目录内的图片文件（浏览器模式渲染论文插图用）。

    安全约束：仅允许 settings.cache_dir 之下的文件，防止任意文件读取。
    Tauri 模式走 convertFileSrc 不经过此端点。
    """
    cache_root = os.path.realpath(settings.cache_dir)
    target = os.path.realpath(path)
    if os.path.commonpath([target, cache_root]) != cache_root:
        raise HTTPException(status_code=403, detail="路径超出缓存目录")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.post("/api/figure/translate")
async def api_figure_translate(payload: dict):
    """按需生成"译制图"（2026-09-06 用户决策：仅表格支持，点按触发）。

    前端在全文翻译完成后，用户点击表格图片触发本接口：
    传入快照 PNG 的绝对路径，返回译制图 markdown（![Table](zh路径)）。
    - 仅接受 tab_* 表格快照（kind=table），图不翻译；
    - 结果落盘缓存（zh 文件存在即直接返回），重复点击幂等；
    - 路径校验同 /api/asset：仅限 settings.cache_dir 之内。
    """
    from ocr import figtranslate

    path = str(payload.get("path") or "")
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    cache_root = os.path.realpath(settings.cache_dir)
    target = os.path.realpath(path)
    if os.path.commonpath([target, cache_root]) != cache_root:
        raise HTTPException(status_code=403, detail="路径超出缓存目录")
    sidecar_path = target + ".json"
    if not os.path.isfile(sidecar_path):
        raise HTTPException(status_code=404, detail="缺少表格元数据（sidecar）")
    try:
        import json

        with open(sidecar_path, encoding="utf-8") as f:
            kind = json.load(f).get("kind")
    except Exception:
        kind = None
    if kind != "table":
        raise HTTPException(status_code=400, detail="仅支持表格快照的按需翻译")
    zh = await figtranslate.translate_figure(
        sidecar_path, None, settings.translate_config
    )
    if not zh or not os.path.isfile(zh):
        reason = figtranslate.last_error() or "未知原因（见后端日志）"
        raise HTTPException(status_code=500, detail=f"{reason}")
    return {"translated": f"![Table]({zh.replace(os.sep, '/')})"}


@app.post("/api/block/formula")
async def api_block_formula(payload: dict):
    """块级公式识别（按需「式」按钮，2026-09-08 用户决策）。

    传入 {file_path, page, bbox}：裁剪该块区域渲染 2.5x PNG → 视觉模型
    （默认 PaddleOCR-VL-1.5，文档解析专精，公式→LaTeX 原生能力）→ 返回
    {latex, cached}。结果按 (pdf_hash, page, bbox, model) 内容寻址缓存，
    重复点按/重开文档幂等零成本；流水线对已缓存公式块自动回填译文位。
    前端拿到 LaTeX 后走既有 KaTeX 管线渲染。
    """
    from cache.file_cache import file_hash
    from ocr.formula import recognize_block_formula

    file_path = str(payload.get("file_path") or "").strip()
    page = payload.get("page")
    bbox = payload.get("bbox") or []
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=400, detail="文件不存在")
    if not isinstance(page, int) or page < 0 or len(bbox) != 4:
        raise HTTPException(status_code=400, detail="参数不完整（page/bbox）")
    pdf_hash = await asyncio.to_thread(file_hash, file_path)
    try:
        return await recognize_block_formula(
            file_path, pdf_hash, page, bbox, settings.ocr_config, settings.cache_dir
        )
    except Exception as e:
        # 异常细节落日志（Hidden 启动时经重定向可查），HTTP 响应只带摘要
        logger.exception(
            "块级公式识别失败 file=%s page=%s bbox=%s", file_path, page, bbox
        )
        raise HTTPException(status_code=502, detail=f"公式识别失败: {e}")


@app.post("/api/block/translate")
async def api_block_translate(payload: dict):
    """单块手动翻译/重翻（2026-09-07 用户需求：逐段点按触发）。

    用于两类场景：某段漏翻（"待翻译…"残留）或译文效果不佳，用户手动
    点按该段的「译/重译」按钮重新翻译。与全文管线走同一链路
    （公式保护 → 翻译 → 还原 → 清理），结果写回**同一缓存 key**
    （text_hash + 目标语 + model + PROMPT_VERSION），重开文档不丢。
    注意：同 key 覆盖写，故"重翻"天然 bypass 旧缓存。
    """
    from translate.base import translate_text
    from translate import sanitize
    from cache.file_cache import translate_key, text_hash, write_cache
    from translate.providers.openai_compat import PROMPT_VERSION

    original = str(payload.get("original") or "")
    # 与全文管线一致：数学字母区规范化后再保护/翻译/算缓存键（2026-09-08）
    original = sanitize.normalize_math_letters(original).strip()
    if not original.strip():
        raise HTTPException(status_code=400, detail="原文为空")
    t_cfg = dict(settings.translate_config)
    source_lang = payload.get("source_lang") or t_cfg.get(
        "source_language", "zh"
    )
    target_lang = payload.get("target_lang") or t_cfg.get(
        "target_language", "en"
    )
    protected, restore = sanitize.protect_formulas(original)
    try:
        translated = await translate_text(
            protected, source_lang, target_lang, t_cfg
        )
    except Exception as e:
        logger.exception("单块翻译失败 lang=%s→%s len=%d", source_lang, target_lang, len(original))
        raise HTTPException(status_code=502, detail=f"翻译失败: {e}")
    out = sanitize.strip_stray_emphasis(
        restore(sanitize.strip_prompt_echo(translated or ""))
    )
    key = translate_key(
        text_hash(original),
        target_lang,
        t_cfg.get("model", ""),
        PROMPT_VERSION,
    )
    write_cache(settings.cache_dir, key, {"translated": out})
    return {"translated": out}


def _make_test_png_b64() -> str:
    """生成 32x32 纯色 PNG 的 base64，用于 OCR 连接测试的最小图片载荷。"""
    size = 32
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + ihdr_crc
    rows = b"".join(b"\x00" + b"\x80\x80\x80" * size for _ in range(size))
    compressed = zlib.compress(rows)
    idat_crc = struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + idat_crc
    iend_crc = struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    iend = struct.pack(">I", 0) + b"IEND" + iend_crc
    return base64.b64encode(sig + ihdr + idat + iend).decode("ascii")


@app.post("/api/config/test")
async def api_test_config(spec: dict):
    """测试第三方 API 连通性（参考 CadAgent 的 Test Connection 逻辑）。
    浏览器直连第三方 API 受 CORS 限制，故统一由本地后端代理发起。
    spec: {api_url, api_key, model, mode: "text" | "ocr"}"""
    api_url = (spec.get("api_url") or "").strip().rstrip("/")
    api_key = (spec.get("api_key") or "").strip()
    model = (spec.get("model") or "").strip()
    mode = spec.get("mode", "text")

    if not api_url:
        raise HTTPException(status_code=400, detail="API 地址不能为空")

    if mode == "ocr":
        content: object = [
            {"type": "text", "text": "ping"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{_make_test_png_b64()}"}},
        ]
    else:
        content = "ping"

    payload = {
        "model": model or "test",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 5,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{api_url}/chat/completions", json=payload, headers=headers
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"无法连接: {type(e).__name__}: {e}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"HTTP {resp.status_code}: {resp.text[:300]}",
        )
    data = resp.json()
    return {"status": "ok", "model": data.get("model", model)}


@app.post("/api/pipeline/run")
async def api_run_pipeline(file_path: dict):
    # 阶段11-T2 子集：全局翻译并发上限（前端本就收口 1，此守卫防绕过/多客户端）
    if running_job_count() >= MAX_RUNNING_JOBS:
        raise HTTPException(
            status_code=429,
            detail=f"已有 {MAX_RUNNING_JOBS} 个翻译任务进行中，请等待完成后再试",
        )
    try:
        result = await run_pipeline(file_path["file_path"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pipeline/running")
async def api_pipeline_running():
    """列出运行中的翻译任务（阶段11-T5：前端 F5 丢 job_id 后据此自动重接管）。"""
    return list_running_jobs()


@app.get("/api/pipeline/status/{job_id}")
async def api_get_pipeline_status(job_id: str):
    try:
        result = await get_pipeline_status(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ocr/process")
async def api_ocr_process(file_path: dict):
    try:
        from ocr.siliconflow import call_ocr
        result = await call_ocr(file_path["file_path"], settings.ocr_config)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/translate/batch")
async def api_translate_batch(blocks: dict):
    try:
        from translate.base import translate_batch
        result = await translate_batch(blocks["texts"], blocks["source_lang"], blocks["target_lang"], settings.translate_config)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def api_get_config():
    """读取当前配置（与 Rust load_config 行为一致）。dev 桥接模式下前端用其加载配置。"""
    if os.path.exists(settings.config_path):
        with open(settings.config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "ocr": settings._default_ocr(),
        "translate": settings._default_translate(),
        "ui": {"default_mode": "bilingual", "theme": "light"},
    }


@app.post("/api/config")
async def api_set_config(payload: dict):
    """写入配置（与 Rust save_config 行为一致）。dev 模式前端保存配置时调用。"""
    parent = settings.data_dir
    os.makedirs(parent, exist_ok=True)
    with open(settings.config_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    settings.refresh()
    return {"status": "ok"}


@app.get("/api/docs")
async def api_list_docs():
    """主页"已翻译文章"列表（阶段6-T3）。file_exists 供前端标记源文件缺失。"""
    import docs_index

    docs = docs_index.load_index(settings.data_dir)
    for d in docs:
        d["file_exists"] = bool(d.get("file_path")) and os.path.isfile(d["file_path"])
    return {"docs": docs, "folders": docs_index.load_folders(settings.data_dir)}


@app.post("/api/folders")
async def api_create_folder(payload: dict):
    """新建文件夹（侧边栏分组，2026-09-09 靠岸学术风格改版）。"""
    import docs_index

    try:
        folder = docs_index.add_folder(settings.data_dir, payload.get("name"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"folder": folder}


@app.post("/api/folders/rename")
async def api_rename_folder(payload: dict):
    import docs_index

    folder_id = str(payload.get("folder_id") or "").strip()
    try:
        hit = docs_index.rename_folder(
            settings.data_dir, folder_id, payload.get("name")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not hit:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return {"ok": True}


@app.post("/api/folders/delete")
async def api_delete_folder(payload: dict):
    """删除文件夹；其中文档回到未分类（不删文档记录与缓存）。"""
    import docs_index

    folder_id = str(payload.get("folder_id") or "").strip()
    docs_index.delete_folder(settings.data_dir, folder_id)
    return {"ok": True}


@app.post("/api/docs/move")
async def api_move_doc(payload: dict):
    """移动文档到文件夹（folder_id=null 表示移出归未分类）。"""
    import docs_index

    doc_id = str(payload.get("doc_id") or "").strip()
    folder_id = payload.get("folder_id")
    folder_id = str(folder_id).strip() if folder_id else None
    if folder_id and not any(
        f.get("folder_id") == folder_id
        for f in docs_index.load_folders(settings.data_dir)
    ):
        raise HTTPException(status_code=404, detail="目标文件夹不存在")
    if not docs_index.set_doc_folder(settings.data_dir, doc_id, folder_id):
        raise HTTPException(status_code=404, detail="文档索引中不存在该记录")
    return {"ok": True}


@app.post("/api/docs/open")
async def api_open_doc(payload: dict):
    """按 doc_id 从缓存重建已翻译会话（阶段6-T3）：零 API 调用、秒开。

    源文件存在时附带坐标标注（原版模式可用）；缺失时对照/紧跟模式
    纯缓存 markdown 仍完整可用，原版模式由前端禁用并提示。
    提取缓存已清空时返回 404，前端引导重新翻译。
    """
    import docs_index
    from pipeline import processor

    doc_id = str(payload.get("doc_id") or "").strip()
    doc = docs_index.get_doc(settings.data_dir, doc_id) if doc_id else None
    if not doc:
        raise HTTPException(status_code=404, detail="文档索引中不存在该记录")
    try:
        result = await processor.open_cached_doc(
            doc.get("pdf_hash", ""),
            int(doc.get("page_count") or 0),
            doc.get("file_path") or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("重开文档失败 doc_id=%s", doc_id)
        raise HTTPException(status_code=502, detail="重开文档失败，详见后端日志")
    return {**result, "doc": doc}


# 前端日志上报：崩溃/未捕获异常落盘（打包 exe 无控制台，这是排查崩溃的主线索）
_frontend_log_lock = threading.Lock()


@app.post("/api/log")
async def api_frontend_log(payload: dict):
    """前端 window.onerror / unhandledrejection 上报，追加写 logs/frontend.log。"""
    level = str(payload.get("level") or "info").upper()[:10]
    message = str(payload.get("message") or "").replace("\n", " ")[:2000]
    log_dir = os.path.join(settings.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}\n"
    async with asyncio.to_thread(_append_frontend_log, log_dir, line):
        pass
    return {"ok": True}


def _append_frontend_log(log_dir: str, line: str) -> None:
    with _frontend_log_lock:
        with open(
            os.path.join(log_dir, "frontend.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """dev 桥接模式：浏览器无法拿到真实文件路径，故先上传到服务端临时目录，
    返回服务端绝对路径供流水线按路径读取。仅用于本地开发/测试。"""
    upload_dir = os.path.join(settings.cache_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename or "doc.pdf")[1] or ".pdf"
    dest = os.path.join(upload_dir, f"{uuid.uuid4().hex}{ext}")
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    return {"path": os.path.abspath(dest)}


@app.get("/api/file/raw")
async def api_file_raw(path: str):
    """dev 桥接模式：返回文件原始字节（缩略图用 pdfjs 通过 URL 读取；
    文本层导出的论文插图也经此接口展示）。仅本地开发使用，
    生产环境由 Tauri convertFileSrc 替代。"""
    p = path
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="file not found")
    import mimetypes
    media_type = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return FileResponse(p, media_type=media_type)


@app.get("/api/file/exists")
async def api_file_exists(path: str):
    return {"exists": os.path.isfile(path)}


# ---------------- 阶段9：BabelDOC 双语 PDF 导出 ----------------

@app.post("/api/export/babeldoc")
async def api_export_babeldoc(payload: dict):
    """启动 BabelDOC 双语 PDF 导出（阶段9-T1）。

    入参 {file_path}；复用设置里的翻译 API 配置（api_url/key/model）。
    缓存命中（<cache>/babeldoc/<pdf_hash>/<model>/ 下已有 dual PDF）瞬时返回
    done+cached=true；同一 (pdf_hash, model) 进行中任务幂等复用。
    """
    from export import babeldoc_export

    try:
        return await babeldoc_export.start_export(
            str(payload.get("file_path") or ""),
            settings.translate_config,
            settings.cache_dir,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/export/babeldoc/cached")
async def api_export_babeldoc_cached(file_path: str):
    """探测文档是否已有排版对照产物（阶段9 验收反馈：命中则免确认直接打开）。"""
    from export import babeldoc_export

    return await babeldoc_export.check_cached(
        file_path, settings.translate_config, settings.cache_dir
    )


@app.get("/api/export/babeldoc/running")
async def api_export_babeldoc_running():
    """列出运行中的排版对照导出任务（阶段11-T5 扩展：App 启动后静默重接管）。"""
    from export import babeldoc_export

    return babeldoc_export.list_running_exports()


@app.get("/api/export/babeldoc/{job_id}")
async def api_export_babeldoc_status(job_id: str):
    """导出任务进度查询。任务表在内存中，后端重启后未完成任务丢失（产物仍在缓存）。"""
    from export import babeldoc_export

    job = babeldoc_export.get_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.delete("/api/export/babeldoc/{job_id}")
async def api_export_babeldoc_cancel(job_id: str):
    from export import babeldoc_export

    if not await babeldoc_export.cancel(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return babeldoc_export.get_status(job_id)

if __name__ == "__main__":
    # 打包态（PyInstaller onefile，GUI 子系统）stdout/stderr 被 Tauri 收走后
    # 只进 println!（release 无控制台等于丢失）——2026-09-13 sidecar 启动即崩
    # （pymupdf layout 资源缺失）却无任何日志可查，血泪教训：冻结环境必须
    # 把 stderr 落盘，启动崩溃才有迹可循。
    if getattr(sys, "frozen", False):
        try:
            _log_dir = os.path.join(
                os.environ.get("APPDATA") or os.path.expanduser("~"),
                "pdf-reader", "logs",
            )
            os.makedirs(_log_dir, exist_ok=True)
            _log_path = os.path.join(_log_dir, "backend-sidecar.log")
            # 追加模式保留历史崩溃记录；启动时写分隔行便于定位会话
            _log_f = open(_log_path, "ab", buffering=0)
            sys.stdout = _log_f
            sys.stderr = _log_f
            os.dup2(_log_f.fileno(), 1)
            os.dup2(_log_f.fileno(), 2)
            _log_f.write(
                f"\n===== sidecar 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} =====\n".encode()
            )
        except Exception:
            pass  # 日志失败绝不阻断启动
    uvicorn.run(app, host="127.0.0.1", port=8000)
