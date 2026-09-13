import sys, os, io
if getattr(sys, "frozen", False):
    import faulthandler
    faulthandler.enable()
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "pdf-reader", "logs")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, "probe-np.log"), "ab", buffering=0)
    sys.stdout = io.TextIOWrapper(f, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = sys.stdout
    os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
print("before-numpy")
import numpy as np
print("numpy-ok", np.__version__)
import pymupdf
print("pymupdf-ok")
import pymupdf4llm
print("pymupdf4llm-ok, use_layout =", pymupdf4llm._use_layout)
