"""BabelDOC 导出 worker。

在 .venv-babeldoc 的 Python 里运行（BabelDOC 依赖隔离，不污染主后端环境），
直接调用 BabelDOC Python API（async_translate）而非 CLI 子进程——
原因：CLI 进度走 rich 渲染无法稳定解析；Python API 的 async_translate
逐事件产出 overall_progress(0-100)，天然适合 JSON 行协议上报。

stdout 协议（每行一个 JSON，供主后端逐行读取）：
  {"type": "ready"}                                    启动成功（模型加载完毕）
  {"type": "progress", "stage": "...", "overall": 42}  总进度 0-100
  {"type": "finish", "dual": "...", "mono": "..."}     成功，产物绝对路径
  {"type": "error", "error": "..."}                    失败
诊断/日志一律走 stderr，保证 stdout 纯净。

失败边界：
  ① 布局模型权重下载失败 → error 行带可执行引导（缓存路径/上游/离线资产命令）；
  ② 免费档 429 限流 → 应用层 qps 减半重跑整次尝试，最多 3 次
     （BabelDOC 内部 tenacity 100 次指数退避仍是第一道防线）；
  ③ 大文档分批：--max-pages-per-part 超页数自动切片，进度由 BabelDOC 父
     监视器按批加权聚合，产物由 ResultMerger 合并为单个 dual.pdf。

API key 仅经 argv 传入本进程（BabelDOC 不支持环境变量读取），
主后端负责不把它写进任何日志。
"""

import argparse
import asyncio
import json
import os
import sys
import time


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _clean_partial_outputs(output_dir: str) -> None:
    """重试前清理上次尝试可能残留的半成品产物，防产物看门狗误命中旧文件。"""
    if not os.path.isdir(output_dir):
        return
    for name in os.listdir(output_dir):
        if name.endswith(".dual.pdf") or name.endswith(".mono.pdf"):
            try:
                os.remove(os.path.join(output_dir, name))
            except OSError:
                pass


def _is_rate_limit_error(msg: str) -> bool:
    low = msg.lower()
    return "429" in low or "ratelimit" in low or "rate limit" in low


def _rss_mb() -> float:
    """当前进程 WorkingSet（MB）。仅 Windows（psapi）；失败/非 Windows 返回 0
    （主后端据此跳过告警）。子集：RSS 阈值告警的数据来源。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1_048_576
    except Exception:
        pass
    return 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="源 PDF 绝对路径")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--qps", type=float, default=2)
    parser.add_argument(
        "--max-pages-per-part",
        type=int,
        default=None,
        dest="max_pages_per_part",
        help="大文档分批：每批最大页数（不传 = 不分批）",
    )
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="zh")
    args = parser.parse_args()

    # BabelDOC 的 import 在隔离进程内完成，无所谓启动耗时
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.high_level import async_translate
    from babeldoc.format.pdf.translation_config import (
        TranslationConfig,
        WatermarkOutputMode,
    )
    from babeldoc.translator.translator import OpenAITranslator

    translator = OpenAITranslator(
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
    )

    # ①：首跑布局模型权重下载失败（断网/上游不可达/校验失败）时
    # 给出可执行的引导文案，而不是把底层异常原样抛给前端用户
    try:
        doc_layout_model = DocLayoutModel.load_onnx()
    except Exception as e:  # noqa: BLE001
        _emit({
            "type": "error",
            "error": (
                "布局模型（DocLayout-YOLO）加载失败，大概率是首次运行的权重下载失败："
                f"{type(e).__name__}: {e}。处理建议："
                "① 检查网络后重试（自动依次尝试 huggingface / hf-mirror / modelscope "
                "三个下载源）；"
                "② 可手动下载 doclayout_yolo_docstructbench_imgsz1024.onnx 放入 "
                "~/.cache/babeldoc/models/（Windows 为 "
                "%USERPROFILE%\\.cache\\babeldoc\\models\\）；"
                "③ 离线机器可用官方 CLI 预生成资产包后在本机恢复："
                "babeldoc --generate-offline-assets <目录> / "
                "--restore-offline-assets <压缩包>"
            ),
        })
        _force_exit()

    split_strategy = None
    if args.max_pages_per_part:
        # ③：大文档分批——超页数自动切片；进度由 BabelDOC 父监视器
        # 按批加权聚合（overall 不归零），产物由 ResultMerger 合并为单个 dual.pdf
        split_strategy = TranslationConfig.create_max_pages_per_part_split_strategy(
            args.max_pages_per_part
        )

    _emit({"type": "ready"})

    async def run_attempt(qps: float) -> tuple[dict | None, str | None]:
        """单次完整翻译尝试。"""
        config = TranslationConfig(
            translator=translator,
            input_file=args.input,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            doc_layout_model=doc_layout_model,
            output_dir=args.output_dir,
            qps=qps,
            use_rich_pbar=False,           # tqdm/rich 都不需要，进度走事件流
            auto_extract_glossary=False,   # 决策：省 token，术语表后续增强
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            # 与 实测一致：Qwen3-8B 关闭思考链，避免 <think> 污染译文
            custom_system_prompt=(
                "/no_think You are a professional academic paper translator. "
                "Translate the input English text into Simplified Chinese. "
                "Keep formulas, citations, references and proper nouns unchanged. "
                "Output only the translation."
            ),
            split_strategy=split_strategy,
        )

        # ---- 事件消费 + 产物看门狗双轨 ----
        # 实测：BabelDOC 0.6.4 以 Python API 调用时，产物落盘后
        # "finish" 事件可能永不到来（finished_callback 未触发，async-for 挂死在
        # 99% Save PDF）——CLI 路径无此问题。故不能只等事件：并行跑一个
        # 看门狗，轮询输出目录，*.dual.pdf 出现且大小稳定即判定成功，主动收尾。
        # 收尾用 os._exit：绕开 BabelDOC 残留的非守护线程导致解释器无法退出。
        result: dict | None = None
        error_msg: str | None = None

        async def _consume() -> None:
            nonlocal result, error_msg
            async for event in async_translate(config):
                etype = event.get("type")
                if etype in ("progress_start", "progress_update", "progress_end"):
                    _emit({
                        "type": "progress",
                        "stage": str(event.get("stage", "")),
                        "overall": float(event.get("overall_progress", 0) or 0),
                        # 子集：RSS 随进度上报，主后端超阈值告警
                        "rss_mb": round(_rss_mb(), 1),
                    })
                elif etype == "finish":
                    tr = event.get("translate_result")
                    if tr is not None:
                        dual = getattr(
                            tr, "no_watermark_dual_pdf_path", None
                        ) or getattr(tr, "dual_pdf_path", None)
                        mono = getattr(
                            tr, "no_watermark_mono_pdf_path", None
                        ) or getattr(tr, "mono_pdf_path", None)
                        result = {"dual": str(dual or ""), "mono": str(mono or "")}
                elif etype == "error":
                    error_msg = str(event.get("error", "unknown"))
                if error_msg:
                    return

        consume_task = asyncio.ensure_future(_consume())

        def _find_dual() -> str | None:
            if not os.path.isdir(args.output_dir):
                return None
            for name in os.listdir(args.output_dir):
                if name.endswith(".dual.pdf"):
                    return os.path.join(args.output_dir, name)
            return None

        DUAL_STABLE_SECS = 4.0   # 大小连续两次采样一致且文件 mtime 距今超过该值
        HARD_TIMEOUT_SECS = 4 * 3600
        poll_gap = 3.0
        last_size = -1
        stable_since: float | None = None
        dual_path: str | None = None
        deadline = asyncio.get_event_loop().time() + HARD_TIMEOUT_SECS
        loop = asyncio.get_event_loop()
        while result is None and error_msg is None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(consume_task), timeout=poll_gap
                )
                break  # 事件流正常结束（无 finish/error 也算结束）
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            dual = _find_dual()
            if dual:
                try:
                    size = os.path.getsize(dual)
                    age = time.time() - os.path.getmtime(dual)
                except OSError:
                    size, age = -1, 0
                if size > 0 and size == last_size and age > DUAL_STABLE_SECS / 2:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= DUAL_STABLE_SECS:
                        dual_path = dual
                        break
                else:
                    stable_since = None
                last_size = size
            if loop.time() > deadline:
                error_msg = "导出超时（4 小时）"
                break

        if dual_path and result is None:
            mono = dual_path.replace(".dual.pdf", ".mono.pdf")
            result = {
                "dual": dual_path,
                "mono": mono if os.path.isfile(mono) else "",
            }
        elif result is None:
            # 事件流提前结束但看门狗未达稳定窗口：最后查一次产物兜底
            dual = _find_dual()
            if dual and os.path.isfile(dual):
                mono = dual.replace(".dual.pdf", ".mono.pdf")
                result = {"dual": dual, "mono": mono if os.path.isfile(mono) else ""}

        return result, error_msg

    # ②：限流退避——整次尝试仍因 429/限流失败时，qps 减半重跑，
    # 最多 3 次（重跑前清理半成品产物，防看门狗误命中旧文件）
    qps: float = args.qps
    retries = 0
    while True:
        result, error_msg = await run_attempt(qps)
        if (
            error_msg
            and retries < 3
            and _is_rate_limit_error(error_msg)
        ):
            retries += 1
            qps = max(1.0, qps / 2)
            _emit({
                "type": "progress",
                "stage": f"触发限流，qps 降至 {qps:g} 重试（{retries}/3）",
                "overall": 0,
            })
            _clean_partial_outputs(args.output_dir)
            continue
        break

    if error_msg:
        _emit({"type": "error", "error": error_msg})
        _force_exit()
    if not result or not result.get("dual") or not os.path.isfile(result["dual"]):
        _emit({"type": "error", "error": "dual PDF 产物未找到（事件流与产物看门狗均未命中）"})
        _force_exit()
    _emit({"type": "finish", "dual": result["dual"], "mono": result["mono"]})
    _force_exit()


def _force_exit() -> None:
    """冲刷 stdout 后硬退出：BabelDOC 线程池残留会让正常退出挂死。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _emit({"type": "error", "error": "cancelled"})
    except Exception as e:  # noqa: BLE001 — 兜底：任何异常都以 error 行上报而非裸崩
        _emit({"type": "error", "error": f"{type(e).__name__}: {e}"})
        sys.exit(1)
