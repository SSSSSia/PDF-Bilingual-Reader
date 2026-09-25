"""启动端口自愈：占用进程身份判定测试（纯函数，不触进程操作）。"""

from main import _looks_like_our_backend


def test_sidecar_image_name_matches():
    assert _looks_like_our_backend("pdf-backend-od.exe", r"D:\app\pdf-backend-od.exe")
    assert _looks_like_our_backend("pdf_backend.exe", "")


def test_dev_python_backend_matches():
    assert _looks_like_our_backend(
        "python.exe", r"D:\CodeFile\PDF-Reader\.venv\Scripts\python.exe backend/main.py"
    )


def test_unrelated_processes_rejected():
    # 未知程序占用 8000：一律不杀
    assert not _looks_like_our_backend("nginx.exe", "nginx: worker")
    # python 但不是本应用后端（别的脚本/服务）
    assert not _looks_like_our_backend("python.exe", "python -m http.server 8000")
    assert not _looks_like_our_backend("python.exe", "jupyter notebook")
    # 空值防御
    assert not _looks_like_our_backend("", "")
