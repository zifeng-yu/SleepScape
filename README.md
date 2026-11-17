# SleepScape

眠境 · 个人睡眠与卧室环境观测系统。

更新：2026-11-17。

第一版负责可靠采集、保存和展示；按2026-11-17要求新增基于完整双源证据的LLM睡眠解读方案。
当前已实现 **Forerunner 265 中国区 Garmin 采集与本地看板**；硬件已到货并核对接线，主控已升级至支持USB本机采集的固件。环境数据可通过USB直接保存到Mac，无需先配置Wi-Fi、HTTPS及设备令牌；实测与长期验收边界见核验报告。云端上传和环境曲线联动仍待完成。

## 当前状态

| 部分 | 状态 |
| --- | --- |
| Garmin采集、保存、修订和核验 | 已完成真实取数、独立对账与重放验证 |
| Garmin看板 | 夜晚详情、7/30日历史、数据状态；共用时间轴、来源与缺失提示已实现 |
| 本机访问与备份 | 默认仅本机；独立密码、在线备份和临时恢复核验已实现 |
| 环境硬件 | C008-C + AHT20 SEN0528 + VEML7700 SEN0228 + DFR0759分线模块；接线、芯片/Flash识别及程序写入通过；当前固件0.3.0支持USB本地采集与原HTTPS模式，短采、软件测试及长期待办统一见核验报告 |
| 云端与自动运行 | Linux/Caddy/定时器模板已提供，尚未安装；2核2G与长期运行待验收 |
| LLM睡眠解读 | qwen4-max模型层、输入/Prompt/输出及栏目方案已建立；双源齐全才首次生成，每晚一份；程序和真实模型联调待完成 |
| 最新补齐计划 | [非云端程序与文件](docs/plans/completion.md)统一列待开发模块、文件、依赖与验收；自动备份/容量保护仍待实现 |

详细证据只维护在 [核验报告](docs/verification.md)。API到本地数据的一致性验证不等于手表测量准确度验证。
Garmin未提供分期区间，因此当前展示睡眠窗口和阶段累计，不生成分期时间线。

## 启动使用

本机双击 [启动看板.command](启动看板.command)，再打开 <http://127.0.0.1:8000>；终端按 `Ctrl+C` 停止。
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

今日睡眠可显示为暂定，全天统计截至昨日。手机/外网访问、历史补采、密钥配置及恢复操作统一见[运行手册](docs/operations.md)。

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
| [补齐计划](docs/plans/completion.md) | 剩余程序与文件、开发依赖和验收矩阵 |
| [模型层方案](docs/plans/models.md) | qwen4-max连接、配置、协议和审计边界 |
| [LLM睡眠分析方案](docs/plans/llm.md) | 双源完整性、分析输入、Prompt、报告与调用预算 |
| [自动维护方案](docs/plans/maintenance.md) | 自动备份、保留、离机复制和容量保护 |
| [AGENTS.md](AGENTS.md) | 后续开发者和代理须遵守的工作约定 |

## 程序分层

```text
src/sleepscape/
  domain/       纯数据模型、转换、日期/负荷规则、对账与降采样
  adapters/     Garmin官方接口目录、认证与受限网络传输
  storage/      SQLite读写、运行元数据和私有报告文件
  services/     采集、核验、看板视图与备份用例编排
  web/          访问保护与静态页面模块
  app.py        只读看板与独立认证的精确设备POST入口
  cli.py        命令入口
  config.py     配置与区域约束
docs/           当前方案、数据合同、Prompt、操作与核验文档
deploy/         尚未启用的服务器配置模板
tests/          保留的合成回归测试，browser/为隔离页面测试
firmware/       ESP-IDF主程序、纯C采样/缓存核心、原生回归与配置样例
tools/          固件安装/编译/刷写/配置入口、开发产物清理
```

生产运行使用Python 3.12、FastAPI、SQLite WAL和包内静态资源。
Node/Playwright仅用于浏览器验收；生产服务不需要前端构建、Redis、Celery或外部数据库。
看板保持只读，浏览器不接触Garmin Key/token；设备POST独立认证，默认关闭，须显式登记后开启。程序与文档只保留当前实现；原始观测、修订和恢复备份属于数据证据。

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

剩余模块、文件和实施顺序统一见[补齐计划](docs/plans/completion.md)。重点是环境UTC查询与看板曲线、双源完整性检查、模型连接与报告、每日编排和自动维护，沿用现有领域/适配/存储/服务分层。

USB四通道短采、真实读数与入库已经通过；固定摆位、持续观测和故障恢复仍待验收。可继续使用[USB本机采集](docs/operations.md#usb本机采集)取得数据，Mac需保持唤醒和连接。接线、首次安装及普通升级分别在[硬件方案](docs/plans/hardware.md)和[运行手册](docs/operations.md#首次安装与后续升级)维护；现有样机不重复整片清除。

Garmin定时模板为上海时间09:00一次，尚未安装；可配置N日回查、缺项检查及LLM生成的每日编排待开发。联网硬件启动后全天采集、分钟汇总，正常约5分钟批量上传；实际Wi-Fi/HTTPS上传与断网补传待验收。

只有Garmin与环境必需数据齐全才首次送模型，缺项明示、次日补取，每晚最多一份成功报告且不自动修订。[机器合同](docs/contracts/sleep-analysis.schema.json)与[Prompt](docs/prompts/sleep-analysis-system.txt)是待实现功能规范；模型提供有证据的观察和有限假设，不宣称因果、最大影响因素或医疗结论。

公网IP＋HTTPS及自动证书续期方案已确定。服务器信息、模型URL/Key、离机备份目标和长期验收仍待落实；现有USB路径可独立使用。
