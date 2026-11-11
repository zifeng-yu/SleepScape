# SleepScape

眠境 · 个人睡眠与卧室环境观测系统。

第一版负责可靠采集、保存和展示，为后续个人睡眠研究积累数据。
当前已实现 **Forerunner 265 中国区 Garmin 采集与本地看板**；环境固件、设备上传与存储已进入可编译、可复验的采购前软件阶段；采购、实机与环境曲线联动尚待验收。

## 当前状态

| 部分 | 状态 |
| --- | --- |
| Garmin采集、保存、修订和核验 | 已完成真实取数、独立对账与重放验证 |
| Garmin看板 | 夜晚详情、7/30日历史、数据状态；共用时间轴、来源与缺失提示已实现 |
| 本机访问与备份 | 默认仅本机；独立密码、在线备份和临时恢复核验已实现 |
| 环境硬件 | 已确认C008-C + AHT20 SEN0528 + VEML7700 SEN0228 + DFR0759分线模块；单总线3.3V免焊接线，含Mac转接头约300元，可为接口匹配及可靠性适量上浮；本轮适配与验证状态见核验报告，采购及实机待验收 |
| 云端与自动运行 | Linux/Caddy/定时器模板已提供，尚未安装；2核2G与长期运行待验收 |

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
| [采购方案](docs/procurement.md) | 中国渠道、完整BOM、预算条件及超预算决策 |
| [数据契约](docs/data-contract.md) | 日期、单位、字段来源、缺失、质量及负荷计算口径 |
| [运行手册](docs/operations.md) | 安装、凭据、命令、部署、备份和开发检查 |
| [核验报告](docs/verification.md) | 当前通过证据、保留数据和未验收边界 |
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
docs/           唯一当前文档与Garmin/看板/硬件方案
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

主选C008-C集成麦克风主控、Gravity AHT20 SEN0528、VEML7700 SEN0228与DFR0759分线模块。两块传感器用附线插到分线模块，分线模块附线通过四根公对公跳线接主控底部3V3/GND/G21/G25；不用焊接或额外购买Grove线。
用户已确认采用可核实的品牌官网替代选型；采购前适配、编译和协议验证以[核验报告](docs/verification.md)为准。下一步按[采购方案](docs/procurement.md)核对实际结算及附件，买一套样机，按运行手册完成USB刷写与配置，再做真实采样、校时和断网补传验收。
全新板首次安装使用`make firmware-first-flash PORT=/dev/cu.实际串口`，读清提示并手动输入`FIRST-INSTALL`后才清除出厂内容并写入；这一步会删除板上全部配置和数据。日后升级使用保留配置及离线队列的`make firmware-flash`，不能重复用首次安装命令。刷写和USB配置后分别运行`make firmware-status PORT=/dev/cu.实际串口`核对版本及配置状态；实际上传另查服务端接收。详细步骤见[运行手册](docs/operations.md#首次安装与后续升级)。
约300元为目标，已计入Mac转接头。用户接受为原厂线材、接口匹配和整套可靠性适量上浮，优先避免到货后因选型不合适而退换；不为无必要功能堆料。清单额度并非锁定成交价，严重超支或改变关键能力时仍提供具体对照由用户决定。
扩展优先保护历史数据、接口和模块边界；接受为性价比局部更换主控/传感器，不保证硬件永远不用改。
环境数据独立保存，通过UTC时间轴与Garmin记录关联，扩展现有看板；连续夜间验证后再部署长期自动运行。
本轮不加入影响因素排名、相关性分析或因果判断。
