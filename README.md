# SleepScape

眠境 · 个人睡眠与卧室环境观测系统。

更新：2026-11-19。

SleepScape 将中国区 Garmin 的睡眠与活动记录、卧室的温度、湿度、光照和相对声音放在同一条时间轴上。云端每天 09:00 回查 Garmin 数据。双源数据达到分析门槛后，系统使用 qwen3.8-max 生成当晚首份解读，结合个人历史说明睡眠表现，并展示所引用的证据。

截至 2026-11-19，P1—P6 均已实现并部署。11 月 19 日的云端日任务已自动发布当日首份报告，当天更新的 Prompt 已完成一次手动重读，后续自然调度仍待观察。设备目前由移动电源独立采集，下一项实机验收是确认常驻 USB 电源。看板入口为 [SleepScape](https://39.107.47.198/sleep)，登录信息保存在本机私有文件 `.secrets/cloud-access.txt`。访问步骤见[操作手册](docs/operations.md#看板入口与持久登录)。

## 当前状态

| 部分 | 状态 |
| --- | --- |
| Garmin采集、保存、修订和核验 | 已完成真实取数、独立对账与重放验证 |
| Garmin看板 | 夜晚详情、7/30日历史、数据状态；四卡来源独立展开，按实际观测显示状态；本机和云端从2026-11-18睡眠归属日起展示，17日晚跨夜环境照常关联，原始历史与30日负荷背景保留 |
| 本机访问与备份 | 默认仅本机；独立密码、在线备份和临时恢复核验已实现 |
| 环境硬件 | C008-C + AHT20 SEN0528 + VEML7700 SEN0228 + DFR0759分线模块；接线、芯片/Flash识别及程序写入通过；当前固件0.3.0支持USB本地采集与原HTTPS模式，短采、软件测试及长期待办统一见核验报告 |
| 环境时间对齐与查询（P2） | 已实现纯时间映射、显式派生索引、只读查询和备份校验；已启用的索引随数据迁入云端并通过核验，原始观测保持不变 |
| 环境曲线与设备状态（P3） | 四环境指标与Garmin共用带日期的时间轴，各轨显示指标/单位与数值刻度；图例按指标归并，原始配置/启动分段保留；关联记录按运动、午睡、声音摘要分类，支持无Garmin的自定义环境窗口 |
| 每日采集编排（P5） | 已实现严格N日配置、daily-run、持久运行状态、同日幂等与显式失败重试、补采及看板缺口提示；云端已连续两日自然执行，11月19日采集后完成资格检查并自动发布当日首份报告 |
| 自动维护（P6） | 已实现成套备份/恢复核验、受控保留、磁盘保护、路径/SSH导出及维护看板；云端11月18日04:00首次自然执行及备份核验通过，迁移后的成套恢复核验通过；真实离机目标尚未配置 |
| 云端部署 | 私有数据与配置已迁入阿里云北京服务器；可信公网IP HTTPS、访问保护、接收端和自动续期机制已启用，重载通过；自然续期与长期运行待观察，原5688服务保留 |
| 硬件联网 | 真实2.4GHz Wi-Fi、校时、TLS、入库查询及四通道短时连续采集通过；Mac与移动电源对照均通过，当前移动电源独立运行。原充电头供电路径待核查，常驻电源/续航、断网补传及整夜验收待完成 |
| 模型基础层与LLM栏目（P1） | 已实现；2026-11-17本机qwen4-max真实合成连接通过。2026-11-18按用户指定切换qwen3.8-max，旧连接证据不代替新模型核验。夜晚/历史/状态页可读配置、核验、预算；完整双源缺项由P4检查栏目展示；GET零模型调用 |
| 双源睡眠解读（P4） | 已实现同快照资格与证据、逐指标基线、模型预算/重试、唯一报告、来源变化提示及证据卡；每个睡眠夜晚累计最多10次、每天合计最多30次，成功即停；真实验收见核验报告 |
| 建设与验收顺序 | [剩余建设计划](docs/plans/completion.md)区分开发、部署与实机验收；本轮报告发布与真实运行结果见核验报告 |

详细证据只维护在 [核验报告](docs/verification.md)。API到本地数据的一致性验证不等于手表测量准确度验证。
Garmin未提供分期区间，因此当前展示睡眠窗口和阶段累计，不生成分期时间线。

## 启动使用

本机双击 [启动看板.command](启动看板.command)，再打开 <http://127.0.0.1:8000/sleep>；终端按 `Ctrl+C` 停止。
也可以在项目根目录执行：

```sh
uv run --locked sleepscape serve
```

首次在新机器安装：

```sh
uv python install 3.12
uv sync --locked
cp -n .env.example .env
uv run --locked sleepscape doctor
```

需要先安装[uv](https://docs.astral.sh/uv/getting-started/installation/)；依赖隔离在`.venv`，配置见`.env.example`。
所有相对路径按启动目录解析，须从仓库根目录运行。
看板启动和刷新只读本地库。完成手表与Garmin Connect同步后，显式更新：

```sh
uv run --locked sleepscape garmin-sync --plan-only  # 离线检查范围
uv run --locked sleepscape garmin-sync            # 实际联网同步
```

已取得的有效睡眠数据直接展示，不因日期是今天统一标“待确认”；缺失、无效和失败旧值分别说明，全天统计截至昨日。后续补采及有效修订继续保留，已有数值不等于永远不会变化。手机/外网访问、历史补采、密钥配置及恢复操作统一见[运行手册](docs/operations.md)。

模型状态可离线查看；显式合成检查仅发送合成请求：

```sh
uv run --locked sleepscape model-status
uv run --locked sleepscape model-check --live  # 一次合成请求，不发送健康数据
```

本机模型地址和密钥保存在被Git忽略的私有配置。用户已确认现有配置用于本机测试及云端09:00自动分析；供应商公开使用边界见[模型方案](docs/plans/models.md)。报告首次使用须按[运行手册](docs/operations.md#睡眠报告生成)显式初始化，手动生成使用`sleep-analyze --date YYYY-MM-DD --live`。`SLEEPSCAPE_LLM_AUTOMATIC_ENABLED`默认false，云端按用户要求启用，具体生效见核验报告。

## 文档分层

每项规则只有一个维护位置；不保留平行旧稿或重复方案。

| 文档 | 负责内容 |
| --- | --- |
| [项目架构](docs/architecture.md) | 系统边界、程序依赖、数据流、资源约束和下一阶段衔接 |
| [Garmin方案](docs/plans/garmin.md) | 官方接口、采集窗口、回查、故障与上游限制 |
| [看板方案](docs/plans/dashboard.md) | 页面、只读API、时间轴、交互及降级展示 |
| [硬件方案](docs/plans/hardware.md) | 研究依据、选型接线、固件、缓存、上传与整条链路验收 |
| [采购方案](docs/procurement.md) | 已购组合、完整BOM、参考预算和补购边界 |
| [数据契约](docs/data-contract.md) | 日期、单位、字段来源、缺失、质量及负荷计算口径 |
| [运行手册](docs/operations.md) | 安装、凭据、命令、部署、备份和开发检查 |
| [核验报告](docs/verification.md) | 当前通过证据、保留数据和未验收边界 |
| [建设计划](docs/plans/completion.md) | 剩余程序、配置/部署、实机验收及执行顺序 |
| [模型层方案](docs/plans/models.md) | 当前qwen3.8-max配置、协议和审计边界 |
| [LLM睡眠分析方案](docs/plans/llm.md) | 双源完整性、分析输入、Prompt、报告与调用预算 |
| [自动维护方案](docs/plans/maintenance.md) | 自动备份、保留、离机复制和容量保护 |
| [AGENTS.md](AGENTS.md) | 后续开发者和代理须遵守的工作约定 |

## 程序分层

```text
src/sleepscape/
  domain/       纯数据模型、日期/负荷、质量/睡眠证据、校验与降采样
  adapters/     Garmin官方接口目录、认证与受限网络传输
  models/       通用模型配置、工厂、兼容协议与安全错误/审计
  storage/      SQLite事实/报告/审计读写、运行元数据及备份
  services/     采集、资格/证据、预算调用与唯一发布、看板视图和备份
  resources/    唯一包内睡眠输入输出Schema与system Prompt
  web/          访问保护与静态页面模块
  app.py        只读看板与独立认证的精确设备POST入口
  cli.py        命令入口
  config.py     配置与区域约束
docs/           当前方案、数据合同说明、合成样例、操作与核验文档
configs/        可提交的模型身份和请求上限；不含URL或Key
deploy/         已部署服务对应的systemd与公网IP HTTPS配置模板
tests/          保留的合成回归测试，browser/为隔离页面测试
firmware/       ESP-IDF主程序、纯C采样/缓存核心、原生回归与配置样例
tools/          固件安装/编译/刷写/配置入口、开发产物清理
```

生产运行使用Python 3.12、FastAPI、SQLite WAL和包内静态资源。
Node/Playwright仅用于浏览器验收；生产服务不需要前端构建、Redis、Celery或外部数据库。
看板保持只读，浏览器不接触Garmin或模型凭据；设备POST独立认证，默认关闭，须显式登记后开启。模型调用使用有界短时子进程，不占用Web请求或SQLite事务。程序与文档只保留当前实现；原始观测、修订和恢复备份属于数据证据。

## 开发与清理

```sh
make check            # Python回归、Ruff和格式检查
make firmware-setup   # 项目内安装固定ESP-IDF及编译工具（首次需要联网）
make firmware-build   # 完整ESP32固件编译与程序容量门槛
make firmware-check   # 原生C、缓存和固件JSON与后端互通验证
make browser-install  # 首次或清理依赖后，重建浏览器验收环境
make browser-check    # 仅使用隔离的合成数据
make clean            # 清理测试结果、构建产物和代码缓存
make clean-dev        # 另清理Node依赖和浏览器缓存
```

必要回归测试与锁文件保留。截图/测试报告/安装包按需生成、验证后清理，不作为项目长期文件保存。
清理工具不会删除源码、测试代码、`.venv`、`.firmware-tools/`、`.env`、`.secrets/`或`data/`；固件产物清理后可重新编译。

本机私有数据统一位于`data/`：数据库、生产核验报告、原始探测观测和恢复备份。
`.env`、`.secrets/`、`data/`、运行日志和开发产物均被Git忽略，不能上传真实数据或凭据。

## 下一阶段

先确认常驻 USB 供电，再观察连续夜晚的采样覆盖、断网补传和新版解读的自然执行。离机备份仍需选定目标并验证恢复，公网证书仍需观察首次自然续期。执行顺序与验收条件见[剩余建设与验收计划](docs/plans/completion.md)。
