# 参考文档归档

本目录归档了本项目开发过程中参考的外部文档(仅作学习与开发参考, 版权归原作者所有)。

## 目录结构

```
docs/
├── wtrti/     # WTRTI 官方文档(MeSoftHorny/WTRTI)
│   ├── README.md            # 仓库说明
│   ├── features.md          # 功能与 OSD 设置(内置 WTRTI OSD / RTSS / Gamescope / VR / 自定义指示器)
│   ├── lua-api.md           # Lua 脚本 API(getStateValue / getVehicleData / value_proc / Script Options)
│   ├── troubleshooting.md   # 常见问题(含"机库残留 OSD"等已知问题)
│   └── features-index.md    # 官方文档索引页
└── wt8111/     # 8111 接口文档(lucasvmx/WarThunder-localhost-documentation)
    ├── README.md            # 接口总览
    ├── State_State.md       # /state  — 载具实时状态(含 valid 布尔字段)
    ├── Mission_Mission.md   # /mission.json — 任务/对局状态(status: running|fail, objectives)
    ├── Mapinfo_MapInfo.md   # /map_info.json — 地图信息
    ├── MapObjects_MapObjects.md  # /map_obj.json — 玩家/载具位置与类型
    ├── Hudmsg_Hudmsg.md     # /hudmsg — 击杀/事件消息
    ├── Gamechat_GameChat.md # /gamechat — 对局聊天
    └── Indicators_Indicators.md   # /indicators — 仪表指示数据

# 实测分析(本项目基于真实对局数据验证的结论)
8111自定义对局实测分析.md   # 进出场信号 / 击杀消息格式 / 昵称收集结论(2026-08-12)
```

## 来源

| 归档 | 仓库 | 分支 | 说明 |
|---|---|---|---|
| `docs/wtrti/` | `github.com/MeSoftHorny/WTRTI` | master | WTRTI 官方文档; 注意: **程序源码未公开**, 该仓库仅文档 + GitHub 自动生成的 Source code(仓库快照) |
| `docs/wt8111/` | `github.com/lucasvmx/WarThunder-localhost-documentation` | master | 8111 本地接口社区文档(含 LICENSE, 引用时请遵守) |

## 关键字段速查(与项目相关)

### 对局状态判定(monitor.py)
- `/mission.json`:`status` 可取 `"running"` / `"fail"`;`objectives` 为任务数组(每项含 `primary` / `status` / `text`)
- `/state`:`valid` 布尔字段(是否处于对局/载具状态)
- 本项目判定: `mission.valid is True` **或** `(mission.status == "running" and objectives 非空)`;退出对局(回机库)后连续 3 秒确认才触发 `battle_ended`

### 玩家昵称收集(monitor.py / nickname_collector.py)
- `/hudmsg`(击杀列表)与 `/gamechat`(发言记录)用于逐步收集对局玩家昵称
- 主机玩家昵称前的 `⋇` 标记在解析时去除

### 注意
- 文档为社区整理, 可能随游戏版本更新略有出入; 字段以实际 8111 返回为准
- WTRTI 与该项目同为"外部透明 OSD + 官方 8111 数据"方案, 不注入游戏进程(详见 WTRTI features.md 的 OSD Setup)
