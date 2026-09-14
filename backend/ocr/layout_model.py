r"""版面检测信号源接入层（阶段12-T9.1）。

DocLayout-YOLO 经随包 BabelDOC 运行时子进程推理（layout_worker.py），
主后端只消费 NDJSON 区域 JSON——onnxruntime 永不进主后端 venv（打包
段错误事故防复发，2026-09-13）。区域按页缓存（伪模型名 doclayout-v1，
ocr_key 内容寻址），回归重放/重开文档零成本。

区域 schema（PDF 点坐标）：
    {"label": "title"|"abandon"|"table"|..., "conf": 0.0~1.0,
     "bbox": [x0, y0, x1, y1]}

失败语义（T9.4 兜底的入口）：运行时缺失 / worker 崩溃 / 停止输出 →
regions() 对该页恒返回 None，调用方（vlm_parse）自动落回字号证据——
版面模型是结构增强信号，不是管线依赖，任何失败都不阻断提取。
"""
import asyncio
import json
import os

from cache.file_cache import ocr_key, read_cache, write_cache

# 区域缓存的伪模型名（ocr_key 的 model 位）。仅在区域产出协议变化
# （类别表/坐标口径/去重规则）时 bump 版本后缀。
LAYOUT_CACHE_MODEL = "doclayout-v1"

# worker 单行输出静默上限（秒）：worker 每页必出一行（含空 regions），
# 静默即挂死——首行前的模型加载 ~3s + 偶发权重下载，150s 足够宽。
LINE_SILENCE_TIMEOUT = 150.0

# ── 区域去重（阶段12-T9.3，纯几何规则）─────────────────────────────
# 决策文档 §2 已知边界：模型偶发对同一区域复检两次（其一置信 0.3~0.6）；
# "Algorithm 1" 伪代码框被判低置信 table（0.26~0.42）。真实信号实测
# 0.72+（title 0.84-0.95 / abandon 0.72-0.92 / table 0.76-0.97）——
# 0.6 下限滤除复检框与伪代码框（不误伤也拿不到快照，登记观察项），
# 同 label IoU>0.6 只保留置信最高者；跨 label 不判重（消费端分通道
# 各有豁免逻辑，title 与 plain text 区域重叠是常态）。
REGION_CONF_FLOOR = 0.6
REGION_IOU_DEDUPE = 0.6


def _iou(a: list, b: list) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(ix1 - ix0, 0.0) * max(iy1 - iy0, 0.0)
    if inter <= 0:
        return 0.0
    area_a = max(a[2] - a[0], 0.0) * max(a[3] - a[1], 0.0)
    area_b = max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def dedupe_regions(regions: list) -> list:
    """置信下限 + 同 label IoU 判重。输入顺序无关，输出按 (y0, x0) 稳定排序。"""
    kept: list[dict] = []
    candidates = [
        r for r in regions or []
        if isinstance(r, dict) and isinstance(r.get("bbox"), (list, tuple))
        and len(r["bbox"]) == 4
        and float(r.get("conf") or 0.0) >= REGION_CONF_FLOOR
    ]
    for reg in sorted(candidates, key=lambda r: -float(r.get("conf") or 0.0)):
        if any(
            k.get("label") == reg.get("label") and _iou(k["bbox"], reg["bbox"]) > REGION_IOU_DEDUPE
            for k in kept
        ):
            continue
        kept.append(reg)
    kept.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return kept


def _worker_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_worker.py")


class LayoutProvider:
    """一次文档提取共用的版面区域提供者（页级流式 + 按页缓存）。

    用法：
        provider = LayoutProvider(file_path, pdf_hash, cache_dir, data_dir)
        await provider.start([0, 1, 2, ...])   # 缓存优先，缺页起 worker
        regions = await provider.regions(pno)  # list[dict] | None
        await provider.close()                 # 任务结束/异常时回收

    start() 幂等；regions() 对未请求的页返回 None。子进程产出经
    test 注入位 _spawn 替换（生产为 asyncio.create_subprocess_exec）。
    """

    def __init__(
        self,
        file_path: str,
        pdf_hash: str,
        cache_dir: str,
        data_dir: str | None = None,
        python_exe: str | None = None,
    ):
        self.file_path = file_path
        self.pdf_hash = pdf_hash
        self.cache_dir = cache_dir
        self.data_dir = data_dir
        self._python_exe = python_exe
        # page -> Future（list[dict] | None）。缓存命中的页 start 时即 resolve。
        self._futures: dict[int, asyncio.Future] = {}
        self._proc = None
        self._reader: asyncio.Task | None = None
        self._stderr_pump: asyncio.Task | None = None
        self._started = False
        self._closing = False  # 正常收尾中：reader 因 EOF 退出不再按失败处理
        # idle → running → done（正常收尾）/ failed（worker 失败）/
        # unavailable（运行时缺失）；regions 可用的判定只看 future 值。
        self.status = "idle"
        self.fail_reason = ""
        self.stats = {"cached": 0, "model": 0, "used": 0}

    # ── 生命周期 ────────────────────────────────────────────────────

    async def start(self, pages: list[int]) -> None:
        """缓存优先：命中页直接 resolve，缺页集合非空才起 worker。"""
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        missing: list[int] = []
        for pno in pages:
            fut = loop.create_future()
            self._futures[pno] = fut
            hit = read_cache(self.cache_dir, ocr_key(self.pdf_hash, pno, LAYOUT_CACHE_MODEL))
            if hit and isinstance(hit.get("regions"), list):
                fut.set_result(hit["regions"])
                self.stats["cached"] += 1
            else:
                missing.append(pno)
        if not missing:
            self.status = "done"
            return
        py = self._python_exe or self._resolve_runtime_python()
        if not py:
            self.status = "unavailable"
            self.fail_reason = "babeldoc 运行时未找到（venv/exe 同级/数据目录三链均缺失）"
            for pno in missing:
                self._futures[pno].set_result(None)
            return
        try:
            self._proc = await self._spawn(py, missing)
        except Exception as e:  # noqa: BLE001 —— 起进程失败即整体降级
            self.status = "failed"
            self.fail_reason = f"worker 启动失败: {e}"
            for pno in missing:
                self._futures[pno].set_result(None)
            return
        self.status = "running"
        self._stderr_pump = asyncio.create_task(self._pump_stderr())
        self._reader = asyncio.create_task(self._read_pages(missing))

    def _resolve_runtime_python(self) -> str | None:
        """随包 BabelDOC 运行时 python（与导出功能同一条解析链）。"""
        try:
            from export.babeldoc_export import venv_python

            return venv_python(self.data_dir)
        except Exception:  # noqa: BLE001 —— 解析链自身异常按缺失处理
            return None

    async def _spawn(self, python_exe: str, pages: list[int]):
        pages_arg = ",".join(str(p) for p in pages)
        return await asyncio.create_subprocess_exec(
            python_exe,
            _worker_path(),
            self.file_path,
            pages_arg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # ── stdout NDJSON 消费 ──────────────────────────────────────────

    async def _read_pages(self, missing: list[int]) -> None:
        pending = set(missing)
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._proc.stdout.readline(), timeout=LINE_SILENCE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    self._fail(f"worker 静默超时（>{LINE_SILENCE_TIMEOUT:.0f}s 无输出）")
                    return
                if not raw:
                    if self._closing:
                        return  # close() 主动杀进程导致的 EOF，不算失败
                    # EOF 且未收到 done：worker 中途退出
                    self._fail(f"worker 提前退出（returncode={self._proc.returncode}）")
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    print(f"[layout] 非 JSON 行（忽略）: {line[:120]}")
                    continue
                if "error" in msg:
                    self._fail(f"worker 上报错误: {msg['error']}")
                    return
                if msg.get("done"):
                    self.status = "done"
                    for pno in pending:
                        self._futures[pno].set_result(None)
                    return
                pno = msg.get("page")
                if not isinstance(pno, int) or pno not in pending:
                    continue  # 重复/未请求页：以首个为准
                pending.discard(pno)
                regions = msg.get("regions")
                if not isinstance(regions, list):
                    regions = []
                write_cache(
                    self.cache_dir,
                    ocr_key(self.pdf_hash, pno, LAYOUT_CACHE_MODEL),
                    {"regions": regions},
                )
                self.stats["model"] += 1
                if not self._futures[pno].done():
                    self._futures[pno].set_result(regions)
        finally:
            # 先摘除自身引用再 close，防 close() 对当前任务 cancel/await
            self._reader = None
            await self.close()

    async def _pump_stderr(self) -> None:
        """stderr 只转发不解析（权重下载进度等），防管道写满死锁。"""
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"[layout-worker] {text[:200]}")
        except Exception:  # noqa: BLE001 —— 泵失败不影响主流程
            pass

    def _fail(self, reason: str) -> None:
        if self.status in ("done", "failed"):
            return
        self.status = "failed"
        self.fail_reason = reason
        print(f"[layout] 版面模型降级: {reason}")
        for fut in self._futures.values():
            if not fut.done():
                fut.set_result(None)
        self._kill_proc()

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # ── 消费接口 ────────────────────────────────────────────────────

    async def regions(self, page: int) -> list[dict] | None:
        """该页版面区域（T9.3 去重后）；未请求/无产出/失败 → None（走兜底）。

        去重放在读取口而非写入口：缓存保持模型原始产出，去重规则升级
        无需失效区域缓存，且缓存命中的旧条目同样被覆盖。"""
        fut = self._futures.get(page)
        if fut is None:
            return None
        regions = await fut
        if regions:
            regions = dedupe_regions(regions)
            self.stats["used"] += 1
        return regions

    async def close(self) -> None:
        """回收子进程与后台任务（幂等；正常收尾后 worker 已自行退出）。"""
        self._closing = True
        current = asyncio.current_task()
        if self._reader is not None and not self._reader.done():
            self._kill_proc()  # 收尾路径取消时确保子进程不残留
            self._reader.cancel()
        for task in (self._reader, self._stderr_pump):
            if task is None or task.done() or task is current:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                self._kill_proc()
        self._reader = None
        self._stderr_pump = None
