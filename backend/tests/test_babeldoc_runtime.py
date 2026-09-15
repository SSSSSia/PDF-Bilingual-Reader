"""babeldoc_runtime 单元测试（阶段9-T6 直接捆绑）：检测链各分支。"""

import os
import sys

from export import babeldoc_export, babeldoc_runtime as rt


def test_runtime_python_detection(tmp_path):
    assert rt.runtime_python(str(tmp_path)) is None
    exe = os.path.join(rt.runtime_dir(str(tmp_path)), "python.exe")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "w", encoding="utf-8") as f:
        f.write("x")
    assert rt.runtime_python(str(tmp_path)) == exe


def test_installed_version(tmp_path):
    assert rt.installed_version(str(tmp_path)) == ""
    d = rt.runtime_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "runtime-version.txt"), "w", encoding="utf-8") as f:
        f.write("babeldoc-0.6.4 py3.12\n")
    assert rt.installed_version(str(tmp_path)) == "babeldoc-0.6.4 py3.12"


def test_venv_python_chain_exe_adjacent(tmp_path, monkeypatch):
    """捆绑安装位：项目 venv 不存在时，回落到后端 exe 同级的 babeldoc-runtime/。"""
    monkeypatch.setattr(babeldoc_export, "_venv_python", lambda: None)
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    fake_exe = exe_dir / "pdf-backend.exe"
    fake_exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert babeldoc_export.venv_python(str(data_dir)) is None

    # exe 同级出现 babeldoc-runtime/python.exe → 命中捆绑安装位
    hit = os.path.join(rt.runtime_dir(str(exe_dir)), "python.exe")
    os.makedirs(os.path.dirname(hit), exist_ok=True)
    with open(hit, "w", encoding="utf-8") as f:
        f.write("p")
    assert babeldoc_export.venv_python(str(data_dir)) == hit

    # data_dir 安装位兜底（exe 目录无运行时时）
    import shutil

    shutil.rmtree(rt.runtime_dir(str(exe_dir)))
    hit2 = os.path.join(rt.runtime_dir(str(data_dir)), "python.exe")
    os.makedirs(os.path.dirname(hit2), exist_ok=True)
    with open(hit2, "w", encoding="utf-8") as f:
        f.write("p")
    assert babeldoc_export.venv_python(str(data_dir)) == hit2


def test_start_export_no_local_shadowing_crash(tmp_path, monkeypatch):
    """回归（2026-09-15 T10 首次实测导出发现）：T6 重构把运行时解析结果赋给
    与模块级函数同名的局部变量 venv_python——同名赋值令函数内该名字整体
    局部化，调用即 UnboundLocalError，2026-09-12 起导出启动恒 500。
    断言 start_export 能走过运行时解析段（拿到 job 结构而非裸异常），
    且解析出的 python 进入 worker argv[0]。"""
    import asyncio
    import subprocess as _sp

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    cfg = {"api_url": "https://x", "api_key": "k", "model": "m"}
    monkeypatch.setattr(babeldoc_export, "venv_python", lambda data_dir=None: "PY")
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["argv0"] = cmd[0]
            raise OSError("stub: 不真正启动 worker")

    monkeypatch.setattr(_sp, "Popen", FakePopen)
    r = asyncio.run(
        babeldoc_export.start_export(str(pdf), cfg, str(tmp_path / "cache"))
    )
    # 启动失败被收敛为 error 任务（原 bug 在此之前就裸抛 UnboundLocalError）
    assert r["status"] == "error"
    assert seen.get("argv0") == "PY"
