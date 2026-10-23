# 把"跑图线路测试器"的文件从本工作区同步到它的独立仓库
#
#   用途: 两个程序都在 d:\course\VSCODE 下并列开发, 但**跑图线路测试器的代码只属于
#         Virream/WTRouteTester 仓库**(黑名单仓库已在 .gitignore 里排除它)。
#         当年靠手抄同步, 漏过 record_overlay_dialog.py —— 所以改成清单式, 一条命令搞定。
#
#   .\tools\sync_route_tester.ps1            # 同步(只拷有变化的)并打印目标仓库状态
#   .\tools\sync_route_tester.ps1 -DryRun    # 只看会动哪些文件, 不拷
#
# 注意: 本脚本自己**不提交**; 同步完请在 d:\course\VSCODE\WTRouteTester 里
#       git add/commit/push(那里是发布仓库)。
param([switch]$DryRun)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $PSScriptRoot
$dst = "d:\course\VSCODE\WTRouteTester"

if (-not (Test-Path (Join-Path $dst ".git"))) {
    Write-Host "!! 找不到目标仓库: $dst" -ForegroundColor Red
    exit 1
}

# 清单: "源相对路径" 或 "源相对路径>目标相对路径"
$manifest = @(
    # 入口 / 打包 / 版本
    "route_tester.py",
    "build_route_tester.ps1",
    "version_info_route.txt",
    "requirements-gl.txt",
    "docs\CHANGELOG.md>CHANGELOG.md",
    # 图标
    "icon\WTRouteTester.ico",
    "icon\icon_512X512_WTRouteTester.png",
    # 测试
    "tests\route_tester_test.py",
    "tests\global_record_test.py",
    # 文档(含两边共用的 8111 经验)
    "docs\跑图线路测试器.md",
    "docs\WTRouteTester-FEATURES.md",
    "docs\8111使用经验.md",
    # 诊断/辅助工具
    "tools\make_app_icon.py",
    "tools\route_map_gl_demo.py",
    "tools\probe_playback_stutter.py",
    "tools\probe_vram.py",
    "tools\probe_watcher_fix.py",
    "tools\probe_frame_align.py",
    "tools\probe_turn_radius.py",
    "tools\probe_map_quality.py",
    "tools\probe_map_upscale.py",
    # 源码模块(黑名单不用, 只在跑图测试器里)
    "wt81111g\global_hotkey.py",
    "wt81111g\global_record.py",
    "wt81111g\help_text.py",
    "wt81111g\hotkey_dialog.py",
    "wt81111g\icons.py",
    "wt81111g\kill_timeline.py",
    "wt81111g\map_cache.py",
    "wt81111g\map_manager.py",
    "wt81111g\record_overlay.py",
    "wt81111g\record_overlay_dialog.py",
    "wt81111g\route_instance.py",
    "wt81111g\route_map_gl.py",
    "wt81111g\route_map_view.py",
    "wt81111g\route_playback.py",
    "wt81111g\route_recorder.py",
    "wt81111g\route_store.py",
    "wt81111g\route_window.py",
    "wt81111g\sound.py",
    "wt81111g\sound_dialog.py",
    "wt81111g\tree_view.py",
    "wt81111g\watcher_dialog.py",
    "wt81111g\zones.py",
    # 两边共用但必须字形一致的文件
    "wt81111g\api8111.py",
    "wt81111g\config.py",
    "wt81111g\garage.py",
    "tools\trim_qt.py",
    "requirements.txt"
)

$copied = 0
$same = 0
$missing = @()
foreach ($item in $manifest) {
    $parts = $item -split ">", 2
    $rel = $parts[0]
    $out = if ($parts.Count -gt 1) { $parts[1] } else { $rel }
    $a = Join-Path $src $rel
    $b = Join-Path $dst $out
    if (-not (Test-Path $a)) { $missing += $rel; continue }
    if (Test-Path $b) {
        $ha = (Get-FileHash $a -Algorithm MD5).Hash
        $hb = (Get-FileHash $b -Algorithm MD5).Hash
        if ($ha -eq $hb) { $same++; continue }
        Write-Host ("更新  {0}" -f $out) -ForegroundColor Yellow
    } else {
        Write-Host ("新增  {0}" -f $out) -ForegroundColor Green
    }
    if (-not $DryRun) {
        $dir = Split-Path -Parent $b
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item $a $b -Force
    }
    $copied++
}

Write-Host ""
Write-Host ("共 {0} 项: 需同步 {1}, 已一致 {2}, 源缺失 {3}" -f $manifest.Count, $copied, $same, $missing.Count)
if ($missing.Count -gt 0) {
    Write-Host "源缺失:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "   $_" }
}
if ($DryRun) { Write-Host "(DryRun: 未复制)" -ForegroundColor Cyan; exit 0 }

Write-Host ""
Write-Host "== 目标仓库状态 ==" -ForegroundColor Cyan
git -C $dst status --short
Write-Host ""
Write-Host "下一步: cd $dst; git add -A 的替代 —— 显式列文件后 commit/push" -ForegroundColor Cyan
