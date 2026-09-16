"""持久化文档索引。

管线 done 时 upsert 一条记录到数据目录（%APPDATA%/pdf-reader/）下的
docs_index.json；此前管线结果只存内存 _jobs（TTL 淘汰），重启后主页
无从展示。本模块只做索引层，不改动现有 KV 缓存结构（阶段 6 范围约束）。

- doc_id = pdf_hash 前 16 位（与 job_id 前缀、images/ 目录名一致）；
- file_mtime 记录翻译时的源文件 mtime：可重建 job_id（pdf_hash16_mtime），
  重开文档时用于定位该次翻译会话；
- 原子写沿用 write_cache 的 tmp + os.replace 模式，防半截 JSON；
- 读取侧防御性容错：文件缺失/损坏一律返回空列表（索引可随时重建，
  丢索引只是丢主页列表，不影响任何缓存数据）。
"""

import json
import os
import tempfile
import time
import uuid

INDEX_FILENAME = "docs_index.json"
FOLDERS_FILENAME = "folders.json"


def index_path(data_dir: str) -> str:
    return os.path.join(data_dir, INDEX_FILENAME)


def folders_path(data_dir: str) -> str:
    return os.path.join(data_dir, FOLDERS_FILENAME)


def load_index(data_dir: str) -> list:
    """读取全部索引记录（新翻译的在前）。缺失/损坏返回空列表。"""
    path = index_path(data_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def upsert_doc(data_dir: str, doc: dict) -> None:
    """按 doc_id upsert（新记录插队首，最近翻译排前），原子落盘。

    translated_at 缺省补当前本地时间；file_path 统一转绝对路径；
    未显式携带 folder_id 时保留旧记录的归档文件夹（重翻译不丢归类）。
    """
    doc_id = (doc.get("doc_id") or "").strip()
    if not doc_id:
        return
    old = get_doc(data_dir, doc_id)
    merged = {**doc, "doc_id": doc_id}
    if "folder_id" not in merged and old is not None:
        merged["folder_id"] = old.get("folder_id")
    merged["file_path"] = os.path.abspath(merged.get("file_path") or "")
    if not merged.get("translated_at"):
        merged["translated_at"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime()
        )
    docs = [d for d in load_index(data_dir) if d.get("doc_id") != doc_id]
    docs.insert(0, merged)
    _atomic_write(data_dir, docs)


def get_doc(data_dir: str, doc_id: str) -> dict | None:
    for d in load_index(data_dir):
        if d.get("doc_id") == doc_id:
            return d
    return None


def _atomic_write(data_dir: str, data, filename: str = INDEX_FILENAME) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, filename)
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------- 文件夹管理 ----------------
# 独立 folders.json，与 docs_index.json 解耦：丢文件夹文件只丢分组，不丢文档记录。


def load_folders(data_dir: str) -> list:
    """读取全部文件夹（创建时间序）。缺失/损坏返回空列表。"""
    path = folders_path(data_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _clean_folder_name(name) -> str:
    name = str(name or "").strip()
    if not name:
        raise ValueError("文件夹名称不能为空")
    return name[:50]


def add_folder(data_dir: str, name) -> dict:
    """新建文件夹，返回新记录。folder_id 用短 uuid（无安全含义，仅标识）。"""
    folder = {
        "folder_id": uuid.uuid4().hex[:12],
        "name": _clean_folder_name(name),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    folders = load_folders(data_dir)
    folders.append(folder)
    _atomic_write(data_dir, folders, FOLDERS_FILENAME)
    return folder


def rename_folder(data_dir: str, folder_id: str, name) -> bool:
    name = _clean_folder_name(name)
    folders = load_folders(data_dir)
    hit = False
    for f in folders:
        if f.get("folder_id") == folder_id:
            f["name"] = name
            hit = True
            break
    if hit:
        _atomic_write(data_dir, folders, FOLDERS_FILENAME)
    return hit


def delete_folder(data_dir: str, folder_id: str) -> None:
    """删除文件夹；归档其中的文档回到未分类（folder_id 置空），不删文档。"""
    folders = [f for f in load_folders(data_dir) if f.get("folder_id") != folder_id]
    _atomic_write(data_dir, folders, FOLDERS_FILENAME)
    docs = load_index(data_dir)
    changed = False
    for d in docs:
        if d.get("folder_id") == folder_id:
            d["folder_id"] = None
            changed = True
    if changed:
        _atomic_write(data_dir, docs)


def set_doc_folder(data_dir: str, doc_id: str, folder_id) -> bool:
    """移动文档到文件夹（folder_id=None 表示未分类）。doc_id 不存在返回 False。"""
    docs = load_index(data_dir)
    hit = False
    for d in docs:
        if d.get("doc_id") == doc_id:
            d["folder_id"] = folder_id
            hit = True
            break
    if hit:
        _atomic_write(data_dir, docs)
    return hit


def rename_doc(data_dir: str, doc_id: str, title: str) -> bool:
    """改文献显示名。仅改索引标题，
    不动源文件；doc_id 不存在或标题为空返回 False。"""
    title = str(title or "").strip()
    if not title:
        return False
    docs = load_index(data_dir)
    hit = False
    for d in docs:
        if d.get("doc_id") == doc_id:
            d["title"] = title[:100]
            hit = True
            break
    if hit:
        _atomic_write(data_dir, docs)
    return hit
