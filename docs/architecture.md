# 项目架构与实施边界

更新：2026-11-23。本文维护项目范围、模块职责和后续扩展边界；最新实测结果集中在 [核验报告](verification.md)。

## 目标和约束

SleepScape用于个人睡眠观测。系统收集Garmin及卧室环境数据，在同一时间轴展示并保留来源和质量信息，
为后续研究积累连续记录。P1—P6 已实现并部署。逐夜解读与 7 日总结分别发布并可引用生成时证据，云端和实机通过范围以核验报告为准。

- 已确认设备为Forerunner 265、中国区账号，使用官方Personal API。
- 硬件目标约300元、免焊、新手可接线和写入程序，计入电源、数据线及USB-C电脑转接头；C008-C、SEN0528、SEN0228及DFR0759组合已到货、完成接线、USB短采和真实Wi-Fi/HTTPS上传。原充电头供电路径曾出现I²C通信错误，Mac和备用移动电源分别通过四通道短时复验；当前移动电源独立采集，常驻电源及长期稳定性仍待确认，具体证据见核验报告。三大平台无法确认时可采用品牌官网或正规经销商；完整成交金额及部分附件铭牌仍待核对。
- 保留数据和接口边界，允许为性价比局部换板/换传感器；用户接受为接口匹配与可靠性适量超出约300元目标；严重超支或改变关键能力时先列具体对照，不为无必要功能扩支。
- 设计基线为2核2GB、20GB磁盘、至少保存一年数据；已部署目标机为2核、约1.7GiB可见内存、40GB磁盘。部署时资源与并发查询已核对，长期性能和年度增长仍需持续实测。
- 程序和文档只维护一套当前实现；原始观测修订、请求审计和数据备份属于必要数据。
- 必要回归测试保留，截图、测试报告、缓存及构建产物可重建，不作为源文件长期维护。

## 晨起睡感的分层设计

领域层定义三档取值；存储层在现有 SQLite 中独立读写，复用容量准入和事务；服务层协调按日查询、保存及服务端时间。HTTP 层验证日期、载荷和权限，前端仅负责显示、读写请求与切日交互。读取路径保持只读，显式迁移前先完成备份和临时恢复核验，备份校验兼容无此表的现存备份，并覆盖新表结构和记录。

浏览器写入仅开放给[数据契约](data-contract.md#晨起睡感记录)规定的精确方法和路径。复用看板身份校验与 Host 检查，生产要求 HTTPS、已认证且请求来源与看板同源；本机模式沿用直接回环边界并检查同源。设备令牌只用于既有采集入口。日任务分别在两次取证快照中读取当夜或实际窗口的可选睡感，仅浏览器独立入口保存用户选项。

## 文档职责

[README 的文档索引](../README.md#文档分层)列出各文件的维护范围。模块与数据流在本页维护，字段、操作、验收分别见[数据契约](data-contract.md)、[运行手册](operations.md)及[核验报告](verification.md)。

## 程序分层

```text
显式CLI / 已启用09:00定时器 → services.daily → services.collection → adapters → Garmin官方API
                              ↓                  ↓
                       daily_runtime元数据   Garmin事实与审计
                               ↓（采集锁释放后）
                services.readiness → domain.analysis_readiness → storage.analysis
                当前只读GET亦可计算           显式保存检查，GET只读
                               ↓（自动开关开启，合格且尚无报告）
            services.sleep_analysis：编排与本地来源复查
                  ↓                           ↓
           sleep_inputs/domain特征      model_call/models
                               ↓                       ↓
                 storage.sleep_analysis ← 输出校验与事务内来源复查
                      唯一逐夜报告/证据 → 夜晚详情
                               ↓（逐夜阶段结束且复盘开关开启）
          services.weekly_review → 双源资格与窗口取证 → 同一模型预算与受监督调用
                               ↓
                 storage.weekly_review → 唯一截止日总结/证据 → 历史页

显式CLI / 已启用04:00定时器 → services.maintenance → services.backup_bundle
                               ↓                        ↓
                    backup_manifest受控清单      SQLite + 运行账本配套快照
                               ↓                        ↓
                    domain.retention规则 ← 核验后发布 → adapters.backup_export
                                                     路径 / 严格SSH目标

Garmin官方API → adapters → services：采集/核验
                                  ↓
环境硬件 → 固件采样/时间/队列 ┬→ HTTPS精确POST → 设备认证 → services：环境接收
                            └→ USB串口 → 本机采集用例 ────┤
                                                        ├→ domain：纯校验/计算
                                                        └→ storage：SQLite事务
                                                               ↑
浏览器页面 → 同源只读GET → services：视图/查询 ─────────────────────┘
     ↓
注册表 / 格式化 / Garmin与环境共轴图表 / 设备状态 / 每日任务卡 / 维护卡 / LLM资格、报告与生成时证据

看板头部 → 同源认证 PUT → services.morning_feeling → storage.morning_feeling → 私有 SQLite

显式合成模型核验 → services.model_health → services.model_call：有界子进程
                            ↓                          ↓
                 storage.model_runtime          models：协议/适配器 → 配置的模型服务
                     私有安全元数据                    （不读取事实库）
                            ↑
                   同源只读模型状态GET
```

| 层与位置 | 职责 | 依赖边界 |
| --- | --- | --- |
| `config.py` | 环境、区域、路径、时区、访问、每日回查范围、看板起始睡眠日及模型配置入口 | 读取设置不认证、不采集、不调用模型 |
| `adapters/` | 官方路径目录、请求计划、SDK认证、参数白名单、有限重试 | 只访问配置区域；不决定看板展示，不写业务库 |
| domain/ | Garmin归一化/对账/负荷、环境合同/时间，以及双源完整性与分析资格 | 纯计算不访问网络/数据库/Web；不以显示降采样判断质量；逐通道有效采样和连续受影响范围由同一策略判定，保留所有缺口 |
| `storage/` | Garmin事实/审计、环境设备/配置/记录/锚点、事务ACK、独立只读查询及模型安全运行元数据 | 只负责数据持久化与有界读取，不调用Garmin或模型 |
| `services/` | 采集/同步、环境接收与设备管理、核验、白名单视图、备份、模型核验与受限调用编排 | 组装适配/领域/存储；模型外呼不持SQLite事务或Garmin锁 |
| `cli.py` | 参数校验、选择用例、安全摘要输出 | 不复制归一化或指标公式，不输出凭据和测量正文 |
| `services/event_audio.py`、`storage/event_audio.py` | 独立私有库接收事件片段，日任务按 Garmin 睡眠窗口裁剪；播放时重新核对窗口 | 音频随保留期限清理，排除在主库备份和模型证据之外 |
| `app.py`、`web/security.py`、`web/device_security.py` | 浏览器Host/密码边界；环境批次与事件音频各自精确 POST 路径的设备认证、HTTPS、限流和大小限制 | GET不触发采集或模型调用；设备POST不使用看板密码或Garmin凭据 |
| `web/static/` | 原生HTML/CSS/JavaScript页面、请求、格式化、图表及分析状态模块 | 不持有Garmin或模型凭据；无CDN、外部字体或浏览器持久化健康数据 |
| `firmware/components/sc_core/` | 音频统计、分钟聚合、事件、时间锚点和CRC分段队列 | 可在主机测试，不依赖Wi-Fi或服务器实现 |
| `firmware/main/` | 板卡驱动、共享I²C总线、NVS/USB配置、采集调度、USB/HTTPS与JSON/ACK桥接 | 两种传输复用驱动/聚合/队列且互斥；不上报PCM，不读取Garmin |
| `tools/firmware.py` | 固定SDK安装、构建、容量检查、互通测试、指定串口刷写/配置、本地采集入口及SDK串口子进程 | 工具链留在项目；数据保存委托服务层，串口依赖不全局安装 |
| `tests/`、`firmware/tests/` | 合成单元、C核心、跨语言协议、集成与浏览器回归 | 不访问真实账号，不使用个人健康值作为样本 |
| `deploy/` | systemd单worker、定时器和公网IP Caddy配置模板 | 当前目标机已显式安装启用；复制模板到其他机器不等于已部署 |
| `models/`（P1已实现） | 稳定model_ref、配置、工厂、协议、OpenAI兼容适配器、严格结构校验及脱敏审计 | 不读事实库，不放睡眠业务Prompt/重试/调度，不覆盖domain/models.py |
| `domain/environment_time.py`、`storage/environment_index.py`/`environment_reader.py`、`services/environment_index.py`/`environment_views.py`（P2已完成） | 环境时间映射、派生索引、有界读取、显式索引/核验用例 | 保留事实表，未知UTC不猜时间；本机索引已显式启用，GET不迁移 |
| `domain/metrics.py`/`environment_display.py`、`services/environment_dashboard.py`（P3已完成） | 唯一指标清单、窗口极值降采样、环境到统一时间轴的有界投影及实际状态 | 复用P2读层；不同配置/boot不连线，异常隔离；覆盖展示不代替P4完整性 |
| `domain/daily.py`、`storage/daily_runtime.py`、`services/daily.py`（P5已实现） | 上海09:00计划、严格N日回查、每日重入控制、运行账本、跨命令等待期限及只读状态 | 复用采集/重试/幂等能力；与手工采集共用互斥，环境上传独立，GET不触发日任务；采集后复查完整性，自动开关开启后进入独立报告用例 |
| `domain/{maintenance,retention}.py`、`storage/{usage,backup_manifest}.py`与`services/{maintenance,backup_bundle}.py`（P6已实现） | 纯水位/保留策略、有界磁盘读取、受控备份清单、SQLite与运行账本配套快照/恢复核验、任务编排及只读状态 | 独立维护锁；只在一致快照阶段借用采集/模型锁，网络导出不持这些锁；仅清理登记的备份，GET不触发维护 |
| `adapters/backup_export.py`及`backup_export_worker.py`（P6已实现） | 路径或严格SSH传输、目标摘要核对、受控发布和目标保留 | 显式配置才导出，固定协议及有界工作进程；目标/主机/凭据不进入公开状态，真实离机位置与存储加密另验 |
| domain/analysis_readiness.py、services/readiness.py（P4第一步已实现） | 闭合睡眠窗口、Garmin必需项及原始四通道、缺口/时间/配置判断；同一只读快照和显式保存 | GET只计算不保存；不外呼，不是LLM特征或报告证据包 |
| `storage/analysis.py`、`storage/sleep_analysis.py`（P4已实现） | 检查三表、独立报告四表与可选手动修订、不可变证据/成功报告、尝试终态和显式迁移 | 先备份/恢复核验再迁移；容量保护，备份涵盖可选表，发布写事务再次guard来源 |
| `domain/sleep_features.py`、`resources/`、`services/{sleep_inputs,sleep_analysis}.py`（P4） | 逐夜特征、可选睡感、输出验证、同快照、重试与唯一报告 | 外呼前结束事务；发布时复查来源，原报告保留生成时证据 |
| `domain/{sleep_review,sleep_window,weekly_review}.py`、`services/weekly_review.py`、`storage/weekly_review.py` | 历史中位数与日历窗口、7 日取证、共享预算重试、唯一总结和失败审计 | 历史统计独立于模型门槛，模型阶段和页面重核逐日复用完整性检查，GET 只读；物理调用统一经 P1 账本 |

依赖向业务和基础能力收敛，不让领域层反向导入服务层。Web视图与CLI核验使用相同的负荷口径；
来源选择、时效和展示状态保留在用例层。`acceptance.py`是用户可调用的离线数据验收能力，属于正式程序。

当前共享能力的唯一实现位置：

| 文件 | 职责 |
| --- | --- |
| `domain/calendar.py` | 纯日期区间，供请求计划及离线评估复用 |
| `domain/dashboard_dates.py` | 可选睡眠展示起点、列表范围与默认日期的纯规则；视图层共用，不改变采集、存储和环境UTC查询 |
| `domain/environment.py`、`domain/environment_integrity.py` | 环境上传合同、单位/计数/质量、档案一致性和纯校验 |
| `storage/environment.py` | 环境表、不可变配置、设备令牌哈希、幂等事务和提交后ACK |
| `services/environment.py` | 环境载荷解析、登记/配置/令牌轮换，生成私有JSON令牌文件 |
| `services/environment_usb.py` | 本机USB采集会话、身份和档案核对、单独的主机时钟锚点、提交后ACK及有界停止 |
| `domain/environment_time.py` | 环境单调时间与锚点的纯映射、时间质量和冲突处理 |
| `storage/environment_index.py`、`storage/environment_reader.py` | 可重建派生索引及有界只读查询，事实记录保持独立 |
| `services/environment_index.py`、`services/environment_views.py` | 显式索引/重建与影子核验，以及脱敏环境时间轴和状态视图；由P3复用 |
| `domain/metrics.py`、`domain/environment_display.py` | 纯指标注册表及保留原始分钟窗口/极值的显示降采样 |
| `services/environment_dashboard.py` | 有界环境序列、分钟详情、受限事件与元数据，合入统一timeline和独立环境GET |
| `web/static/metrics.js`、`web/static/environment.js` | 读取注册表，解释质量和分钟详情、渲染环境状态；与timeline.js共用横轴及图例 |
| `web/static/events.js` | 关联记录的安全DOM呈现、运动/午睡/声音摘要分类计数与范围说明；定位复用app协调的时间轴，不计算声源或觉醒次数 |
| `domain/training_load.py` | 严格30自然日负荷比较；不自行决定失败或陈旧数据能否参与 |
| `storage/reports.py` | 私有JSON报告原子写入和有界读取，供CLI、备份与状态视图复用 |
| `storage/reader.py` | 看板只读查询，以及同步计划需要的已完成运行元数据 |
| `domain/daily.py` | 上海自然日窗口、下一次计划时间、运行状态白名单及睡眠观测存在性规则，不判定双源完整性 |
| `storage/daily_runtime.py` | 有界私有日任务账本与等待期限、原子写入、共用采集互斥及只读运行结果恢复 |
| `services/daily.py` | 纯计划、显式执行、跨日补取、当日重入与显式重试、日任务安全状态；采集结束释放锁后调用独立资格/报告用例 |
| `domain/maintenance.py`、`domain/retention.py` | 纯容量许可与备份保留选择，不访问文件、网络或数据库 |
| `storage/usage.py` | 有界读取系统/数据分区与已知文件大小，统一容量保护及实际磁盘写满识别；状态不暴露路径 |
| `storage/backup_manifest.py` | 受控备份/维护记录、私有原子写入、状态校验与独立维护锁；读取不修复损坏清单 |
| `services/backup_bundle.py` | SQLite与独立运行账本的配套快照、文件/内容/引用验证及临时恢复演练；显式核验，不覆盖运行库 |
| `services/maintenance.py` | 容量预检、备份发布、导出与受控保留的显式用例，以及只读状态；失败不删除原始观测 |
| `adapters/backup_export.py`、`backup_export_worker.py` | 受限导出协议、路径/SSH工作进程、目标核验及登记副本的保留；不执行任意来源命令 |
| `configs/models.toml` | 可提交的模型能力、限制和默认model_ref配置，不保存URL或Key |
| `models/base.py`、`settings.py`、`factory.py`、`openai_compatible.py`、`schema_transport.py`、`audit.py` | 通用协议、配置与凭据边界、客户端创建、有界请求、显式响应协议/Schema传输兼容、原规范响应校验和白名单元数据 |
| `services/model_call.py` | 子进程外呼、总时限和资源回收，不承担睡眠证据准备 |
| `services/model_health.py`、`storage/model_runtime.py` | 显式合成能力核验、跨进程互斥、持久次数预算、私有运行元数据与只读安全状态 |
| domain/analysis_readiness.py、services/{readiness,analysis_views}.py | 同快照分级质量与分析资格及模型安全状态；详情计算当前结果，历史读取已保存检查，不调用模型 |
| `web/static/app.js` | 页面状态、URL、视图协调和用例交互 |
| `web/static/api.js` | 同源只读请求及安全错误转换 |
| `web/static/format.js` | 状态文案、单位与显式时区格式化，无网络或DOM依赖 |
| `web/static/dom.js` | 安全DOM元素与图标构造，不动态插入来源HTML |
| `web/static/timeline.js` | Canvas绘图、光标和范围交互；用回调通知页面，不自行请求API |
| `web/static/analysis.js`、`web/static/report.js` | 资格/质量、执行状态、历史摘要、成功报告、生成时证据及来源变化的安全渲染，无生成入口 |
| `web/static/daily.js` | 每日采集计划、最近执行及逐日睡眠记录状态的独立白名单渲染，无任务启动入口 |
| `web/static/maintenance.js` | 维护执行、本地/目标备份核验及磁盘保护的独立白名单渲染，无执行、删除或恢复入口 |

`index.html`通过`/sleep`及`/sleep/`提供，根入口已取消；页面入口与Basic/Cookie访问边界统一见[数据契约](data-contract.md)。`style.css`管理表现；ES modules直接加载，无打包器。
离线核验会排除失败来源，页面可保留旧值并标stale；它们共享数值公式，但各自保留明确的可用性策略。

## 存储和运行边界

主库为SQLite WAL。原始响应压缩保存，标准记录按来源分区维护当前值，请求/运行审计保留失败与修订证据。
视图使用独立只读事务，限制日期范围和显示点数；采集作为独立短时进程运行，以文件锁阻止并发采集。
备份和影子验收属于专用运维服务，内部包含SQLite操作，不把它们误列为可删除的测试脚本。
浏览器的刷新只重查本地观测和安全状态。生产部署为一个Uvicorn worker、SQLite、可选Caddy及systemd，不引入Redis、Celery或独立数据库服务。
Node/Playwright仅用于浏览器回归；生产安装使用Python依赖及包内静态文件。

P5通过`daily-run`执行，睡眠来源日窗口严格遵循配置，负荷背景另按既有方案补取。日任务与手工Garmin采集共用互斥和服务端要求的等待期限；底层有限重试仍只有一层，不在编排层整轮重复请求。当天采集完成后再次执行不会重抓Garmin，但可复查本地数据并按开关处理未发布报告；已有成功报告零模型请求。采集失败或中断通过显式重试恢复；成功空结果保留缺失事实，下一天在窗口内再取。

`data/daily-runtime.json`只保存最近最多31个任务日、至多128KiB的运行元数据，`data/collection-cooldown.json`保存跨命令等待期限；权限、结构或内容异常时拒绝自动重置。数据状态GET只读这些记录和既有SQLite事实，返回固定状态、计数和日期，不返回内部运行ID、原始错误、凭据或健康数值，不创建状态文件或修复账本。账本不可用只降级每日任务卡；睡眠观测读取失败另有固定状态。下一次09:00是计划时间，`installation_state=unknown`表示页面不探测systemd；云端定时器已启用的证据见核验报告，页面不能推断某次自然触发已完成。

P6通过`maintenance-run`执行，已启用的04:00 systemd定时器负责唤起，不新增应用常驻调度器。`maintenance-state.json`保存有界清单及运行状态；受控备份包由SQLite副本、已有的模型/每日运行/冷却账本和摘要清单构成。获取一致快照时使用既有采集和模型互斥，环境仍使用独立事务；复制后释放锁，再验证包、进行临时恢复核验和目标导出。模型次数、每日幂等及Retry-After必须随同批库恢复，不能只恢复旧库却保留较新completed账本。

保留策略只接收纯元数据，实际删除由服务层在受控清单、文件类型/摘要和最新有效副本保护通过后执行。已有手动备份盘点并核验，但不静默加入删除对象；未知文件、损坏清单或失败导出不能触发危险清理。恢复核验仅在临时目录进行，不自动覆盖生产库。路径与SSH副本使用相同受限协议和目标摘要核验，离机目的地未配置时如实返回状态；SSH严格校验主机身份，传输及外部等待不持数据库、Garmin或模型锁。

容量许可统一由`domain/maintenance.py`和`storage/usage.py`计算，调用位置覆盖大备份、新模型请求、Garmin写批次及环境接收，实际写满仍须失败且不ACK。只读状态查询仅做有界元信息/磁盘容量读取；不遍历备份目录、计算全文哈希、联网检查目标或修改保护状态。维护卡损坏独立降级，04:00安装状态同样为unknown，表示页面不探测systemd；实际安装、自然触发、长期性能与离机目标分别以核验报告为准。

P1的模型连接核验通过显式CLI执行，只发送合成任务，不发送睡眠、身份或环境观测。适配器限制请求/响应、模型名、结束原因及JSON Schema，拒绝重定向和不符合约定的结果；受限子进程保证外呼有总时限且可以回收，不新增常驻模型服务。当前按2026-11-18用户指令使用qwen3.8-max，沿用私有URL/Key；2026-11-17的qwen4-max合成核验仅为历史证据，不代替新模型核验。实际结果以[核验报告](verification.md)为准。

模型次数预算与尝试元数据保存在私有运行文件，使用独立进程锁和原子写入；配置变化、状态损坏和调用失败均有明确安全状态，不能通过自动重置坏文件清空预算。普通状态GET只读取白名单元数据并检查配置，不读取密钥内容、不修复文件、不发模型请求。真实输入、尝试与成功报告由storage/sleep_analysis.py的报告表及可选手动修订保存；现有检查三表不冒充模型输入，显式初始化报告表也不改变Garmin/环境事实。

GET /api/v1/nights/{D}/analysis返回implementation_stage=reports、实时资格、独立执行状态及可用的已保存报告/证据。Garmin与原始环境投影复用同一只读事务，缺项和通道统计由纯函数计算，GET不持久化。历史分别显示上次保存检查/时间和已发布报告摘要，不冒充当前来源未变化；局部错误不清空曲线，私有身份、指纹和错误正文不外发。

硬件层由C008-C的内置PDM麦克风和一条3.3V I²C总线构成；DFR0759将同一组电源/SDA/SCL分给AHT20与VEML7700，不是带独立通道地址的多路复用器。驱动适配限于板级与传感器转换，服务端继续接收带单位、覆盖率和质量的通道记录。当前光照采用固定增益和积分时间，饱和及低光限制必须随数据保留；后续换档、自动量程或换传感器需要新的测量配置与验证。

环境接收复用同一SQLite库，使用独立环境表，不改写Garmin原文、归一化记录或上游健康数据含义。
设备令牌与Garmin Key、浏览器密码分离；服务端只存哈希，新增配置采用新profile_id，不覆盖旧配置。USB本地采集不需要网络令牌，仅本机登记的身份使用不可用于HTTP认证的哨兵；转到云端时再显式配置真实认证。
环境HTTP入口默认关闭，只有明确登记设备并启用HTTPS部署后开启；固定64KiB请求上限、事务幂等和逐记录ACK。USB经显式串口选择复用相同合同和事务，不降低HTTP访问保护。
原始boot_id/单调时间与NTP或host_usb锚点分别保存，UTC未知时不能猜时间或借接收时间填充；主机时间来源及未知误差必须保留，不能将旧启动周期队列套上当前握手时钟。P2查询由P3复用，本机索引已启用，四环境指标、窗口质量与设备状态已接入；独立环境GET和自定义范围支持无Garmin观测的桌面记录。影子重建、容量限制与真实查询证据见[核验报告](verification.md)。

`environment-index`显式初始化并补齐索引，`environment-index --rebuild`显式强制重建，初次迁移及强制重建前要求备份恢复核验通过；`environment-index-verify`在影子副本重建和核对，不迁移真实库。索引启用后，环境摄取在同一事务内刷新派生数据，提交成功后才ACK。GET和普通看板启动不创建或迁移索引。备份恢复校验覆盖可选的`environment_index_schema`、`environment_index_boots`及`environment_time_index`派生表；事实表与索引分别校验，合成协议验收仅使用临时库，不向个人数据库注入测试记录。

固件采用2MiB应用分区、1.5MiB文件系统，构建门槛1.8MiB；活跃队列1MiB、拒收记录隔离上限64KiB，元数据单独校验。
固件仅在storage分区全空白时初始化文件系统；已有损坏文件系统不自动擦除，普通刷写不写NVS/队列。新购板首次安装通过独立first-flash流程，经明确交互确认后清除出厂内容并写入，详见[运行手册](operations.md#首次安装与后续升级)，不能作为常规升级或排障步骤。
隔离空间满时停止危险的上传/队列推进并保留待处理内容，不因此停止其他采样任务；实际介质损坏可能影响持久化，不能承诺零丢失。
完整分区、容量模型、掉电恢复与48小时目标见[硬件方案](plans/hardware.md)，实机压力结果与主机逻辑测试分开记录。


`.env`、`.secrets/`、`data/`、`logs/`保持私有且不进入版本库。模型URL和Key保存在本机与已授权云端的私有配置中，文档、源码、日志和公开状态不复制其内容；当前观测、审计和备份不因程序清理被删除。
新增路径或修改分层时，保持CLI命令、HTTP合同、数据库事实和用户启动入口兼容；必要变更应通过回归和数据完整性验证。

## 资源预算与部署验收

原设计预算20GB约18.6GiB，保留作为单用户容量对照；当前目标机已配置40GB磁盘，按实机用量和增长核算，不因估算预先升级。
[核验报告](verification.md)已有7天合成SQLite增长实测，环境库按该负载外推约**1.3GB/年**；这是原始表及原有索引/接收审计的参考，不包含P2派生UTC索引。
P2另有年度分钟索引实测，但不含全年声音事件和接收审计，不能把两个场景相加或互相替代为全年总量；Garmin、LLM、系统依赖、备份和其他增长仍须计入，也不是实机年度承诺。

硬件方案另按4KiB/分钟、至多1KiB事件/分钟、1KiB状态/5分钟及1KiB锚点/小时预留约2.8GB/年JSON量级，
它是更保守的预算模型，不是实测值，也不等于已经包含SQLite、Garmin、WAL及备份的总盘占用。
业务库不设未经验证的3GiB“充足保证”；年度预算与真实增长定期复核，保留完整观测与必要审计。

服务器不安装ESP-IDF、编译缓存或Node/Playwright；这些只在开发电脑/CI存在。部署先记录系统实际占用，预留至少25%磁盘，
再分配运行库、可恢复的本机备份和WAL临时空间；同机备份数量和总量必须有上限，大量历史备份转移到加密异机存储。
P6已实现磁盘水位保护、受控备份保留与可配置路径/SSH导出，云端11月18日04:00首次自然执行及备份核验通过；长期调度、实际离机目标与真实空间压力仍须验收，不能把首次成功等同长期自动运行已经通过，也不删除原始观测来假装符合预算。
运行7日和30日后复核全年预测。原20GB设计预算继续作为对照；当前目标机已提供40GB磁盘，应按实际系统、业务库、备份、WAL/日志与至少25%余量核算，不再把扩到40GB作为待执行事项。
2核4GB主要增加并发/备份内存余量，不解决硬盘不足，也不提高传感器准确度；目前没有证据要求先升级CPU或内存。

目标机需验证：暖查询P95小于1秒、手机首屏目标小于3秒、采集时可读、无OOM，并给内存和磁盘留下至少25%余量。
本机测试不替代2核2GB长期运行验收；配置、认证和恢复步骤见运行操作。

## 当前阶段与扩展依赖

Garmin采集、数据一致性核验、初步看板、访问控制和备份工具已实现；采购前固件、USB工具、环境接收与安全边界也已建立。
完整编译、固件大小、主机故障测试及真实C序列化到Python接收的证据统一见[核验报告](verification.md)。
USB实机短采已取得四通道读数，记录已迁入云端并可查；云端HTTPS部署与硬件真实Wi-Fi/TLS上传、入库查询也已分别核验，不能由USB证据替代联网实测。Mac与备用移动电源下短时四通道完整采样已验；原充电头供电路径异常根因、常驻电源、移动电源续航、准确度校准、长期掉电恢复和全年资源预算仍待验收。

P1—P6已实现。P4显式/日常检查保存审计，自动分析取用已通过资格核验的证据；夜晚GET只读当前资格和已保存报告，历史区分检查与发布。模型连通、数据合格、报告生成、云端生效与语言质量分别验收，最新证据统一见核验报告。

建设顺序和验收矩阵维护在[建设计划](plans/completion.md)：云端/IP HTTPS部署和硬件真实上传已完成，当前移动电源独立短采已通过，下一步确认常驻稳定电源并进行长期验收，离机目标另行配置核验；实机条件见[硬件方案](plans/hardware.md)。云端是持续运行主库，本机保留迁移时副本；本机USB新增不会自动同步，当前联网设备直接向云端上传。已建环境查询现由完整性检查复用，真实结论仍须完整性通过。

环境接入不改变Garmin原始记录的含义。相对声音指标不得标成校准dBA；阶段累计不得重建分期区间。
日任务将已填写的晨起睡感作为可选证据。咖啡因、酒精和其他未记录因素保持未知。分析门槛和阶段依赖见[数据契约](data-contract.md#睡眠分析合同)和[建设计划](plans/completion.md)。

当前日任务链为daily → collection → 释放采集锁 → readiness N日复查/保存，硬件接收独立运行。检查缺项不额外采集，失败不改写采集终态；同日跳过已完成采集仍可本地复查。检查表未初始化时明确返回not_initialized，不隐式建表。自动开关开启时继续处理合格的未发布夜晚，模型失败独立报告；本轮新链路部署见核验报告。

当夜与 7 日阶段各有独立取证、Prompt、输出校验、唯一发布及失败记录，复用同一模型适配器和日账本。报告状态和授权配置见[LLM 方案](plans/llm.md)，迁移和部署步骤见[运行手册](operations.md)。
