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
#       侧栏的“眼睛”图标来自 assets\icons(PNG), 必须一起打进去。
param([switch]$OneFile)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

Write-Host "== 1/3 打包 onedir(启动更快) =="
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name WTRouteTester `
    --icon app.ico `
    --version-file version_info_route.txt `
    --add-data "assets\icons;assets\icons" `
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
        --icon app.ico `
        --version-file version_info_route.txt `
        --add-data "assets\icons;assets\icons" `
        route_tester.py
}

Write-Host "== 完成 =="
Write-Host "  目录版: dist\WTRouteTester\WTRouteTester.exe"
if ($OneFile) {
    Write-Host "  单文件: dist\WTRouteTester_Portable.exe"
}
