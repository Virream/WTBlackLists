# 一键打包脚本: 应用(onedir 启动更快) + ZIP 版 + 自解压安装版 + 源码版
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

# 当前版本号(来自 config.py APP_VERSION), 产物文件名带上版本号
$version = & $py -c "import re,io,sys; t=io.open('wt81111g/config.py',encoding='utf-8').read(); m=re.search('APP_VERSION\\s*=\\s*[\\x22\\x27]([^\\x22\\x27]+)[\\x22\\x27]', t); sys.stdout.write(m.group(1) if m else '0.0.0')"
if (-not $version) { $version = "0.0.0" }
Write-Host "当前版本: $version"

Write-Host "== 1/5 生成图标(app.ico) =="
& $py "tools\png_to_ico.py"

Write-Host "== 2/5 打包应用 (onedir, 启动更快) =="
# 方案: 真人浏览器兜底(启动系统真实 Edge/Chrome 并读取 DOM) + 应用内
# WebView2 浏览器(pywebview/pythonnet, 子进程抓取, 通常自动过验证)。
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name WTBlackList `
    --icon app.ico `
    --add-data "app.ico;." `
    --collect-all pywebview `
    --collect-all pythonnet `
    --collect-all clr_loader `
    --collect-all bottle `
    --collect-all proxy_tools `
    --version-file version_info.txt `
    main.py

Write-Host "== 2.5/5 精简 Qt 体积(删除用不到的 Qt6 DLL/翻译/图片插件) =="
& $py "tools\trim_qt.py"

Write-Host "== 3/5 生成应用载荷 zip =="
& $py "tools\make_payload.py"

Write-Host "== 4/5 生成 ZIP 版 =="
Copy-Item "build_assets\app_payload.zip" "dist\WTBlackList_${version}.zip" -Force

Write-Host "== 5/5 打包自解压安装程序 =="
& $py -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --uac-admin `
    --name WTBlackList_Setup `
    --icon app.ico `
    --add-data "app.ico;." `
    --add-data "build_assets\app_payload.zip;." `
    --version-file version_info_installer.txt `
    "tools\installer\installer_main.py"
# 安装程序文件名带上版本号
Remove-Item "dist\WTBlackList_Setup_${version}.exe" -Force -ErrorAction SilentlyContinue
Rename-Item "dist\WTBlackList_Setup.exe" "WTBlackList_Setup_${version}.exe"

Write-Host "== 6/6 生成源码 zip(放入 dist) =="
& $py "tools\make_source_zip.py"
Copy-Item "WTBlackList_source.zip" "dist\WTBlackList_source_${version}.zip" -Force
Remove-Item "WTBlackList_source.zip" -Force -ErrorAction SilentlyContinue

Write-Host "== 完成 =="
Write-Host "  应用目录: dist\WTBlackList\WTBlackList.exe"
Write-Host "  ZIP 版:   dist\WTBlackList_${version}.zip"
Write-Host "  安装版:   dist\WTBlackList_Setup_${version}.exe"
Write-Host "  源码版:   dist\WTBlackList_source_${version}.zip"
