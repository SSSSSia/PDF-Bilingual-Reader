"""BabelDOC 上传模式：/api/library/import 入库端点 + 完成登记钩子测试。"""

import hashlib
import os

from fastapi.testclient import TestClient

import docs_index
from export.babeldoc_export import _finish_register
from main import app

client = TestClient(app)


def _make_pdf(tmp_path, name="论文.pdf", content: bytes = b"%PDF-1.4 babeldoc-import"):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _make_real_pdf(tmp_path, name="论文.pdf", pages=2):
    """真实多页 PDF（pymupdf 生成）：入库页数读取的断言前提。"""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return str(p)


def _patch_data_dir(monkeypatch, tmp_path):
    # Settings.data_dir 是 config_path 的派生只读 property，patch 其源头
    monkeypatch.setattr(
        __import__("main").settings,
        "config_path",
        str(tmp_path / "data" / "config.json"),
    )


def test_import_materializes_and_registers(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    src = _make_real_pdf(tmp_path, "我的论文.pdf", pages=2)
    r = client.post(
        "/api/library/import", json={"file_path": src, "title": "我的论文"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 库内副本：files/<sha1>.pdf
    assert os.path.isfile(body["path"])
    assert os.path.dirname(body["path"]) == os.path.join(
        str(tmp_path / "data"), "files"
    )
    expected_hash = hashlib.sha1(open(src, "rb").read()).hexdigest()
    assert body["doc_id"] == expected_hash[:16]
    # 索引记录：translating + reader=babeldoc，标题用上传名，页数读源 PDF
    doc = docs_index.get_doc(str(tmp_path / "data"), body["doc_id"])
    assert doc["status"] == "translating"
    assert doc["reader"] == "babeldoc"
    assert doc["title"] == "我的论文"
    assert doc["file_path"] == body["path"]
    assert doc["page_count"] == 2


def test_import_dedups_same_content(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    a = _make_pdf(tmp_path, "a.pdf")
    b = _make_pdf(tmp_path, "b(1).pdf")
    ra = client.post("/api/library/import", json={"file_path": a})
    rb = client.post("/api/library/import", json={"file_path": b})
    assert ra.json()["path"] == rb.json()["path"]
    assert ra.json()["doc_id"] == rb.json()["doc_id"]


def test_import_rejects_missing_file(monkeypatch, tmp_path):
    _patch_data_dir(monkeypatch, tmp_path)
    r = client.post(
        "/api/library/import",
        json={"file_path": str(tmp_path / "不存在.pdf")},
    )
    assert r.status_code == 400


def test_finish_register_flips_babeldoc_doc(monkeypatch, tmp_path):
    data_dir = str(tmp_path / "data")
    pdf_hash = "a" * 40
    docs_index.upsert_doc(
        data_dir,
        {"doc_id": pdf_hash[:16], "pdf_hash": pdf_hash, "status": "translating",
         "reader": "babeldoc", "title": "t", "file_path": "x.pdf"},
    )
    _finish_register({"_data_dir": data_dir, "pdf_hash": pdf_hash, "job_id": "j1"})
    assert docs_index.get_doc(data_dir, pdf_hash[:16])["status"] == "done"
    # 其余字段原样保留
    assert docs_index.get_doc(data_dir, pdf_hash[:16])["reader"] == "babeldoc"


def test_finish_register_ignores_plain_docs(tmp_path):
    data_dir = str(tmp_path / "data")
    pdf_hash = "b" * 40
    # 普通文献（无 reader 标记）：完成登记不得改写状态
    docs_index.upsert_doc(
        data_dir,
        {"doc_id": pdf_hash[:16], "pdf_hash": pdf_hash, "status": "done",
         "title": "t", "file_path": "x.pdf"},
    )
    _finish_register({"_data_dir": data_dir, "pdf_hash": pdf_hash, "job_id": "j2"})
    assert docs_index.get_doc(data_dir, pdf_hash[:16])["status"] == "done"  # 不变
    # 无索引记录：no-op 不抛错
    _finish_register({"_data_dir": data_dir, "pdf_hash": "c" * 40, "job_id": "j3"})
    # 缺 data_dir：no-op
    _finish_register({"pdf_hash": pdf_hash, "job_id": "j4"})
