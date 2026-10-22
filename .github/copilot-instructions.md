# 本仓库的协作规则(给 AI 助手 / 自动化工具)

## 0. 【最高优先级】禁止读取游戏文件

**不得以任何方式读取、扫描、探测用户的游戏文件**,除非用户在当次对话中**明确授予权限**。

- 范围(不限于):游戏安装目录(`E:\SteamLibrary\steamapps\common\War Thunder\`)、
  `.game_logs/*.clog`、`Documents/My Games/WarThunder/Saves/**`、`cache/`、`levels/`、
  回放文件、任何游戏存档/配置/日志;
- **动之前必须先问**,并说明:要读哪个路径、读什么内容、为什么非读不可;
- **用户同意一次 ≠ 永久同意**:每次都当作不授权;
- 原因:用户担心触发反作弊(BattleEye/EAC)导致封号。

### 需要游戏内数据时怎么做

1. **优先使用游戏自带的 `http://127.0.0.1:8111` 本地 HTTP 接口** —— 官方提供、公开文档、纯读,
   是本项目的唯一合法数据来源(`/indicators`、`/map_obj.json`、`/map.img`、`/map_info.json` 等);
2. 8111 拿不到的信息(例如**地图名**),就改设计绕过它 ——
   例如本项目用图片哈希当地图身份, 名字交给用户手动命名(见 `docs/8111使用经验.md` §3.6);
3. 仍然没有别的办法时, **先征求用户同意**;被拒绝就换方案,不要绕道实现。

## 1. 本工作区里有两个**独立程序** —— 代码与推送必须分清

`d:\course\VSCODE\WT81111G` 只是**同时开发它们的工作区**(都在调 8111, 方便经验复用),
两个程序互不依赖, 不是同一个产品。

| 内容 | 只推给 |
|---|---|
| 黑名单助手(本项目主体) + **两边共用的文档** | `Virream/WTBlackLists` |
| 跑图线路测试器(WTRouteTester)的**一切**: 源码 / 打包脚本 / 图标 / 它的文档 / 测试 / 专用探针 | `Virream/WTRouteTester`(`d:\course\VSCODE\WTRouteTester`) |

- 跑图测试器的文件在本地 `.gitignore` 里有**一整块明确清单**(搜「跑图线路测试器」);
  **不要 `git add -f` 把它们塞回本仓库**, 也不要往 `WTBlackLists` 推。
  典型: `route_tester.py`、`wt81111g/route_*.py`、`wt81111g/help_text.py`、`icons.py`、
  `zones.py`、`map_cache.py`、`config.py` 里的 `ROUTE_VERSION`、`version_info_route.txt`、
  `build_route_tester.ps1`、`icon/*WTRouteTester*`、`tests/route_tester_test.py`、
  `tools/probe_playback_stutter.py` 等专用探针、`docs/跑图线路测试器.md`、
  `docs/WTRouteTester-FEATURES.md`;
- 改完跑图测试器代码: 跑 `tools\sync_route_tester.ps1`(清单式同步到 `WTRouteTester`,
  支持 `-DryRun`), 再到**那个**仓库 `git add/commit/push`;
  **一次提交只装属于同一个仓库的文件**;
- 确实算两边共用的只有: `wt81111g/api8111.py`、`config.py`、`garage.py`、`assets/`、
  `tools/trim_qt.py`、`README.md`、`docs/CHANGELOG.md`、`docs/8111使用经验.md`;
- 拿不准某个文件算哪边 → **先问用户**, 不要默认“一起推”。

## 2. 其它约定

- 回复语言:简体中文;
- **不要过度写测试**:没有改动到的功能不必回归;只在改动相关代码时跑针对性用例;
- 本仓库启用 git:完成一块工作就提交一次(方便回滚);
  PowerShell 里提交中文信息用 `git commit -F <utf8 文件>` 最稳;
- 打包:黑名单用 `build.ps1`(输出 `dist\WTBlackList*`);跑图线路测试器的
  `build_route_tester.ps1` 在 `WTRouteTester` 仓库,那个脚本会暂存并恢复
  `dist\WTRouteTester\data`, **不要**让重建清掉用户数据(录制的轨迹 + 地图缓存);
- 技术约定见 `docs/8111使用经验.md`、`docs/CHANGELOG.md` 与各模块 docstring;
  跑图线路测试器的文档在 `Virream/WTRouteTester` 的 `docs/` 里。
