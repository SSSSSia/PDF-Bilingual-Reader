"""版面检测信号源接入层单测。

不真起 babeldoc 运行时——LayoutProvider._spawn 按模块惯例注入 FakeProc
（stdout NDJSON 可编程），覆盖：缓存命中短路、正常流式收尾、worker 错误、
提前退出、静默超时、运行时缺失、未请求页 None。
worker 脚本（layout_worker.py）的真实推理由 backend/tools/layout_model_quality.py
在随包运行时上回归，不进 pytest。
"""
import asyncio
import json

from cache.file_cache import ocr_key, read_cache, write_cache
from ocr import layout_model
from ocr.layout_model import LAYOUT_CACHE_MODEL, LayoutProvider


class _FakeStream:
    def __init__(self, lines: list[bytes] | None = None, hang: bool = False):
        self._lines = list(lines or [])
        self._hang = hang

    async def readline(self):
        if self._hang:
            await asyncio.sleep(30)
            return b""
        if self._lines:
            return self._lines.pop(0)
        return b""  # EOF


class _FakeProc:
    def __init__(self, stdout: _FakeStream, stderr: _FakeStream | None = None):
        self.stdout = stdout
        self.stderr = stderr or _FakeStream()
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _ndline(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _make_provider(tmp_path, monkeypatch, proc=None):
    """标准构造 + _spawn 替换（proc=None 表示禁止起进程）。"""
    p = LayoutProvider(
        "fake.pdf", "hash-x", str(tmp_path), python_exe="irrelevant"
    )
    calls = []

    async def fake_spawn(py, pages):
        calls.append((py, pages))
        if proc is None:
            raise RuntimeError("不应起 worker")
        return proc

    monkeypatch.setattr(p, "_spawn", fake_spawn)
    return p, calls


def _drain(p: LayoutProvider, pages: list[int]):
    """start → 等全部 future → 等 reader 收尾 → close（同一事件循环内）。"""

    async def run():
        await p.start(pages)
        for fut in p._futures.values():
            await fut
        for _ in range(200):
            if p._reader is None or p._reader.done():
                break
            await asyncio.sleep(0.01)
        await p.close()

    asyncio.run(run())


# ── 缓存命中：不起 worker ─────────────────────────────────────────

def test_start_cache_hit_skips_worker(tmp_path, monkeypatch):
    write_cache(
        tmp_path,
        ocr_key("hash-x", 0, LAYOUT_CACHE_MODEL),
        {"regions": [{"label": "title", "conf": 0.9, "bbox": [1, 2, 3, 4]}]},
    )
    p, calls = _make_provider(tmp_path, monkeypatch, proc=None)

    async def run():
        await p.start([0])  # 全部命中 → 不起 worker
        return await p.regions(0)

    regions0 = asyncio.run(run())
    assert regions0 == [{"label": "title", "conf": 0.9, "bbox": [1, 2, 3, 4]}]
    assert calls == []
    assert p.status == "done"
    assert p.stats["cached"] == 1


# ── 正常流式：逐页 resolve + 落缓存 + done ─────────────────────────

def test_worker_streaming_writes_cache_and_resolves(tmp_path, monkeypatch):
    proc = _FakeProc(_FakeStream([
        _ndline({"page": 0, "regions": [{"label": "abandon", "conf": 0.8, "bbox": [0, 0, 100, 20]}]}),
        _ndline({"page": 2, "regions": []}),
        _ndline({"done": True}),
    ]))
    p, calls = _make_provider(tmp_path, monkeypatch, proc=proc)

    async def run():
        await p.start([0, 2])
        r0, r2 = await p.regions(0), await p.regions(2)
        for fut in p._futures.values():
            await fut
        for _ in range(200):
            if p._reader is None or p._reader.done():
                break
            await asyncio.sleep(0.01)
        return r0, r2

    r0, r2 = asyncio.run(run())
    asyncio.run(p.close())
    assert r0 and r0[0]["label"] == "abandon"
    assert r2 == []  # 扫描页空 regions（合法产出，区别于 None 失败）
    assert p.status == "done"
    assert calls and calls[0][1] == [0, 2]  # 缺页集合按请求顺序传入
    # 区域已按页落缓存（doclayout-v1 键）
    hit = read_cache(tmp_path, ocr_key("hash-x", 0, LAYOUT_CACHE_MODEL))
    assert hit and hit["regions"][0]["label"] == "abandon"
    assert p.stats["model"] == 2


# ── 失败路径：错误行 / 提前退出 / 静默超时 ─────────────────────────

def test_worker_error_line_fails_all(tmp_path, monkeypatch):
    proc = _FakeProc(_FakeStream([
        _ndline({"page": 0, "regions": [{"label": "title", "conf": 0.9, "bbox": [1, 1, 2, 2]}]}),
        _ndline({"error": "依赖导入失败"}),
    ]))
    p, _ = _make_provider(tmp_path, monkeypatch, proc=proc)

    async def run():
        await p.start([0, 1])
        r0, r1 = await p.regions(0), await p.regions(1)
        for fut in p._futures.values():
            await fut
        for _ in range(200):
            if p._reader is None or p._reader.done():
                break
            await asyncio.sleep(0.01)
        return r0, r1

    r0, r1 = asyncio.run(run())
    asyncio.run(p.close())
    assert r0 and r0[0]["label"] == "title"
    assert r1 is None  # 未产出页落 None → 调用方走兜底
    assert p.status == "failed"
    assert "依赖导入失败" in p.fail_reason


def test_worker_early_exit_fails_remaining(tmp_path, monkeypatch):
    proc = _FakeProc(_FakeStream([
        _ndline({"page": 0, "regions": []}),
    ]))  # EOF 无 done
    p, _ = _make_provider(tmp_path, monkeypatch, proc=proc)
    _drain(p, [0, 3])
    assert p.status == "failed"
    assert "提前退出" in p.fail_reason


def test_worker_silence_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(layout_model, "LINE_SILENCE_TIMEOUT", 0.05)
    proc = _FakeProc(_FakeStream(hang=True))
    p, _ = _make_provider(tmp_path, monkeypatch, proc=proc)
    _drain(p, [0])
    assert p.status == "failed"
    assert "静默超时" in p.fail_reason
    assert proc.killed


# ── 运行时缺失：unavailable，不抛异常 ─────────────────────────────

def test_runtime_missing_marks_unavailable(tmp_path, monkeypatch):
    p = LayoutProvider("fake.pdf", "hash-x", str(tmp_path), python_exe=None)
    monkeypatch.setattr(
        "export.babeldoc_export.venv_python", lambda data_dir=None: None
    )
    _drain(p, [0])
    assert p.status == "unavailable"
    r = asyncio.run(p.regions(0))
    assert r is None


# ── 未请求页 ─────────────────────────────────────────────────────

def test_regions_unrequested_page_is_none(tmp_path, monkeypatch):
    p, _ = _make_provider(tmp_path, monkeypatch, proc=None)

    async def run():
        await p.start([])  # 空请求 → done，不起 worker
        return await p.regions(7)

    r = asyncio.run(run())
    assert p.status == "done"
    assert r is None


# ── 区域去重（纯几何）───────────────────────────────────────

from ocr.layout_model import dedupe_regions  # noqa: E402


def test_dedupe_conf_floor_boundary():
    regions = [
        {"label": "table", "conf": 0.499, "bbox": [10, 10, 200, 100]},
        {"label": "table", "conf": 0.50, "bbox": [20, 20, 210, 110]},
    ]
    out = dedupe_regions(regions)
    assert len(out) == 1 and out[0]["conf"] == 0.50  # 下限含边界；0.499 滤除


def test_dedupe_fg_rag_p3_measured_case():
    """实测回归：第三张无框表 0.56 与复检框
    0.38 同 bbox——0.56 保留（验收标准 1），0.38 双重死亡（下限+IoU）。"""
    regions = [
        {"label": "table", "conf": 0.92, "bbox": [318, 317, 557, 375]},
        {"label": "table", "conf": 0.76, "bbox": [318, 238, 557, 295]},
        {"label": "table", "conf": 0.56, "bbox": [318, 83, 558, 216]},
        {"label": "table", "conf": 0.38, "bbox": [318, 83, 557, 216]},  # 复检框
        {"label": "table", "conf": 0.30, "bbox": [60, 500, 300, 560]},  # Algorithm 框
    ]
    out = dedupe_regions(regions)
    assert [r["conf"] for r in out] == [0.56, 0.76, 0.92]


def test_dedupe_same_label_iou_keeps_highest_conf():
    regions = [
        {"label": "title", "conf": 0.84, "bbox": [40, 40, 500, 90]},
        {"label": "title", "conf": 0.75, "bbox": [42, 42, 498, 88]},  # IoU≈0.95
        {"label": "title", "conf": 0.9, "bbox": [40, 200, 500, 250]},  # 不重叠
    ]
    out = dedupe_regions(regions)
    assert len(out) == 2
    confs = {r["conf"] for r in out}
    assert confs == {0.84, 0.9}  # 高置信复检框被吞，离散框保留


def test_dedupe_cross_label_overlap_kept():
    # title 与 plain text 同区域重叠：跨 label 不判重（消费端分通道）
    regions = [
        {"label": "title", "conf": 0.9, "bbox": [40, 40, 500, 90]},
        {"label": "plain text", "conf": 0.8, "bbox": [40, 40, 500, 90]},
    ]
    assert len(dedupe_regions(regions)) == 2


def test_dedupe_output_sorted_by_yx_and_robust_to_malformed():
    regions = [
        {"label": "table", "conf": 0.9, "bbox": [10, 300, 200, 400]},
        {"label": "abandon", "conf": 0.8, "bbox": [0, 700, 600, 780]},
        {"label": "title", "conf": 0.9, "bbox": [40, 40, 500, 90]},
        {"label": "title", "conf": 0.9, "bbox": [1, 2]},      # 畸形
        {"label": "abandon", "conf": 0.9},                     # 无 bbox
        "not-a-dict",                                          # 非法类型
    ]
    out = dedupe_regions(regions)
    ys = [r["bbox"][1] for r in out]
    assert ys == sorted(ys)
    assert len(out) == 3


def test_provider_regions_applies_dedupe(tmp_path, monkeypatch):
    """读取口去重：worker 原始产出（含复检框/低置信框）→ regions 已净化。"""
    proc = _FakeProc(_FakeStream([
        _ndline({"page": 0, "regions": [
            {"label": "table", "conf": 0.92, "bbox": [55, 292, 365, 395]},
            {"label": "table", "conf": 0.45, "bbox": [56, 293, 364, 394]},  # 低置信复检
            {"label": "abandon", "conf": 0.85, "bbox": [40, 695, 575, 775]},
        ]}),
        _ndline({"done": True}),
    ]))
    p, _ = _make_provider(tmp_path, monkeypatch, proc=proc)

    async def run():
        await p.start([0])
        r = await p.regions(0)
        for fut in p._futures.values():
            await fut
        for _ in range(200):
            if p._reader is None or p._reader.done():
                break
            await asyncio.sleep(0.01)
        return r

    r = asyncio.run(run())
    asyncio.run(p.close())
    assert r is not None and len(r) == 2  # 0.45 复检框已滤
    assert {x["label"] for x in r} == {"table", "abandon"}
