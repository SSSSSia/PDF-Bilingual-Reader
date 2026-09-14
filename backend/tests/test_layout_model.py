"""阶段12-T9.1：版面检测信号源接入层单测。

不真起 babeldoc 运行时——LayoutProvider._spawn 按模块惯例注入 FakeProc
（stdout NDJSON 可编程），覆盖：缓存命中短路、正常流式收尾、worker 错误、
提前退出、静默超时、运行时缺失、未请求页 None。
worker 脚本（layout_worker.py）的真实推理由 backend/tools/layout_model_quality.py
在随包运行时上回归（T9.5），不进 pytest。
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
