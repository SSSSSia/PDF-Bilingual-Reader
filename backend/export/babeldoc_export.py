"""BabelDOC 双语 PDF 导出服务。

职责：
- POST /api/export/babeldoc          启动导出任务（子进程跑 babeldoc_worker.py）
- GET  /api/export/babeldoc/{job_id} 查询进度（内存任务表，不落盘）
- DELETE /api/export/babeldoc/{job_id} 取消（kill 子进程）

设计要点：
- 隔离：BabelDOC 依赖重（onnx 等），装在项目根 .venv-babeldoc 独立 venv；
  子进程用该 venv 的 python 跑 worker，与主后端环境完全隔离。
- 幂等缓存：产物目录 <cache>/babeldoc/<pdf_hash>/<model_slug>/，
  dual PDF 已存在即视为命中，直接返回 done（复用 产物零成本重开）。
- 同一 (pdf_hash, model) 已有 running 任务时幂等返回该任务。
- 安全：api_key 仅经 argv 传给 worker 子进程（BabelDOC 不支持环境变量读取），
  本模块所有日志/异常/任务表均不含 key。
- 进度协议：worker stdout 逐行 JSON（见 babeldoc_worker.py 模块注释），
  主后端只做转发聚合，不解析 BabelDOC 内部结构。
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid

logger = logging.getLogger("pdf-reader.export")

# 任务在内存中的生命周期 = 后端进程生命周期；重启后 GET 404，前端提示重新导出即可
# （产物本身落盘缓存，重启后重新"导出"同一 PDF 会瞬时命中缓存返回 done）
_jobs: dict[str, dict] = {}

_PUMP_TASKS: dict[str, asyncio.Task] = {}

_STATUS = ("pending", "running", "done", "error", "cancelled")
# 全局 worker 并发上限（实测单 worker RSS 峰值 ~1.5GB；原 2 为 裁剪
# 后保留值）。既定决策收到 1：前端单任务模型 + 任务可见性按
# 「正在生成的那一篇」呈现，同时只跑一篇语义最清晰，内存最稳
_MAX_WORKERS = 1


def list_running_exports() -> list[dict]:
    """运行中导出任务列表：App 启动时据此静默重接管。"""
    return [
        {
            "job_id": jid,
            "file_path": j.get("file_path") or "",
            "progress": j.get("progress", 0),
        }
        for jid, j in _jobs.items()
        if j["status"] in ("pending", "running")
    ]


def _project_root() -> str:
    # __file__ = <root>/backend/export/babeldoc_export.py → 上溯三层到项目根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _venv_python() -> str | None:
    """BabelDOC 独立 venv 的 python 路径（开发态项目 venv）；未安装返回 None。"""
    if sys.platform == "win32":
        p = os.path.join(_project_root(), ".venv-babeldoc", "Scripts", "python.exe")
    else:
        p = os.path.join(_project_root(), ".venv-babeldoc", "bin", "python")
    return p if os.path.isfile(p) else None


def venv_python(data_dir: str | None = None) -> str | None:
    """运行时 python 解析链：
    1. 开发态项目 venv .venv-babeldoc；
    2. 随包运行时：与后端 exe 同级的 babeldoc-runtime/（tauri resources 安装位，
       PyInstaller sys.executable 恒指 exe 本体，打包态即安装目录）；
    3. 数据目录 <data_dir>/babeldoc-runtime/（保留扩展位）。"""
    p = _venv_python()
    if p:
        return p
    from export import babeldoc_runtime

    candidates = [os.path.dirname(os.path.abspath(sys.executable))]
    if data_dir:
        candidates.append(data_dir)
    for parent in candidates:
        p = babeldoc_runtime.runtime_python(parent)
        if p:
            return p
    return None


def _worker_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "babeldoc_worker.py")


def _model_slug(model: str) -> str:
    """模型名转安全目录名：Qwen/Qwen3-8B -> Qwen_Qwen3-8B"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model) or "default"


def _output_dir(cache_dir: str, pdf_hash: str, model: str) -> str:
    return os.path.join(cache_dir, "babeldoc", pdf_hash, _model_slug(model))


def _find_cached_dual(out_dir: str) -> str | None:
    """命中缓存：目录里已有 *.dual.pdf 即返回（BabelDOC 命名 <stem>.<wm>.<lang>.dual.pdf）。"""
    if not os.path.isdir(out_dir):
        return None
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".dual.pdf"):
            return os.path.join(out_dir, name)
    return None


def _find_cached_mono(out_dir: str) -> str:
    if not os.path.isdir(out_dir):
        return ""
    for name in sorted(os.listdir(out_dir)):
        if name.endswith(".mono.pdf"):
            return os.path.join(out_dir, name)
    return ""


def _public(job: dict) -> dict:
    """对外序列化：剔除内部字段；任何情况下都不携带 api_key。"""
    return {
        k: v
        for k, v in job.items()
        if not k.startswith("_") and k != "api_key"
    }


def get_status(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    return _public(job) if job else None


async def cancel(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if not job:
        return False
    if job["status"] in ("done", "error", "cancelled"):
        return True
    proc: subprocess.Popen | None = job.get("_proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            logger.exception("取消导出任务失败 job_id=%s", job_id)
    job["status"] = "cancelled"
    job["finished_at"] = time.time()
    job["message"] = "已取消"
    return True


async def start_export(file_path: str, translate_config: dict, cache_dir: str) -> dict:
    """启动（或命中缓存/幂等复用）一个 BabelDOC 导出任务。失败抛 ValueError/FAQ。"""
    file_path = (file_path or "").strip()
    if not file_path or not os.path.isfile(file_path):
        raise ValueError("文件不存在")
    if not file_path.lower().endswith(".pdf"):
        raise ValueError("仅支持 PDF 文件")

    api_url = (translate_config.get("api_url") or "").strip().rstrip("/")
    api_key = (translate_config.get("api_key") or "").strip()
    model = (translate_config.get("model") or "").strip()
    if not api_url or not api_key or not model:
        raise ValueError("API 配置不完整（api_url / api_key / model），请先在设置中完成配置")

    # 局部变量不得与模块级 venv_python 同名（同名赋值会把函数内该名字
    # 整体变为局部变量，RWS 调用即 UnboundLocalError——重构
    # 引入，导出启动恒 500，人工验收首次实测发现）
    runtime_python = venv_python(os.path.dirname(os.path.abspath(cache_dir)))
    if not runtime_python:
        raise ValueError(
            "BabelDOC 运行时缺失（正常安装包内自带）——请重新下载完整版安装包"
        )

    from cache.file_cache import file_hash

    pdf_hash = await asyncio.to_thread(file_hash, file_path)
    out_dir = _output_dir(cache_dir, pdf_hash, model)

    # 缓存命中：产物已存在，直接返回 done（幂等，零成本）
    cached_dual = _find_cached_dual(out_dir)
    if cached_dual:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "done",
            "progress": 100.0,
            "stage": "",
            "message": "缓存命中",
            "cached": True,
            "file_path": file_path,
            "pdf_hash": pdf_hash,
            "model": model,
            "dual_path": cached_dual,
            "mono_path": _find_cached_mono(out_dir),
            "created_at": time.time(),
            "finished_at": time.time(),
        }
        _jobs[job_id] = job
        logger.info("BabelDOC 缓存命中 file=%s model=%s", os.path.basename(file_path), model)
        return _public(job)

    # 幂等：同一 (pdf_hash, model) 已有进行中任务则直接复用
    for job in _jobs.values():
        if (
            job["pdf_hash"] == pdf_hash
            and job["model"] == model
            and job["status"] in ("pending", "running")
        ):
            return _public(job)

    # 子集（裁剪后保留）：全局 worker 并发上限。实测单 worker
    # RSS 峰值 ~1.5GB，并发叠加是 内存耗尽崩溃的同源风险；
    # 超限直接拒绝（前端错误卡提示可读），不做复杂排队
    running = sum(1 for j in _jobs.values() if j["status"] in ("pending", "running"))
    if running >= _MAX_WORKERS:
        raise ValueError(
            f"已有 {running} 个排版对照任务进行中（上限 {_MAX_WORKERS}），"
            "请等待完成或取消后再试"
        )

    os.makedirs(out_dir, exist_ok=True)
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "pending",
        "progress": 0.0,
        "stage": "启动中",
        "message": "",
        "cached": False,
        "file_path": file_path,
        "pdf_hash": pdf_hash,
        "model": model,
        "dual_path": "",
        "mono_path": "",
        "log_path": os.path.join(out_dir, "worker.log"),
        "created_at": time.time(),
        "finished_at": None,
    }
    _jobs[job_id] = job

    # worker 诊断日志直接落盘（关键：绝不能用 PIPE 且不读——BabelDOC 日志
    # 写满 64KB 管道缓冲后 worker 会永久阻塞在 stderr 写上，表现为"假死"
    # 卡在某个百分比。真实论文 1h 不完成即此根因，实证
    # cache.v1.db-wal 在 19:41 后停止增长而进程存活）。
    log_path = os.path.join(out_dir, "worker.log")
    log_fh = open(log_path, "a", encoding="utf-8")
    cmd = [
        runtime_python,
        _worker_path(),
        "--input", file_path,
        "--output-dir", out_dir,
        "--base-url", api_url,
        "--api-key", api_key,   # 仅进 argv，绝不写日志/任务表
        "--model", model,
        "--qps", "6",           # 429：BabelDOC 内部 tenacity 重试兜底 + worker 应用层 qps 减半重跑（②）
        "--max-pages-per-part", "200",  # ③：大文档分批（未超页数不分批），进度/产物自动聚合合并
        "--lang-in", "en",      # 范围：英文学术论文 → 中文
        "--lang-out", "zh",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=log_fh,
            cwd=_project_root(),
        )
    except OSError as e:
        log_fh.close()
        job["status"] = "error"
        job["finished_at"] = time.time()
        job["message"] = f"无法启动 BabelDOC 子进程: {e}"
        logger.exception("BabelDOC 子进程启动失败 job_id=%s", job_id)
        return _public(job)

    job["_proc"] = proc
    job["_log_fh"] = log_fh
    job["status"] = "running"
    logger.info(
        "BabelDOC 导出启动 job_id=%s file=%s model=%s（key 已隐去）",
        job_id, os.path.basename(file_path), model,
    )
    _PUMP_TASKS[job_id] = asyncio.create_task(_pump(job, proc))
    return _public(job)


async def _pump(job: dict, proc: subprocess.Popen) -> None:
    """读取 worker stdout 的 JSON 行，更新进度；进程结束后收敛任务状态。"""
    job_id = job["job_id"]
    last_decile = -1
    try:
        assert proc.stdout is not None
        while True:
            line = await asyncio.to_thread(proc.stdout.readline)
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = event.get("type")
            if etype == "ready":
                job["stage"] = "加载布局模型"
                job["progress"] = 1.0
            elif etype == "progress":
                overall = float(event.get("overall") or 0)
                job["progress"] = max(job.get("progress", 0), min(99.0, overall))
                job["stage"] = str(event.get("stage") or job.get("stage") or "")
                # 子集：worker RSS 阈值告警（每 job 一次）。
                # 崩溃实证单 worker 峰值 ~1.5GB，超 2GB 即向崩溃工况演进
                rss_mb = float(event.get("rss_mb") or 0)
                if rss_mb >= 2048 and not job.get("_rss_warned"):
                    job["_rss_warned"] = True
                    logger.warning(
                        "BabelDOC worker RSS 告警 job_id=%s %.0fMB（阈值 2048MB）",
                        job_id, rss_mb,
                    )
                # 每 10% 落一行后端日志
                decile = int(job["progress"] // 10)
                if decile > last_decile:
                    last_decile = decile
                    logger.info(
                        "BabelDOC 进度 job_id=%s %s%% stage=%s",
                        job_id, round(job["progress"]), job["stage"],
                    )
            elif etype == "finish":
                job["progress"] = 100.0
                job["stage"] = ""
                job["status"] = "done"
                job["dual_path"] = str(event.get("dual") or "")
                job["mono_path"] = str(event.get("mono") or "")
                job["finished_at"] = time.time()
                logger.info("BabelDOC 导出完成 job_id=%s dual=%s", job_id, job["dual_path"])
            elif etype == "error":
                job["status"] = "error"
                job["message"] = str(event.get("error") or "未知错误")
                job["finished_at"] = time.time()
                logger.error("BabelDOC 导出失败 job_id=%s: %s", job_id, job["message"])

        # stdout 关闭 = 进程即将退出；等退出码收敛最终状态
        code = await asyncio.to_thread(proc.wait)
        if job["status"] in ("pending", "running"):
            job["status"] = "error"
            job["finished_at"] = time.time()
            if code == 0:
                job["message"] = "子进程结束但未产出结果"
            else:
                stderr_tail = _read_log_tail(job)
                job["message"] = f"子进程异常退出（code={code}）{stderr_tail}"
            logger.error("BabelDOC 导出失败 job_id=%s: %s", job_id, job["message"])
    except Exception:  # noqa: BLE001 — 泵任务兜底：任何异常都收敛为 error 而非悬空 running
        logger.exception("BabelDOC 进度泵异常 job_id=%s", job_id)
        if job["status"] in ("pending", "running"):
            job["status"] = "error"
            job["message"] = "内部错误（进度读取异常）"
            job["finished_at"] = time.time()
    finally:
        fh = job.pop("_log_fh", None)
        if fh:
            try:
                fh.close()
            except OSError:
                pass


def _read_log_tail(job: dict, limit: int = 400) -> str:
    """worker.log 末尾片段（诊断用；stderr 已改落盘不再走 PIPE）。"""
    try:
        with open(job["log_path"], encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4000))
            return f.read()[-limit:].replace("\n", " ")
    except OSError:
        return ""


async def check_cached(file_path: str, translate_config: dict, cache_dir: str) -> dict:
    """探测文档是否已有排版对照产物。

    前端进入「原版PDF·左右对照」时先调本接口：命中则免确认直接打开，
    未命中才弹「开始生成」确认卡。只读探测，无副作用。
    """
    file_path = (file_path or "").strip()
    model = (translate_config.get("model") or "").strip()
    if not file_path or not os.path.isfile(file_path) or not model:
        return {"cached": False, "dual_path": "", "mono_path": ""}

    from cache.file_cache import file_hash

    pdf_hash = await asyncio.to_thread(file_hash, file_path)
    out_dir = _output_dir(cache_dir, pdf_hash, model)
    dual = _find_cached_dual(out_dir)
    return {
        "cached": bool(dual),
        "dual_path": dual or "",
        "mono_path": _find_cached_mono(out_dir) if dual else "",
    }
