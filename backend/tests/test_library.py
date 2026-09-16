"""文献库副本单元测试。"""

import os
import time

from library import library_dir, materialize


def _make_pdf(path, content: bytes = b"%PDF-1.4 test-a"):
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_materialize_copies_and_names_by_hash(tmp_path):
    src = _make_pdf(str(tmp_path / "我的论文 v2.pdf"))
    dest = materialize(src, str(tmp_path / "data"))
    assert os.path.isfile(dest)
    assert dest.startswith(os.path.join(str(tmp_path / "data"), "files"))
    assert len(os.path.basename(dest).split(".")[0]) == 40  # sha1 命名
    with open(dest, "rb") as f:
        assert f.read() == b"%PDF-1.4 test-a"


def test_materialize_dedups_same_content(tmp_path):
    """同一内容不同文件名 → 同一份库内副本（哈希命名去重）。"""
    a = _make_pdf(str(tmp_path / "a.pdf"))
    b = _make_pdf(str(tmp_path / "b(1).pdf"))
    da = materialize(a, str(tmp_path / "data"))
    db = materialize(b, str(tmp_path / "data"))
    assert da == db
    files = os.listdir(library_dir(str(tmp_path / "data")))
    assert len(files) == 1


def test_materialize_preserves_mtime(tmp_path):
    """copy2 保留 mtime：run_pipeline 的 job_id 含 mtime，副本与原文件
    同 job_id——任务复用/缓存命中不受入库影响。"""
    src = _make_pdf(str(tmp_path / "m.pdf"))
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest = materialize(src, str(tmp_path / "data"))
    assert int(os.path.getmtime(dest)) == 1_600_000_000


def test_materialize_reuses_existing_without_recopy(tmp_path):
    """库内已有同内容副本时直接复用，不重写（mtime 不变）。"""
    src = _make_pdf(str(tmp_path / "x.pdf"))
    dest = materialize(src, str(tmp_path / "data"))
    stamp = time.time()
    os.utime(dest, (stamp, stamp))
    again = materialize(src, str(tmp_path / "data"))
    assert again == dest
    assert abs(os.path.getmtime(dest) - stamp) < 1
