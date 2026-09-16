"""文件夹管理：docs_index 文件夹层 + API 端点测试。"""

import tempfile

from fastapi.testclient import TestClient

import docs_index
from main import app

client = TestClient(app)


def _doc(doc_id="abcdef0123456789", **kw):
    base = {
        "doc_id": doc_id,
        "title": "TOG",
        "file_path": r"D:\papers\tog.pdf",
        "pdf_hash": "abcdef0123456789" + "0" * 24,
        "page_count": 31,
        "file_mtime": 1757400000,
        "status": "done",
    }
    base.update(kw)
    return base


# ---------------- docs_index 文件夹层 ----------------


def test_missing_folders_returns_empty():
    assert docs_index.load_folders(tempfile.mkdtemp()) == []


def test_corrupt_folders_returns_empty(tmp_path):
    (tmp_path / "folders.json").write_text("{not json", encoding="utf-8")
    assert docs_index.load_folders(str(tmp_path)) == []


def test_add_folder(tmp_path):
    f = docs_index.add_folder(str(tmp_path), "GraphRAG")
    assert f["name"] == "GraphRAG"
    assert f["folder_id"]
    assert [x["name"] for x in docs_index.load_folders(str(tmp_path))] == ["GraphRAG"]


def test_add_folder_rejects_blank(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        docs_index.add_folder(str(tmp_path), "   ")


def test_rename_folder(tmp_path):
    f = docs_index.add_folder(str(tmp_path), "旧名")
    assert docs_index.rename_folder(str(tmp_path), f["folder_id"], "新名")
    assert docs_index.load_folders(str(tmp_path))[0]["name"] == "新名"
    assert not docs_index.rename_folder(str(tmp_path), "nope", "x")


def test_delete_folder_moves_docs_to_unsorted(tmp_path):
    f = docs_index.add_folder(str(tmp_path), "GraphRAG")
    docs_index.upsert_doc(str(tmp_path), _doc(folder_id=f["folder_id"]))
    docs_index.delete_folder(str(tmp_path), f["folder_id"])
    assert docs_index.load_folders(str(tmp_path)) == []
    # 文档不删，归未分类
    assert docs_index.get_doc(str(tmp_path), _doc()["doc_id"])["folder_id"] is None


def test_set_doc_folder(tmp_path):
    docs_index.upsert_doc(str(tmp_path), _doc())
    f = docs_index.add_folder(str(tmp_path), "RL")
    assert docs_index.set_doc_folder(str(tmp_path), _doc()["doc_id"], f["folder_id"])
    assert docs_index.get_doc(str(tmp_path), _doc()["doc_id"])["folder_id"] == f["folder_id"]
    # 移出归未分类
    assert docs_index.set_doc_folder(str(tmp_path), _doc()["doc_id"], None)
    assert docs_index.get_doc(str(tmp_path), _doc()["doc_id"])["folder_id"] is None
    assert not docs_index.set_doc_folder(str(tmp_path), "ghost", None)


def test_upsert_preserves_folder_id(tmp_path):
    """重翻译（done 钩子再次 upsert，不带 folder_id）不丢归类。"""
    docs_index.upsert_doc(str(tmp_path), _doc())
    f = docs_index.add_folder(str(tmp_path), "RL")
    docs_index.set_doc_folder(str(tmp_path), _doc()["doc_id"], f["folder_id"])
    docs_index.upsert_doc(str(tmp_path), _doc(page_count=32))  # 重翻译 upsert
    assert docs_index.get_doc(str(tmp_path), _doc()["doc_id"])["folder_id"] == f["folder_id"]


# ---------------- API 端点 ----------------


def test_api_docs_returns_folders(tmp_path, monkeypatch):
    from main import settings

    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: str(tmp_path)))
    docs_index.add_folder(str(tmp_path), "GraphRAG")
    r = client.get("/api/docs")
    assert r.status_code == 200
    body = r.json()
    assert [f["name"] for f in body["folders"]] == ["GraphRAG"]


def test_api_folder_crud_and_move(tmp_path, monkeypatch):
    from main import settings

    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: str(tmp_path)))
    r = client.post("/api/folders", json={"name": "GraphRAG"})
    assert r.status_code == 200
    fid = r.json()["folder"]["folder_id"]

    docs_index.upsert_doc(str(tmp_path), _doc())
    assert client.post("/api/docs/move", json={"doc_id": _doc()["doc_id"], "folder_id": fid}).json()["ok"]
    assert client.post("/api/docs/move", json={"doc_id": _doc()["doc_id"], "folder_id": None}).json()["ok"]
    assert client.post("/api/docs/move", json={"doc_id": _doc()["doc_id"], "folder_id": "ghost"}).status_code == 404
    assert client.post("/api/docs/move", json={"doc_id": "ghost", "folder_id": None}).status_code == 404

    assert client.post("/api/folders/rename", json={"folder_id": fid, "name": "RAG"}).json()["ok"]
    assert client.post("/api/folders/rename", json={"folder_id": fid, "name": ""}).status_code == 400
    assert client.post("/api/folders/rename", json={"folder_id": "ghost", "name": "x"}).status_code == 404

    assert client.post("/api/folders/delete", json={"folder_id": fid}).json()["ok"]
    assert client.get("/api/docs").json()["folders"] == []
