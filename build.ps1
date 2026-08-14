# 一键打包脚本: 应用(onedir 启动更快) + ZIP 版 + 自解压安装版
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

Write-Host "== 1/5 生成图标(app.ico) =="
& $py "tools\png_to_ico.py"

Write-Host "== 2/5 打包应用 (onedir, 启动更快) =="
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name WTBlackList `
    --icon app.ico `
    --add-data "app.ico;." `
    --version-file version_info.txt `
    main.py

Write-Host "== 2.5/5 精简 Qt 体积(删除用不到的 Qt6 DLL/翻译/图片插件) =="
& $py "tools\trim_qt.py"

Write-Host "== 3/5 生成应用载荷 zip =="
& $py "tools\make_payload.py"

Write-Host "== 4/5 生成 ZIP 版 =="
Copy-Item "build_assets\app_payload.zip" "dist\WTBlackList.zip" -Force

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

Write-Host "== 完成 =="
Write-Host "  应用目录: dist\WTBlackList\WTBlackList.exe"
Write-Host "  ZIP 版:   dist\WTBlackList.zip"
Write-Host "  安装版:   dist\WTBlackList_Setup.exe"
