# 打包"跑图线路测试器"为 exe(独立程序, 与黑名单助手互不影响)
#
#   .\build_route_tester.ps1              # ★默认: 只打 onedir 目录版(启动快, 推荐)
#   .\build_route_tester.ps1 -OneFile     # 仅"需要多版本分发"时才用(额外产出单文件版, 耗时约翻倍)
#
# 注意: 测试/迭代阶段不要加 -OneFile, 避免重复耗时。
#
# 产物:
#   dist\WTRouteTester\WTRouteTester.exe           目录版(整个文件夹一起拷走)
#   dist\WTRouteTester_Portable.exe                单文件版(启动稍慢, 需解压到临时目录)
#
# 说明: 仅依赖 PyQt6 + requests, 无需 pywebview/pythonnet(那是黑名单主程序的依赖)。
#       精简 Qt 时**保留 jpeg/webp 图片插件** —— 地图图片来自 8111 的 /map.img。
#       侧栏的“眼睛”图标来自 assets\icons(PNG), 必须一起打进去;
#       窗口/任务栏图标来自 icon\(PNG), exe 图标用 icon\WTRouteTester.ico
#       (由 tools\make_app_icon.py 从那张 AI 画的 PNG 转出多尺寸 ico, 重跑即可)。
#
# ⚠ 数据保护: 程序把数据写在 **exe 同级目录的 data\** 下(录制的 routes.json 与
#   缓存的地图 maps\)。PyInstaller 重建会清掉整个 dist\WTRouteTester,
#   所以打包前会把 data 暂存到 dist\_data_backup、 完成后原样搬回。
param([switch]$OneFile)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

$dataDir = Join-Path $root "dist\WTRouteTester\data"
$dataBak = Join-Path $root "dist\_data_backup"
if (Test-Path $dataDir) {
    Write-Host "== 0/3 暂存用户数据(routes.json / maps\ 缓存) =="
    Remove-Item $dataBak -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item $dataDir $dataBak -Force
}

Write-Host "== 1/3 打包 onedir(启动更快) =="
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name WTRouteTester `
    --icon "icon\WTRouteTester.ico" `
    --version-file version_info_route.txt `
    --add-data "assets\icons;assets\icons" `
    --add-data "icon\icon_512X512_WTRouteTester.png;icon" `
    route_tester.py

Write-Host "== 2/3 精简 Qt 体积(保留 jpeg/webp 图片插件, 保证地图图片可解码) =="
& $py "tools\trim_qt.py" WTRouteTester --keep-imageformats

if ($OneFile) {
    Write-Host "== 3/3 打包单文件版 =="
    & $py -m PyInstaller `
        --noconfirm `
        --onefile `
        --windowed `
        --name WTRouteTester_Portable `
        --icon "icon\WTRouteTester.ico" `
        --version-file version_info_route.txt `
        --add-data "assets\icons;assets\icons" `
        --add-data "icon\icon_512X512_WTRouteTester.png;icon" `
        route_tester.py
}

if (Test-Path $dataBak) {
    Write-Host "== 恢复用户数据(routes.json / maps\ 缓存) =="
    New-Item -ItemType Directory -Force -Path (Split-Path $dataDir -Parent) | Out-Null
    Move-Item $dataBak $dataDir -Force
}

Write-Host "== 完成 =="
Write-Host "  目录版: dist\WTRouteTester\WTRouteTester.exe"
if ($OneFile) {
    Write-Host "  单文件: dist\WTRouteTester_Portable.exe"
}
