<#
.SYNOPSIS
    将 FastAPI 后端 (backend/main.py) 打包为 Tauri sidecar 可执行文件。
.DESCRIPTION
    决策 D1：内嵌 Python 后端，使打包后的 exe 开箱即用。
    产物命名 pdf-backend-<target-triple>.exe，放入 src-tauri/sidecar/，
    由 tauri.conf.json 的 bundle.externalBin 引用，main.rs 启动时 spawn。
.NOTES
    开发态（npm run tauri dev）不会拉起该 sidecar，请用 scripts/dev-start.ps1
    单独启动后端，避免两端同时占用 8000 端口。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/build-backend.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "",       # 指定 Python 解释器；默认探测 .venv 再回落到 python
    [string]$TargetTriple = ""  # 目标三元组；默认从 rustc -vV 推断
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$sidecarDir = Join-Path $root "src-tauri\sidecar"
$backendEntry = Join-Path $root "backend\main.py"
$buildDir = Join-Path $root "build"

if (-not (Test-Path $backendEntry)) { throw "未找到后端入口: $backendEntry" }

# 1) 定位 Python 解释器
if ([string]::IsNullOrWhiteSpace($Python)) {
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) { $Python = $venvPython }
    else { $Python = "python" }
}
Write-Host "[1/5] Python: $Python"
& $Python -c "import sys; print('       ' + sys.version.replace('\n',' '))"
if ($LASTEXITCODE -ne 0) { throw "找不到可用的 Python 解释器，请用 -Python 指定。" }

# 2) 确保 PyInstaller 可用
# 注意：管理员终端下 PyInstaller 6 会向 stderr 打 DEPRECATION 警告，而
# PS5.1 的 EAP=Stop 会把「带重定向的 native stderr」误判为致命错误——
# native 调用期间临时降级 EAP，仅用退出码判断成败（2026-09-11 新机实测）
Write-Host "[2/5] 检查 PyInstaller ..."
$ErrorActionPreference = "Continue"
& $Python -m PyInstaller --version 2>$null
$pyiOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $pyiOk) {
    Write-Host "      未检测到，正在安装 PyInstaller ..."
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败，请手动安装后重试。" }
}

# 3) 推断目标三元组（Tauri 按 <name>-<triple>.exe 解析 externalBin）
if ([string]::IsNullOrWhiteSpace($TargetTriple)) {
    if (Get-Command rustc -ErrorAction SilentlyContinue) {
        $line = (& rustc -vV | Select-String -Pattern '^host:')
        if ($line) { $TargetTriple = ($line -split '\s+')[1] }
    }
    if ([string]::IsNullOrWhiteSpace($TargetTriple)) { $TargetTriple = "x86_64-pc-windows-msvc" }
}
Write-Host "[3/5] 目标三元组: $TargetTriple"

$outName = "pdf-backend-$TargetTriple"
$outExe = Join-Path $sidecarDir "$outName.exe"

# 4) 清理旧产物
if (Test-Path $outExe) {
    Write-Host "      清理旧产物: $outExe"
    Remove-Item $outExe -Force
}

# 5) 打包（uvicorn 需显式收集，否则运行期会缺模块；stderr 噪声处理同步骤 2）
# pymupdf≥新版 layout 子模块的 onnx 资源（51MB）--collect-all 收不齐，
# 缺失时 sidecar 启动即崩且 GUI 态无日志（2026-09-13 用户实测白屏门禁卡死
# 根因）——显式 add-data；pymupdf4llm 同理整体收集
$sitePkgs = & $Python -c "import sys, pathlib; print(next(p for p in sys.path if p.endswith('site-packages')))"
Write-Host "      site-packages: $sitePkgs"
$layoutRes = Join-Path $sitePkgs "pymupdf\layout\resources"
if (-not (Test-Path $layoutRes)) { throw "未找到 pymupdf layout 资源目录: $layoutRes" }
Write-Host "[4/5] 正在打包后端，请稍候（约 1-3 分钟）..."
$ErrorActionPreference = "Continue"
& $Python -m PyInstaller `
    --noconfirm --clean --onefile `
    --name $outName `
    --distpath $sidecarDir `
    --workpath (Join-Path $buildDir "pyinstaller") `
    --specpath $buildDir `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    --collect-all uvicorn `
    --collect-all PyMuPDF `
    --collect-all pymupdf4llm `
    --add-data "$layoutRes;pymupdf/layout/resources" `
    $backendEntry
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pyiExit -ne 0) { throw "后端打包失败，请检查上方 PyInstaller 输出。" }

# 6) 校验产物
Write-Host "[5/5] 校验产物 ..."
if (-not (Test-Path $outExe)) { throw "未找到产物: $outExe" }
$sizeMB = [math]::Round((Get-Item $outExe).Length / 1MB, 2)
Write-Host "完成: $outExe ($sizeMB MB)" -ForegroundColor Green
Write-Host "提示: 之后执行 npm run tauri build 即可产出自包含的 exe。"
