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
import shutil
import sys
import json
import uuid

import httpx

# 冻结态（PyInstaller）stderr/stdout 尽早落盘：onefile + GUI 子系统下若崩溃
# 发生在任何日志管道建立之前将无任何痕迹（排查 sidecar 段错误的
# 教训）。放在模块顶部，让导入期崩溃也能留下遗言。
if getattr(sys, "frozen", False):
    try:
        import faulthandler
        import io

        faulthandler.enable()  # 段错误等 native 崩溃时自动打印 Python 栈
        _log_dir = os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"),
            "pdf-reader", "logs",
        )
        os.makedirs(_log_dir, exist_ok=True)
        _log_f = open(
            os.path.join(_log_dir, "backend-sidecar.log"), "ab", buffering=0
        )
        # TextIOWrapper 包装：raw 文件对象收不了 print 的 str（TypeError）
        _log_t = io.TextIOWrapper(
            _log_f, encoding="utf-8", errors="replace", line_buffering=True
        )
        sys.stdout = _log_t
        sys.stderr = _log_t
        os.dup2(_log_f.fileno(), 1)
        os.dup2(_log_f.fileno(), 2)
        _log_t.write(
            f"\n===== sidecar 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"pid={os.getpid()} =====\n"
        )
    except Exception:
        pass  # 日志失败绝不阻断启动

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
# 端点失败原因不再只存在于 HTTP 响应里。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pdf-reader")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await settings.init()
    # 启动即打印实际生效的数据目录，兜底轨激活时显式告警
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
    """按需生成"译制图"。

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
    """块级公式识别。

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
    """单块手动翻译/重翻。

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
    # 与全文管线一致：数学字母区规范化后再保护/翻译/算缓存键
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
    # 子集：全局翻译并发上限（前端本就收口 1，此守卫防绕过/多客户端）
    if running_job_count() >= MAX_RUNNING_JOBS:
        raise HTTPException(
            status_code=429,
            detail=f"已有 {MAX_RUNNING_JOBS} 个翻译任务进行中，请等待完成后再试",
        )
    # 上传即入库：源 PDF 复制进 <data_dir>/files/，此后
    # 管线/阅读/导出全用库内副本——原文件移动/改名/删除零影响；原始
    # 文件名作为展示名传入（标题/文献库显示用）
    fp = str(file_path.get("file_path") or "")
    if not os.path.isfile(fp):
        raise HTTPException(
            status_code=400,
            detail="源文件不存在（可能已被移动、改名或删除）。请在文献库重新上传该 PDF，翻译缓存仍在、重传即恢复。",
        )
    display = os.path.basename(fp)
    try:
        from library import materialize

        fp = materialize(fp, settings.data_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"入库失败：{e}")
    try:
        result = await run_pipeline(fp, display_name=display)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pipeline/running")
async def api_pipeline_running():
    """列出运行中的翻译任务。"""
    return list_running_jobs()


@app.post("/api/pipeline/cancel")
async def api_cancel_pipeline(payload: dict):
    """取消进行中的翻译任务（删除文献联动）。任务不存在/已结束返回 ok=false。"""
    from pipeline import processor

    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="缺少 job_id")
    return {"ok": processor.cancel_job(job_id)}


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
    """主页"已翻译文章"列表。file_exists 供前端标记源文件缺失。"""
    import docs_index

    docs = docs_index.load_index(settings.data_dir)
    for d in docs:
        d["file_exists"] = bool(d.get("file_path")) and os.path.isfile(d["file_path"])
    return {"docs": docs, "folders": docs_index.load_folders(settings.data_dir)}


@app.post("/api/folders")
async def api_create_folder(payload: dict):
    """新建文件夹。"""
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


@app.post("/api/docs/rename")
async def api_rename_doc(payload: dict):
    """改文献显示名：仅改索引标题，不动源文件。"""
    import docs_index

    doc_id = str(payload.get("doc_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if not docs_index.rename_doc(settings.data_dir, doc_id, title):
        raise HTTPException(status_code=404, detail="文档索引中不存在该记录")
    return {"ok": True, "title": title[:100]}


@app.post("/api/docs/delete")
async def api_delete_doc(payload: dict):
    """删除文献：移除索引条目 + 快照图缓存 + 文献库源副本。

    提取/翻译缓存按内容哈希寻址、与单篇文献无绑定关系，保留——
    删除后重新上传同一文件立即缓存命中恢复，反悔零成本。
    """
    import docs_index

    doc_id = str(payload.get("doc_id") or "").strip()
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    doc = docs_index.get_doc(settings.data_dir, doc_id)
    if not docs_index.delete_doc(settings.data_dir, doc_id):
        raise HTTPException(status_code=404, detail="文档索引中不存在该记录")
    # 快照图缓存（cache/images/<doc_id>）
    shutil.rmtree(
        os.path.join(settings.cache_dir, "images", doc_id), ignore_errors=True
    )
    # 文献库源副本：仅清 <data_dir>/files/ 之内的库文件，
    # 用户本地原文件（docs_index 里存的历史路径）绝不动
    fp = str((doc or {}).get("file_path") or "")
    files_root = os.path.join(settings.data_dir, "files")
    if (
        fp.startswith(files_root + os.sep)
        and os.path.isfile(fp)
        and os.path.commonpath([fp, files_root]) == files_root
    ):
        try:
            os.remove(fp)
        except OSError:
            pass  # 库文件删除失败不阻断条目删除
    return {"ok": True}


@app.post("/api/docs/open")
async def api_open_doc(payload: dict):
    """按 doc_id 从缓存重建已翻译会话：零 API 调用、秒开。

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
    page_count = int(doc.get("page_count") or 0)
    if not page_count:
        # 自愈：索引记录缺页数（历史版本完成钩子写失败的半条记录）→
        # 从库内副本读真实页数重建；成功后回写修复记录，卡片元信息随之恢复
        fp = doc.get("file_path") or ""
        if fp and os.path.isfile(fp):
            try:
                import pymupdf

                with pymupdf.open(fp) as pdf:
                    page_count = len(pdf)
                doc = {**doc, "page_count": page_count, "status": "done"}
                docs_index.upsert_doc(settings.data_dir, doc)
                logger.info("重开时修复缺页数记录 doc_id=%s page_count=%d", doc_id, page_count)
            except Exception:
                logger.warning("修复缺页数记录失败 doc_id=%s", doc_id, exc_info=True)
    try:
        result = await processor.open_cached_doc(
            doc.get("pdf_hash", ""),
            page_count,
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
    await asyncio.to_thread(_append_frontend_log, log_dir, line)
    return {"ok": True}


def _append_frontend_log(log_dir: str, line: str) -> None:
    with _frontend_log_lock:
        with open(
            os.path.join(log_dir, "frontend.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """dev 桥接模式：浏览器无法拿到真实文件路径，故先上传到服务端，
    返回绝对路径供流水线按路径读取。仅用于本地开发/测试。

     起直接落文献库（<data_dir>/files/<内容sha1>.pdf，去重），
    不再进临时目录——后续管线/重开全用库内副本。"""
    import hashlib

    from library import library_dir

    content = await file.read()
    dest = os.path.join(
        library_dir(settings.data_dir), f"{hashlib.sha1(content).hexdigest()}.pdf"
    )
    if not os.path.isfile(dest):
        with open(dest, "wb") as f:
            f.write(content)
    return {"path": dest}


@app.post("/api/library/import")
async def api_library_import(payload: dict):
    """BabelDOC 上传模式入库：源 PDF 复制进文献库 + 登记索引（reader=babeldoc）。

    不启动自研翻译管线——该模式只跑 BabelDOC 对照生成（解析与翻译全由
    BabelDOC 独立完成，不复用也不产出应用翻译缓存）。status 从 translating
    起，生成完成由 babeldoc_export 完成钩子置 done，前端无需回写。
    同内容重复上传按内容哈希去重，登记幂等。"""
    import docs_index
    from library import materialize

    fp = str(payload.get("file_path") or "")
    if not os.path.isfile(fp):
        raise HTTPException(
            status_code=400,
            detail="源文件不存在（可能已被移动、改名或删除）。请重新选择该 PDF。",
        )
    try:
        fp = materialize(fp, settings.data_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"入库失败：{e}")
    from cache.file_cache import file_hash

    pdf_hash = await asyncio.to_thread(file_hash, fp)
    # 页数直接读源 PDF：该模式不走 /api/docs/open 自愈路径，页数必须在
    # 入库时就落档，否则文献卡会一直显示缺页数
    page_count = 0
    try:
        import pymupdf

        with pymupdf.open(fp) as pdf:
            page_count = len(pdf)
    except Exception:
        logger.warning("入库读取页数失败 file=%s", fp, exc_info=True)
    title = str(payload.get("title") or "").strip()
    if not title:
        title = os.path.splitext(os.path.basename(fp))[0]
    # upsert 是整条替换：带旧记录字段（page_count 等）以免丢失
    old = docs_index.get_doc(settings.data_dir, pdf_hash[:16]) or {}
    docs_index.upsert_doc(
        settings.data_dir,
        {
            **old,
            "doc_id": pdf_hash[:16],
            "title": title,
            "file_path": fp,
            "pdf_hash": pdf_hash,
            "file_mtime": int(os.path.getmtime(fp)),
            "page_count": page_count,
            "status": "translating",
            "reader": "babeldoc",
        },
    )
    return {"path": fp, "doc_id": pdf_hash[:16]}


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


# ---------------- BabelDOC 双语 PDF 导出 ----------------

@app.post("/api/export/babeldoc")
async def api_export_babeldoc(payload: dict):
    """启动 BabelDOC 双语 PDF 导出。

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
            settings.data_dir,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/export/babeldoc/cached")
async def api_export_babeldoc_cached(file_path: str):
    """探测文档是否已有排版对照产物。"""
    from export import babeldoc_export

    return await babeldoc_export.check_cached(
        file_path, settings.translate_config, settings.cache_dir
    )


@app.get("/api/export/babeldoc/running")
async def api_export_babeldoc_running():
    """列出运行中的排版对照导出任务。"""
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

# ---------------- 开发/随包启动：端口占用自愈 ----------------


def _looks_like_our_backend(image_name: str, cmdline: str) -> bool:
    """判断端口占用进程是否为本应用后端。只自动结束可确认身份的进程：
    - 随包 sidecar：映像名/命令行含 pdf-backend（pdf-backend-od.exe 等）；
    - 开发后端：python 进程且命令行含 backend + main.py。
    其余（未知程序占用 8000）一律不碰。"""
    n = (image_name or "").lower()
    c = (cmdline or "").lower()
    if n.startswith(("pdf-backend", "pdf_backend")) or "pdf-backend" in c:
        return True
    if n.startswith("python") and "backend" in c and "main.py" in c:
        return True
    return False


def _process_identity(pid: int) -> tuple[str, str]:
    """(映像名, 命令行)。查询失败返回空串（随后按身份不匹配处理）。

    显式 errors="replace"：系统工具输出为本地码页（中文 Windows GBK），
    子进程默认解码可能撞 UnicodeDecodeError（实测 uv 环境按 UTF-8 解码）。"""
    import subprocess as _sp

    try:
        r = _sp.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        first = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
        image = first.split('","')[0].strip('"') if '","' in first else ""
    except Exception:
        image = ""
    try:
        r = _sp.run(
            ["wmic", "process", "where", f"processid={pid}", "get", "commandline", "/value"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        cmdline = next(
            (ln.split("=", 1)[1] for ln in (r.stdout or "").splitlines()
             if ln.startswith("CommandLine=")),
            "",
        )
    except Exception:
        cmdline = ""
    if not cmdline:
        try:
            r = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            cmdline = (r.stdout or "").strip()
        except Exception:
            pass
    return image, cmdline


def _free_port(port: int) -> None:
    """启动前清理占用端口的旧后端进程（残留实例自愈）。

    仅 Windows（开发与随包均实际只跑 Windows）；逐 PID 核身份后 taskkill，
    身份不明者不杀、报错退出（uvicorn 否则只会抛裸 bind 异常）。"""
    if sys.platform != "win32":
        return
    import subprocess as _sp

    try:
        out = _sp.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        print(f"警告：无法枚举端口占用（netstat 失败），跳过端口 {port} 自动清理")
        return
    pids = set()
    for line in (out or "").splitlines():
        parts = line.split()
        # TCP  127.0.0.1:8000  0.0.0.0:0  LISTENING  <pid>
        if (
            len(parts) >= 5
            and parts[3] == "LISTENING"
            and parts[1].rsplit(":", 1)[-1] == str(port)
        ):
            pids.add(parts[4])
    unknown: list[tuple[str, str]] = []
    for pid_s in pids:
        pid = int(pid_s)
        if pid == os.getpid():
            continue
        image, cmdline = _process_identity(pid)
        if _looks_like_our_backend(image, cmdline):
            try:
                _sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=15)
                print(f"已结束残留后端进程 PID={pid}（{image or 'backend'}），释放端口 {port}")
            except Exception:
                unknown.append((image, f"PID={pid} 结束失败"))
        else:
            unknown.append((image or "?", f"PID={pid}"))
    if unknown:
        detail = "；".join(f"{name}（{info}）" for name, info in unknown)
        print(
            f"端口 {port} 被其它程序占用：{detail}。"
            "自动清理仅限本应用后端进程，请手动关闭占用者后重试。"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    _free_port(8000)
    uvicorn.run(app, host="127.0.0.1", port=8000)
