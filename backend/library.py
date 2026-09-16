"""文献库文件副本。

背景：docs_index 若只记录源文件的原始路径，源目录一旦移动/改名/删除，
依赖源文件的功能（原版模式/公式识别/导出）都会失效。文献库软件的标准
做法（Zotero 同款）：上传时把 PDF 复制进应用数据目录，此后一切操作用
库内副本，原文件位置变化零影响。

设计要点：
- `<data_dir>/files/<内容sha1>.pdf`：按内容哈希命名天然去重（同一文件
  重复上传只存一份；哈希名也规避 CJK/超长文件名问题）；
- shutil.copy2 保留 mtime——run_pipeline 的 job_id 含 mtime，副本与
  原文件同 job_id，任务复用/缓存命中不受影响；
- 临时名 + os.replace 原子落盘，并发上传同一内容安全；
- 翻译缓存按 pdf_hash 键控，副本与原文件内容一致 → 全部照常命中。
"""

import os
import shutil

from cache.file_cache import file_hash


def library_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "files")
    os.makedirs(d, exist_ok=True)
    return d


def materialize(file_path: str, data_dir: str) -> str:
    """把源 PDF 复制进文献库（同内容副本已存在则直接复用），返回库内路径。"""
    dest = os.path.join(library_dir(data_dir), f"{file_hash(file_path)}.pdf")
    if not os.path.isfile(dest):
        tmp = f"{dest}.{os.getpid()}.tmp"
        shutil.copy2(file_path, tmp)
        os.replace(tmp, dest)
    return dest
