"""
两层缓存：
  1) OCR 层：文件哈希 + 页号        -> 该页的 blocks 原文
  2) 翻译层：原文哈希 + 目标语言 + 模型 -> 译文

设计要点：
- key 只依赖内容哈希，不依赖文件路径，因此文件改名/移动后缓存依然命中。
- key 不依赖 OCR provider，后续切换 OCR 方案（见总纲 R6）无需清缓存。
- 译文缓存引入 model/lang，换模型或换目标语言不会串味。
"""

import os
import json
import hashlib
import tempfile
from typing import Optional


def _sha1(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def file_hash(path: str) -> str:
    """对文件内容做哈希。大文件逐块读取，避免一次性载入内存。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text_hash(text: str) -> str:
    return _sha1(text)


# 缓存版本号：识别/切块/提示词策略升级时 +1，让旧缓存整体失效。
# v2: RENDER_SCALE 1.8→2.5、max_tokens 8192→16384、切块保护表格公式、提示词重写
# v3: 混合 OCR（文本层优先）、RENDER_SCALE 2.5、段级切块（不再句级切分）
CACHE_VERSION = "v3"


def ocr_key(pdf_hash: str, page: int, model: str) -> str:
    return _sha1("ocr", CACHE_VERSION, pdf_hash, str(page), model)


def translate_key(
    src_hash: str, target_lang: str, model: str, prompt_version: str = ""
) -> str:
    """翻译缓存键。prompt_version 为空时与旧版兼容；传入 openai_compat.PROMPT_VERSION
    后，提示词升级自动使旧翻译缓存失效。"""
    return _sha1("tr", CACHE_VERSION, prompt_version, src_hash, target_lang, model)


def _cache_path(cache_dir: str, key: str) -> str:
    # 按前两位分目录，避免单目录文件过多
    return os.path.join(cache_dir, key[:2], f"{key}.json")


def read_cache(cache_dir: str, key: str) -> Optional[dict]:
    """读取缓存。损坏或不存在均返回 None，绝不抛异常。"""
    path = _cache_path(cache_dir, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_cache(cache_dir: str, key: str, data: dict) -> None:
    """原子写入：先写临时文件再替换，避免进程中断产生半截 JSON。"""
    path = _cache_path(cache_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def clear_cache(cache_dir: str) -> int:
    """清空缓存，返回删除的文件数。"""
    removed = 0
    if not os.path.isdir(cache_dir):
        return 0
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def cache_stats(cache_dir: str) -> dict:
    count = 0
    size = 0
    if os.path.isdir(cache_dir):
        for root, _dirs, files in os.walk(cache_dir):
            for name in files:
                if name.endswith(".json"):
                    try:
                        size += os.path.getsize(os.path.join(root, name))
                        count += 1
                    except OSError:
                        pass
    return {"entries": count, "bytes": size}
