"""持久化文档索引 docs_index 的单元测试。"""

import json
import os
import tempfile

from docs_index import get_doc, index_path, load_index, upsert_doc


def _doc(doc_id="abcdef0123456789", **kw):
    base = {
        "doc_id": doc_id,
        "title": "DEMO: Knowledge Agent",
        "file_path": r"D:\papers\dalk.pdf",
        "pdf_hash": "abcdef0123456789" + "0" * 24,
        "page_count": 12,
        "file_mtime": 1757400000,
        "status": "done",
    }
    base.update(kw)
    return base


def test_missing_index_returns_empty():
    assert load_index(tempfile.mkdtemp()) == []


def test_corrupt_index_returns_empty(tmp_path):
    p = tmp_path / "docs_index.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_index(str(tmp_path)) == []


def test_upsert_inserts_and_sets_translated_at(tmp_path):
    upsert_doc(str(tmp_path), _doc())
    docs = load_index(str(tmp_path))
    assert len(docs) == 1
    assert docs[0]["translated_at"]  # 缺省补时间
    assert docs[0]["status"] == "done"


def test_upsert_same_doc_id_replaces_not_duplicates(tmp_path):
    upsert_doc(str(tmp_path), _doc(page_count=10))
    upsert_doc(str(tmp_path), _doc(page_count=12))
    docs = load_index(str(tmp_path))
    assert len(docs) == 1
    assert docs[0]["page_count"] == 12


def test_recent_translation_sorted_first(tmp_path):
    upsert_doc(str(tmp_path), _doc(doc_id="old"))
    upsert_doc(str(tmp_path), _doc(doc_id="new"))
    docs = load_index(str(tmp_path))
    assert [d["doc_id"] for d in docs] == ["new", "old"]


def test_no_doc_id_is_ignored(tmp_path):
    upsert_doc(str(tmp_path), {"title": "x"})
    assert load_index(str(tmp_path)) == []


def test_file_path_made_absolute(tmp_path):
    upsert_doc(str(tmp_path), _doc(file_path="relative/dalk.pdf"))
    assert os.path.isabs(load_index(str(tmp_path))[0]["file_path"])


def test_atomic_write_no_tmp_leftover(tmp_path):
    upsert_doc(str(tmp_path), _doc())
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
    assert leftovers == []
    # 文件是合法 JSON（原子 replace 生效）
    json.loads(open(index_path(str(tmp_path)), encoding="utf-8").read())


def test_get_doc(tmp_path):
    upsert_doc(str(tmp_path), _doc(doc_id="aaa"))
    upsert_doc(str(tmp_path), _doc(doc_id="bbb"))
    assert get_doc(str(tmp_path), "bbb")["doc_id"] == "bbb"
    assert get_doc(str(tmp_path), "ccc") is None
