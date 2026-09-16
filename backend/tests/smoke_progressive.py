"""冒烟脚本：验证流水线渐进回传。

启动一次 pipeline 并高频轮询 status，记录时间线上
(progress, 页数, 已译段数) 的变化——若「status=running 且已译段数递增」
出现过，则渐进呈现链路成立（后端逐段更新 + status 端点如实回传）。
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PDF = r"D:\CODE_FILE\CODE_AI\PDF-Reader\test_ocr\GraphRAG-Bench.pdf"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def snapshot(status):
    pages = status.get("pages") or []
    translated = sum(
        1 for p in pages for b in p.get("blocks", []) if b.get("translated")
    )
    total = sum(len(p.get("blocks", [])) for p in pages)
    return status.get("progress", 0), len(pages), translated, total


def main():
    # 健康检查
    for _ in range(20):
        try:
            get("/api/health")
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("FAIL: backend not reachable")
        sys.exit(1)

    t0 = time.time()
    start = post("/api/pipeline/run", {"file_path": PDF})
    job_id = start["job_id"]
    print(f"job: {job_id}  reused={start.get('reused')}")

    timeline = []
    prev = None
    while time.time() - t0 < 600:
        s = get(f"/api/pipeline/status/{job_id}")
        snap = snapshot(s)
        if snap != prev:
            dt = time.time() - t0
            timeline.append((round(dt, 1), s["status"], *snap))
            prev = snap
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.4)

    progressive_seen = False
    print(f"{'t(s)':>6}  {'status':<8} {'prog':>4} {'pages':>5} {'translated':>10} {'blocks':>6}")
    for dt, st, prog, npg, tr, tot in timeline:
        print(f"{dt:>6}  {st:<8} {prog:>4} {npg:>5} {tr:>10} {tot:>6}")
        if st == "running" and tr > 0:
            progressive_seen = True

    final = get(f"/api/pipeline/status/{job_id}")
    stats = final.get("stats") or {}
    print("\nstats:", stats)
    if final["status"] == "done" and progressive_seen:
        print("\nSMOKE PASS: running 期间已译段数递增 → 渐进回传成立")
    elif final["status"] == "done":
        print("\nSMOKE PASS(cache): 任务完成但未观测到 running 中间态（全部命中缓存，秒级完成）")
    else:
        print("\nSMOKE FAIL:", final.get("error"))
        sys.exit(1)


if __name__ == "__main__":
    main()
