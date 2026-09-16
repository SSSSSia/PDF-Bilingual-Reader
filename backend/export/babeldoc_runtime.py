"""BabelDOC 运行时定位。

方案沿革：曾决策「可选组件 + 应用内下载」（下载链路见 git 历史
babeldoc_runtime 早期版本），用户复核后改为**直接捆绑**——目标
用户多为非计算机专业、不在意体积，随包分发最可控（安装包 ~72MB→约 400MB，
安装后 +660MB）。运行时由 scripts/build-babeldoc-runtime.ps1 暂存为
dist/babeldoc-runtime/（python-3.12 embeddable 自包含 + .venv-babeldoc
site-packages，babeldoc 0.6.4 锁版本），经 tauri resources 随安装包落位到
后端 exe 同级目录，开箱即用、无任何下载步骤。

检测链见 export.babeldoc_export.venv_python：
  1. 开发态项目 venv .venv-babeldoc；
  2. 随包运行时：后端 exe 同级 babeldoc-runtime/（tauri resources 安装位）；
  3. 数据目录 <data_dir>/babeldoc-runtime/（保留扩展位，当前不使用）。

安全边界：本模块不接触任何 api_key；不做任何网络行为。
"""

import os
import sys

RUNTIME_DIR_NAME = "babeldoc-runtime"


def runtime_dir(parent: str) -> str:
    """parent（exe 目录 / 数据目录）下的运行时目录路径。"""
    return os.path.join(parent, RUNTIME_DIR_NAME)


def runtime_python(parent: str) -> str | None:
    """parent 下的运行时 python 路径；不存在返回 None。"""
    if sys.platform == "win32":
        p = os.path.join(runtime_dir(parent), "python.exe")
    else:
        p = os.path.join(runtime_dir(parent), "bin", "python")
    return p if os.path.isfile(p) else None


def installed_version(parent: str) -> str:
    """运行时版本描述（runtime-version.txt 首行，随包构建时写入）；缺失返回空串。"""
    p = os.path.join(runtime_dir(parent), "runtime-version.txt")
    try:
        with open(p, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""
