# 一键构建自包含 exe：
#   1) 阶段9-T6：暂存 BabelDOC 运行时（embeddable python + site-packages）到
#      dist/babeldoc-runtime/，经 tauri --resources 注入随安装包捆绑（开箱即用）
#   2) 先用 PyInstaller 把 Python 后端打包为 Tauri sidecar（src-tauri/sidecar/）
#   3) 再 tauri build 把 sidecar + 运行时一并打进桌面应用
#
# 运行时注入说明：tauri.conf.json 的 resources 恒为 []（保证无运行时目录时
# 直接 `tauri build`/`tauri dev` 也能过——2026-09-12 实测静态声明会在目录
# 缺失时炸 build script）；本脚本在暂存成功后经 `--resources` CLI 参数动态
# 注入，正式包始终携带运行时。
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1
# 跳过后端打包（仅当 sidecar 已存在且未改动后端时）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -SkipBackend
# 跳过运行时暂存（仅调试用——正式发布必须包含运行时，否则对照功能不可用）：
#   powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1 -SkipRuntime

param(
    [switch]$SkipBackend,
    [switch]$SkipRuntime
)

$ErrorActionPreference = "Stop"

$steps = 3
$configArg = @()
if (-not $SkipRuntime) {
    Write-Host "==> [1/$steps] 暂存 BabelDOC 运行时（tauri config 注入用）..." -ForegroundColor Cyan
    powershell -ExecutionPolicy Bypass -File scripts/build-babeldoc-runtime.ps1 -SkipZip
    # 暂存目录在 build/（vite frontendDist 之外，见 build-babeldoc-runtime.ps1 说明）
    $runtimeDir = Join-Path (Get-Location) "build/babeldoc-runtime"
    if (-not (Test-Path (Join-Path $runtimeDir "python.exe"))) {
        throw "运行时暂存后仍缺 python.exe：$runtimeDir"
    }
    # tauri 2 CLI 无 --resources 参数：经 -c 配置合并注入（相对 src-tauri 解析）
    $configArg = @("-c", '{\"bundle\":{\"resources\":{\"../build/babeldoc-runtime/\":\"babeldoc-runtime/\"}}}')
}
else {
    Write-Host "==> [1/$steps] 跳过运行时暂存（-SkipRuntime，产物将不含 BabelDOC！）" -ForegroundColor Yellow
}

if (-not $SkipBackend) {
    Write-Host "==> [2/$steps] 构建内嵌后端 sidecar ..." -ForegroundColor Cyan
    powershell -ExecutionPolicy Bypass -File scripts/build-backend.ps1
}
else {
    # 防呆（2026-09-13 实测踩坑）：-SkipBackend 跳过了含新端点的 sidecar，
    # 装机后旧后端 404。后端源码比 sidecar 产物新时拒绝跳过。
    $sidecar = Get-ChildItem (Join-Path $root "src-tauri\sidecar") -Filter "pdf-backend-*.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $backendNewest = Get-ChildItem (Join-Path $root "backend") -Recurse -Include "*.py" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($sidecar -and $backendNewest -and $backendNewest.LastWriteTime -gt $sidecar.LastWriteTime) {
        throw "-SkipBackend 不安全：backend 源码（$($backendNewest.Name) $($backendNewest.LastWriteTime)）比 sidecar 产物（$($sidecar.LastWriteTime)）新，请去掉 -SkipBackend 重新构建"
    }
    Write-Host "==> [2/$steps] 跳过 sidecar 构建（-SkipBackend，源码无更新）" -ForegroundColor Yellow
}

Write-Host "==> [3/$steps] 构建桌面应用 (tauri build) ..." -ForegroundColor Cyan
# npm 需 Odd 转义链把 -c JSON 透传给 tauri CLI（直接引号会被 npm 剥掉）
npm run tauri build -- $configArg

Write-Host "完成。产物位于 src-tauri/target/release/bundle/。" -ForegroundColor Green
