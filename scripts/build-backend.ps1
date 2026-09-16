<#
.SYNOPSIS
    将 FastAPI 后端 (backend/main.py) 打包为 Tauri sidecar 可执行文件。
.DESCRIPTION
    决策 D1：内嵌 Python 后端，使打包后的 exe 开箱即用。
    产物为 onedir 目录 src-tauri/sidecar/pdf-backend-od/（pdf-backend.exe
    + _internal 依赖），由 build-exe.ps1 经 tauri resources 随包分发，
    main.rs 启动时按资源目录定位 spawn（v0.14.3 起，onefile 启动慢已废）。
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

# 3) 推断目标三元组（仅作构建信息展示，产物命名已与三元组无关）
if ([string]::IsNullOrWhiteSpace($TargetTriple)) {
    if (Get-Command rustc -ErrorAction SilentlyContinue) {
        $line = (& rustc -vV | Select-String -Pattern '^host:')
        if ($line) { $TargetTriple = ($line -split '\s+')[1] }
    }
    if ([string]::IsNullOrWhiteSpace($TargetTriple)) { $TargetTriple = "x86_64-pc-windows-msvc" }
}
Write-Host "[3/5] 目标三元组: $TargetTriple"

# onedir 产物目录（v0.14.3）：onefile 每次启动解压 66MB + Defender 全量
# 重扫，启动耗时数秒~半分钟且随机（用户装机实测）；onedir 直跑安装目录内
# 文件，仅升级后首启扫描一次。exe 统一命名 pdf-backend.exe（main.rs 资源
# 目录定位 + 启动前 taskkill /IM 清残留都按这个名字）。
$outDir = Join-Path $sidecarDir "pdf-backend-od"
$outExe = Join-Path $outDir "pdf-backend.exe"

# 4) 清理旧产物
if (Test-Path $outDir) {
    Write-Host "      清理旧产物: $outDir"
    Remove-Item $outDir -Recurse -Force
}

# 5) 打包（uvicorn 需显式收集，否则运行期会缺模块；stderr 噪声处理同步骤 2）
# pymupdf.layout（1.28 新 Layout 引擎，51MB onnx 资源）显式排除：多进程加载
# 模型在 onefile 冻结环境段错误（2026-09-13 打包版白屏根因），且 textlayer
# 已显式 use_layout(False)——排除后 pymupdf4llm 导入时 ImportError 自动落
# classic 管线，与 dev 行为一致
$ErrorActionPreference = "Continue"
Write-Host "[4/5] 正在打包后端，请稍候（约 1-3 分钟）..."
& $Python -m PyInstaller `
    --noconfirm --clean --onedir `
    --name pdf-backend-od `
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
    --exclude-module pymupdf.layout `
    $backendEntry
$pyiExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pyiExit -ne 0) { throw "后端打包失败，请检查上方 PyInstaller 输出。" }

# 6) 统一产物名：PyInstaller onedir 产出 pdf-backend-od/pdf-backend-od.exe
#    （+ _internal 依赖目录），统一改名为 pdf-backend.exe
Write-Host "[5/5] 校验产物 ..."
$builtExe = Join-Path $outDir "pdf-backend-od.exe"
if (-not (Test-Path $builtExe)) { throw "未找到产物: $builtExe" }
Rename-Item -Path $builtExe -NewName "pdf-backend.exe"
if (-not (Test-Path $outExe)) { throw "未找到产物: $outExe" }
$sizeMB = [math]::Round(((Get-ChildItem $outDir -Recurse | Measure-Object Length -Sum).Sum) / 1MB, 2)
Write-Host "完成: $outExe（onedir 全目录 $sizeMB MB）" -ForegroundColor Green
Write-Host "提示: 之后执行 npm run tauri build 即可产出自包含的 exe。"
