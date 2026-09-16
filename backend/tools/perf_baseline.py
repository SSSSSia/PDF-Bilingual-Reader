"""性能基线压测（可复现，决策门工具）。

前置：后端已在 127.0.0.1:8000 运行（.venv/Scripts/python.exe backend/main.py），
且 --pdf 指向一篇真实 PDF（建议用已翻译过的文档——缓存命中让 LLM 成本趋近于零，
同时完整走提取/轮询/状态链路）。

用法：
  .venv/Scripts/python.exe backend/tools/perf_baseline.py --pdf "<PDF绝对路径>" \
      [--window 45] [--skip-translation] [--skip-babeldoc]

场景：
  S1 idle       无负载 /api/health 延迟分布
  S2 translate  翻译流水线负载下 /api/health 与 /api/pipeline/status 的延迟
  S3 babeldoc   BabelDOC worker 负载下 /api/health 延迟 + worker RSS（测完自动取消）
  S4 combined   翻译 + BabelDOC 并发下 /api/health 延迟（对齐 验收线 P95 < 200ms）
全程后台每 3s 采样 python 进程 RSS，按命令行区分主后端（main.py）与
babeldoc worker（babeldoc_worker.py）。

产出：摘要表（stdout） + 完整 JSON（logs/perf-baseline-<时间戳>.json）。
判定参考：并发重负载下 /api/health P95 < 200ms。
"""

import argparse
import asyncio
import json
import statistics
import time

import httpx

BASE = "http://127.0.0.1:8000"
PROBE_INTERVAL = 0.25
RSS_INTERVAL = 3.0


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[idx]


def summarize(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "p50_ms": round(statistics.median(samples), 1),
        "p95_ms": round(pct(samples, 95), 1),
        "max_ms": round(max(samples), 1),
    }


async def probe(client: httpx.AsyncClient, url: str, samples: list[float]) -> None:
    t0 = time.perf_counter()
    try:
        await client.get(url)
        samples.append((time.perf_counter() - t0) * 1000)
    except Exception:
        samples.append(float("nan"))


async def rss_sampler(rss_log: dict) -> None:
    """后台采样 python 进程 RSS（字节），按角色分类记录最大值。
    依赖 PowerShell CIM 查询命令行，区分主后端与 babeldoc worker。"""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                " | Select-Object ProcessId,CommandLine,WorkingSetSize"
                " | ConvertTo-Json -Compress",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            data = json.loads(out.decode("utf-8", "replace") or "[]")
            items = data if isinstance(data, list) else [data]
            for it in items:
                cmd = str(it.get("CommandLine", ""))
                role = None
                if "babeldoc_worker" in cmd:
                    role = "worker"
                elif "main.py" in cmd:
                    role = "backend"
                if role:
                    rss = int(it.get("WorkingSetSize", 0) or 0)
                    rss_log[role] = max(rss_log.get(role, 0), rss)
        except Exception:
            pass
        await asyncio.sleep(RSS_INTERVAL)


async def run_window(
    client: httpx.AsyncClient,
    seconds: int,
    targets: list[str],
) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {url: [] for url in targets}
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        for url in targets:
            await probe(client, url, samples[url])
        await asyncio.sleep(PROBE_INTERVAL)
    return samples


async def start_pipeline(client: httpx.AsyncClient, pdf: str) -> str | None:
    r = await client.post(f"{BASE}/api/pipeline/run", json={"file_path": pdf})
    return r.json().get("job_id")


async def start_babeldoc(client: httpx.AsyncClient, pdf: str) -> str | None:
    r = await client.post(f"{BASE}/api/export/babeldoc", json={"file_path": pdf})
    return r.json().get("job_id")


async def cancel_babeldoc(client: httpx.AsyncClient, job_id: str | None) -> None:
    if job_id:
        await client.delete(f"{BASE}/api/export/babeldoc/{job_id}")
        print("[BabelDOC 任务已取消]")


def report(title: str, samples: dict[str, list[float]], rss: dict) -> dict:
    out: dict = {"scenario": title, "rss_peak_mb": {
        k: round(v / 1_048_576) for k, v in rss.items()
    }}
    print(f"\n── {title} ──")
    for url, s in samples.items():
        out[url] = summarize([x for x in s if x == x])  # 滤掉 NaN（连接失败样本）
        print(f"  {url:45s} n={out[url].get('n', 0):4d}  "
              f"P50={out[url].get('p50_ms', float('nan')):7.1f}ms  "
              f"P95={out[url].get('p95_ms', float('nan')):7.1f}ms  "
              f"max={out[url].get('max_ms', float('nan')):8.1f}ms")
    print(f"  RSS 峰值(MB): {out['rss_peak_mb']}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--window", type=int, default=45, help="每场景测量秒数")
    ap.add_argument("--skip-translation", action="store_true")
    ap.add_argument("--skip-babeldoc", action="store_true")
    args = ap.parse_args()

    reports: list[dict] = []
    rss_log: dict = {}
    sampler = asyncio.ensure_future(rss_sampler(rss_log))

    async with httpx.AsyncClient(timeout=8) as client:
        # S1 idle
        s = await run_window(client, max(15, args.window // 2), [f"{BASE}/api/health"])
        reports.append(report("S1 idle（无负载）", s, dict(rss_log)))

        # S2 翻译负载
        if not args.skip_translation:
            job = await start_pipeline(client, args.pdf)
            print(f"\n[翻译任务] job_id={job}")
            s = await run_window(
                client, args.window,
                [f"{BASE}/api/health", f"{BASE}/api/pipeline/status/{job}"],
            )
            reports.append(report("S2 翻译流水线负载", s, dict(rss_log)))

        # S3 BabelDOC 负载（测完取消，避免持续占用 worker/额度）
        if not args.skip_babeldoc:
            bjob = await start_babeldoc(client, args.pdf)
            print(f"\n[BabelDOC 任务] job_id={bjob}")
            s = await run_window(
                client, args.window, [f"{BASE}/api/health"],
            )
            reports.append(report("S3 BabelDOC worker 负载", s, dict(rss_log)))
            await cancel_babeldoc(client, bjob)

        # S4 双负载（重新起翻译——缓存命中最快进入流式阶段）
        if not args.skip_babeldoc:
            job = None
            if not args.skip_translation:
                job = await start_pipeline(client, args.pdf)
            bjob = await start_babeldoc(client, args.pdf)
            targets = [f"{BASE}/api/health"]
            if job:
                targets.append(f"{BASE}/api/pipeline/status/{job}")
            s = await run_window(client, args.window, targets)
            reports.append(report("S4 翻译+BabelDOC 双负载", s, dict(rss_log)))
            await cancel_babeldoc(client, bjob)

        sampler.cancel()

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = f"logs/perf-baseline-{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": ts, "pdf": args.pdf, "reports": reports},
                  f, ensure_ascii=False, indent=2)
    print(f"\n完整报告已写入 {out_path}")
    health_p95 = [r["http://127.0.0.1:8000/api/health"].get("p95_ms")
                  for r in reports if "http://127.0.0.1:8000/api/health" in r]
    print(f"各场景 health P95（ms）: {health_p95} —— 验收线: 并发重负载 P95 < 200")


if __name__ == "__main__":
    asyncio.run(main())
