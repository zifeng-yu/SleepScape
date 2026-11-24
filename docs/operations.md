# 安装、运行与维护

更新：2026-11-23。已有配置与数据可沿用。云端为持续运行主库，配置值与运行证据分别按本手册和核验报告维护。
本文只维护操作步骤，实施状态与测试结果见[核验报告](verification.md)。

## 晨起睡感

从项目根目录显式执行 `uv run --locked sleepscape morning-feeling-prepare`。云端在生产工作目录以服务用户运行 `.venv/bin/sleepscape morning-feeling-prepare`。命令对新库先初始化基础表，再备份并临时恢复核验，最后建立独立记录表。已初始化时返回 already_initialized。普通启动、刷新及日任务不迁移。

打开夜晚详情，核对当前醒来日期，点击“较差”“一般”或“较好”。选中态表示已保存的值；点击另一档即可修改。通过原日期选择器翻到以前的日期即可补填。遇到读取或保存失败先刷新确认，排查服务认证、剩余空间和显式初始化结果。

## 本机安装与启动

所有命令从仓库根目录运行，相对配置路径均以启动目录为准。
新机器需要Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)和Python3.12：

```sh
uv python install 3.12
uv sync --locked
cp -n .env.example .env
uv run --locked sleepscape doctor
```

依赖隔离在`.venv`；`doctor`只检查环境及配置文件是否存在，不读取Key或验证认证。
静态看板随Python程序分发，生产运行不需要Node或前端构建服务。

```sh
uv run --locked sleepscape serve
```

打开`http://127.0.0.1:8000/sleep`；macOS也可双击根目录`启动看板.command`。启动窗口保持开启，按`Ctrl+C`停止。
页面刷新只查询本地数据库。空库不会自动联网、创建演示数据或把进程健康当作数据已采集。
默认无密码，仅允许本机直接访问；局域网或公网访问按部署步骤配置独立密码和HTTPS。

## 配置与凭据

模板为`.env.example`，实际配置为不提交的`.env`。环境变量可覆盖本地配置。

| 配置 | 默认/用途 |
| --- | --- |
| `GARMIN_PERSONAL_API_BASE_URL` | 中国区`https://personal.connectapi.garmin.cn`；不自动换区 |
| `SLEEPSCAPE_TIMEZONE` | `Asia/Shanghai`；来源时刻和本地日期的转换 |
| `SLEEPSCAPE_DASHBOARD_START_DATE` | 默认空、不裁剪；可设严格ISO睡眠归属日期，范围2000-01-01—2100-12-31，仅限制夜晚入口和历史展示 |
| `SLEEPSCAPE_GARMIN_KEY_FILE` | `.secrets/garmin-personal-api-key` |
| `SLEEPSCAPE_GARMIN_TOKEN_CACHE` | `.secrets/garmin-personal-api-bearer-token.json` |
| `SLEEPSCAPE_GARMIN_LOOKBACK_DAYS` | `7`；daily-run严格回查含上海今日的1—31个睡眠来源日，负荷背景另计 |
| `SLEEPSCAPE_DATA_DIR` | `./data` |
| `SLEEPSCAPE_HOST` / `SLEEPSCAPE_PORT` | `127.0.0.1` / `8000` |
| `SLEEPSCAPE_WEB_AUTH_FILE` | 默认不设置；部署使用`.secrets/dashboard-auth.json` |
| `SLEEPSCAPE_WEB_ALLOWED_HOSTS` | 默认本机域名/IP；远程部署填写实际公网IP或域名的JSON数组 |
| `SLEEPSCAPE_ENVIRONMENT_INGEST_ENABLED` | 默认false；实际HTTPS部署并登记设备后才改true，控制设备环境批次和声音事件片段的 HTTPS 上传 |
| `SLEEPSCAPE_DEBUG_API_DOCS` | 默认false；开启只提供受保护的`/openapi.json`，`/docs`始终关闭 |
| `SLEEPSCAPE_LLM_ENABLED` | 默认false；启用模型连接，开启本身不发送请求 |
| `SLEEPSCAPE_LLM_AUTOMATIC_ENABLED` | 默认false；daily-run报告调度开关，当前云端按用户授权启用；不改变GET只读性质 |
| `SLEEPSCAPE_LLM_REVIEW_ENABLED` | 默认false；在已启用自动逐夜分析的同一日任务中追加七日总结。涉及向指定模型发送白名单内的晨起睡感枚举 |
| `SLEEPSCAPE_LLM_MODEL_REF` / `SLEEPSCAPE_LLM_CONFIG_FILE` | `sleep_analyst` / `configs/models.toml`；非敏感模型身份与请求限制 |
| `SLEEPSCAPE_LLM_BASE_URL` / `SLEEPSCAPE_LLM_ENDPOINT` | 默认未设置；前者追加`/chat/completions`，后者为完整地址，两者互斥且只允许HTTPS |
| `SLEEPSCAPE_LLM_KEY_FILE` | `.secrets/sleep-llm-key`；Key仅存私有文件 |

项目Settings读取配置，再交给官方SDK；`SLEEPSCAPE_GARMIN_*`是本项目的路径配置。
需要首次创建或更新Key时，在本人浏览器打开[中国区Personal API Key页面](https://connect.garmin.cn/app/settings/personalApiToken)，
登录后按页面条款选择有效期，将仅显示一次的Key保存到上述私有文件。
不要把Key写入聊天、命令参数、日志、模板或版本库；程序自动换取并缓存token，无需手动复制token或提供账号密码。
保持私有目录700、Key/token和实际配置文件600。有效Key可换取新token；Key过期/撤销时更新本地文件。
手表须先与Garmin Connect同步，本程序不能代替手机同步。

### 看板睡眠日期展示范围

本机和云端私有`.env`均已设置`SLEEPSCAPE_DASHBOARD_START_DATE=2026-11-18`，云端新程序已安装并完成健康检查，夜晚入口和历史列表从该睡眠归属日起展示；生效核验见[核验报告](verification.md)。如需调整，修改该值后重新启动本机看板，云端只重启本项目`sleepscape.service`使Web配置生效；留空恢复全部历史可见。不需要重建索引、改固件、重复取数或重启其他站点。发布程序更新仍按下文的部署流程进行。

核对受保护的`/api/v1/status`中`dashboard_start_date`及页面日期下界。没有允许日期的睡眠时，应显示等待/缺少数据，不能回退到旧夜晚；旧日详情API返回404。18日睡眠仍可显示17日晚真实环境，昨日运动及30日基线继续读取原数据，独立环境自定义窗口仍可调试。该配置只改变展示范围，不删除任何记录；具体合同见[数据契约](data-contract.md#看板睡眠日期展示边界)。

## 每日采集编排

P5已实现，以下命令从项目根目录执行：

```sh
uv run --locked sleepscape daily-run --plan-only
uv run --locked sleepscape daily-status
# 显式执行今日采集，会访问已配置的Garmin官方API：
uv run --locked sleepscape daily-run
# 仅在当日未完成、原因已处理并满足Retry-After后显式重试：
uv run --locked sleepscape daily-run --retry
```

前两条只读/离线，不读取凭据正文、不联网、不建库或写运行状态。`--plan-only`和`--retry`互斥。计划固定使用Asia/Shanghai日期，与09:00模板一致；查看计划不代表定时器已经安装。修改N在私有`.env`设置`SLEEPSCAPE_GARMIN_LOOKBACK_DAYS=7`，支持1—31。详细范围含当天共N日，停机不会扩大；负荷背景为截至昨日N+30个已结束日。N=1不含昨日，若需次日补晚同步数据应保留默认7或至少2。

一次显式执行处理当前窗口，将结果和逐日睡眠观测缺口提供给看板。HTTP成功但数据为空不立即重试；次日窗口内补取后看板读取新事实。同日终态默认跳过；`--retry`只对partial/failed/interrupted/deferred发起新尝试，completed即使有缺口也不重复。修改N不会绕过同日幂等规则；需临时补范围时使用下文显式日期补采，或等次日日任务。成功及跳过已完成轮次退出0，未完成、延后或被锁阻止退出非零，报告用例另按自动开关执行，采集退出状态不被模型失败改写。

`data/daily-runtime.json`保存至多31个任务日的最新尝试、计数与固定状态，最大128KiB；更早的Garmin原始数据和SQLite运行审计保留。`data/collection-cooldown.json`保存跨手动/每日采集的Retry-After下限。两者均为私有原子写文件；不含健康测量、响应正文或凭据。任务与手动采集共用`.collection.lock`，不阻塞环境独立摄取。进程退出后文件锁由系统释放，遗留running通过SQLite运行结果判定已完成或中断，不以删锁/删账本作为排障方式。

状态损坏、权限不安全或时钟回退会明确阻止执行；保留文件和原数据库，先核对原因。data目录须700，账本及锁文件须600，不接受符号链接。只读状态不会自动修复；若真实进程已退出，页面可只读推断interrupted，下一次显式执行才持久化恢复。业务执行状态与睡眠观测是否存在分开；available不表示双源数据齐全。部署前无需为测试运行真实采集，更不能向真实库灌入合成数据。

## 双源完整性检查

完整性检查是独立的零模型调用入口；报告生成另见下一节。新库首次使用显式保存检查前，先运行：

```sh
uv run --locked sleepscape analysis-prepare
# 检查并保存配置N日（包含上海今日，默认7日）：
uv run --locked sleepscape analysis-check
# 指定睡眠来源日，仅检查既有事实，不补采Garmin：
uv run --locked sleepscape analysis-check --date YYYY-MM-DD
# 不建表、不保存，也不联网或调用模型：
uv run --locked sleepscape analysis-check --date YYYY-MM-DD --read-only
```

analysis-prepare先在线备份并在临时目录恢复核验，再显式迁移可选三张检查表；已有完整合法表时幂等返回。失败须保留备份和审计、检查原因，不删除表或旧检查“重置”。本轮目标机是否已执行只以核验报告为准，复制程序不等于迁移已完成。

analysis-check不访问Garmin、不向硬件拉取、不读模型Key、不调用模型。默认保存当前配置N日检查，--date允许显式检查窗口外日期；过窗状态仍显示expired_unanalyzed并保留实际core_ready，不自动扩窗或生成报告。--read-only不需新表；普通保存缺表明确报analysis_not_initialized，空间不足拒绝写入。输出含缺项和时间范围，属于私有数据，不提交Git或复制到公开日志。

daily-run在采集步骤返回并释放采集锁后复查同一N日，已完成采集的同日跳过也可复查本地新增硬件数据；锁阻止/未结束的采集不并行启动该步骤。检查失败单列readiness状态，不清空观测、不改写采集结果、不额外重抓。随后按开关依次运行逐夜与 7 日报告用例，输出独立的 `reports` 和 `review` 阶段结果。两阶段沿用同一模型和共享预算，采集终态按采集结果记录。

夜晚详情GET实时只读计算当前检查；历史显示最后保存的结果/时间，二者可能不同。补传后详情刷新可看到新覆盖，历史须下次日任务或显式analysis-check保存后更新。ready仅表示满足当前数据门槛，quality_state独立说明完整/有缺口/不足，不是医学准确或已有报告。字段、计数与未知边界详见[数据契约](data-contract.md#当前检查结果与存储)。

## 睡眠报告生成

新机器或原库尚无报告表时，先确认现有事实/索引可读，然后从项目目录执行：

```sh
uv run --locked sleepscape sleep-analysis-prepare
# 显式发送最小白名单健康证据到已配置模型，日期必须在配置N日内：
uv run --locked sleepscape sleep-analyze --date YYYY-MM-DD --live
# 不指定日期时：优先今日，其次窗口内最早的合格未发布夜晚：
uv run --locked sleepscape sleep-analyze --live
```

已发布夜晚使用新 Prompt 重新解读时，显式执行 `sleepscape sleep-analyze --date YYYY-MM-DD --live --revise`。使用生产工作目录和服务账号。先备份及临时恢复核验，每次最多一次模型请求；失败仍显示原报告，成功后详情和历史入口自动读取新报告。相同 Prompt 已发布时零调用。自动任务继续跳过已有报告。

sleep-analysis-prepare先创建SQLite备份并临时恢复核验，再显式创建独立报告四表；已有合法表幂等返回。与analysis-prepare的检查三表不同，二者都不调用模型。缺表、半套表或版本错误须按安全错误排查，不能删表/证据重置。备份包含已存在的可选表并验证载荷与引用；迁移是否在目标机执行，以核验报告为准。

sleep-analyze没有--live拒绝执行，不重新采集Garmin、不控制硬件，不自动迁移。只有窗口内ready且未有成功报告才进入证据/模型阶段；重复执行已有报告零请求。输出为固定状态/调用次数，完整输入、报告和尝试在私有SQLite保留。模型文字只解释方向，数值由看板证据卡展示；禁止把这些私有内容提交Git或复制到公共日志。

自动路径复用同一个服务：在私有.env设置SLEEPSCAPE_LLM_ENABLED=true和SLEEPSCAPE_LLM_AUTOMATIC_ENABLED=true，并保留已配置的URL/Key路径。自动开关默认false；当前云端已获用户明确授权按现配置启用，生效以核验报告为准。云端更新后仅重启sleepscape.service使Web状态读取新配置，并安装最新garmin-sync.service模板、systemctl daemon-reload；该模板TimeoutStartSec=2h，容纳最多30次各180秒的模型调用及原采集、有限等待，不立即重跑已完成Garmin。不要另建重复09:00任务。

所有合成/真实请求共享上海自然日30次持久额度；每个night_date跨输入、跨日累计最多10次，成功发布立即停止。历史失败、中断和其他已记录尝试继续计数，换输入或换日不清零；取消每天最多两个夜晚和同输入每日两次的旧限制。网络或结构纠错可在夜晚剩余次数及每日总额度内继续；遵守Retry-After，超过30秒不在本轮等待，后续执行仍须尊重未过冷却。配置/鉴权/模型能力错误先修正，不能删model-runtime或尝试审计绕过限制。夜晚累计10次后仍未成功则为blocked_attempts，没有强制解除CLI。若任务中断，保留占位及尝试，由后续受控执行标记中断并重新检查。

发布前及发布事务内会重读来源/资格/配置；发生变化不展示旧候选。发布后源事实变化只显示source_changed，既有报告仍对应生成时证据且不自动修改；未知当前来源状态明确提示无法比较。页面没有生成按钮，刷新不花费请求额度。结构失败以固定output_schema_*错误码区分唯一项、必需包含、缺字段、固定值、类型及其他规则；业务校验失败为output_business_mismatch。只排查安全码及源数据状态，不要求保存或公开失败响应/校验值。一次失败用尽同输入剩余尝试时仍返回failed和原错误码，整轮partial、CLI非零；后续预检blocked_attempts表示没有新的调用，不是报告成功。模型失败不回滚采集事实；09:00 自然发布已核验，后续复查新版正文和长期稳定性。

## 7 日复盘总结

首次安装默认关闭复盘阶段。按项目 [Linux 发布流程](#linux部署与公网ip-https)核对安装包，在目标环境的工作目录，以 SleepScape 服务账号运行一次 `sleepscape sleep-review-prepare`。该命令先验证备份及临时恢复，再显式初始化或升级复盘表，重复运行返回当前初始化状态。已有复盘库需要完成升级后才能执行自动重试，旧报告和审计继续可读。

获得本用途及晨起睡感白名单的明确授权后，在目标机私有配置设置 `SLEEPSCAPE_LLM_REVIEW_ENABLED=true`，保留既有模型 URL、Key 及自动分析配置。按发布流程使 Web 读取新配置，等待原 09:00 定时器自然执行，核对逐夜与 `review` 两段状态及历史页。当日已执行的采集无需为上线重跑。回退时关掉复盘开关，并保留兼容的新表备份校验、报告和执行记录。实际启用及自然运行见[核验报告](verification.md#7-日复盘总结2026-11-20)。

## 模型配置与连接核验

本机已经保存用户提供的模型地址和Key，并完成合成连接核验；无需重新粘贴。新机器或轮换时，将地址写入私有`.env`，Key保存到`SLEEPSCAPE_LLM_KEY_FILE`指定的普通文件（目录700、文件600，不允许符号链接）。不要把Key写入环境模板、终端参数或TOML。非敏感模型配置只维护`configs/models.toml`；安装包内的默认配置在构建时由该文件生成，无需维护第二份。 当前该文件显式配置response_mode="json_object"，同模型与私有URL/Key不变；schema_profile="aliyun-compatible-1"保留供可配置的json_schema模式使用。协议仅由显式配置选择，运行时不自动切换。JSON对象模式只保证JSON语法，system仍动态提供完整规范Schema，返回和发布仍需完整本地Schema与业务校验；不得删除规范约束。省略response_mode时默认json_schema，部署须包含当前TOML。实际生效与真实结果见核验报告。

```sh
uv run --locked sleepscape model-status
uv run --locked sleepscape model-check --live
```

`model-status`和`doctor`只读取非敏感配置及已保存的安全状态，对密钥仅检查文件元信息，不读内容、不联网、不建库。`model-check`没有`--live`会拒绝执行；有该参数才发送一次合成探针，核对返回模型、结构、固定值及usage，不读取或发送健康库。修改地址、模型参数或替换Key后，旧核验显示`configuration_changed`，需显式重新核验。

单次最长180秒，总输出预算不超过4096 Token（合成探针512），响应不超过256KiB。每上海自然日共享30次物理请求，先记额度再调用，失败和中断也计入；独立模型锁防止多个进程重复调用，适配器内部不重试。模型账本保留最近30个上海日期（含当天），最多1024条、1MiB；日任务等其他运行元数据仍按各自128KiB边界处理，不能因模型账本扩容放宽全部文件。网络故障可在剩余额度内稍后显式重试，429须等待Retry-After。`model-check`成功证明这次合成连接通过，不代表存在可分析的完整夜晚。model-status的last_check_*沿用共享运行账本的最近模型请求，可能是合成探针或真实报告请求；connection_state=verified表示当前配置最近请求成功，不是独立的合成验收凭证，也不保证业务报告已发布。

状态保存于私有`data/model-runtime.json`，只含配置指纹、时间、错误码、耗时及Token计数，不存Key、URL、Prompt或响应正文。下次显式调用时保留最近30个上海日期的元数据，最多1024条/1MiB；GET不会清理或修复它。出现`model_state_unavailable`应检查本机权限或状态损坏，保留证据，不删除文件重置调用额度。运行元数据不在单独SQLite文件内，但已纳入成套备份；迁移运行环境时应私下连同该文件和配置迁移，以免当日次数丢失。

看板“模型配置与请求记录”中的“本晚报告”依据已保存并通过校验的报告；“最近请求状态（共享）”与“该次请求结果”对应共享账本的最后一次请求，可能来自探针或其他夜晚。两者独立：报告已生成时，即使最近请求失败，也不会撤销报告或将本晚改成未发布。全局数据状态页没有特定夜晚上下文，报告是否成功请查看相应夜晚详情；不要为消除失败提示而清零或改写调用账本。

看板详情已接入双源完整性与分析资格，显示当前阻塞项、非阻塞质量提示、有效采样覆盖、完整分钟覆盖、可选缺项和检查时间。默认显示当前质量结论，有阻塞时列出项数与主要指标。点击“查看完整性检查详情”可查看全部阻塞项与通道覆盖；其中“数据缺口与质量提示”可独立展开，聚焦标题后按Enter或空格也可操作。历史读取上次保存检查，模型配置状态单独显示。已发布报告和生成时证据另列，来源后来改变仅提示；页面刷新或切换日期不调用模型、不保存检查。

用户已确认现有Token Plan团队版私有配置用于本机测试与云端每日09:00分析，按该配置执行；供应商公开范围在[模型方案](plans/models.md)客观记录，不根据套餐标签静默替换模型或阻止已授权用例。

## 环境时间索引与只读查询

P2/P3已完成，本机索引已显式启用并核验原始事实未变。新机器或从未启用索引的库，先核验已有事实可稳定映射，再显式启用；当前库无需重复强制重建：

```sh
uv run --locked sleepscape environment-index-verify
uv run --locked sleepscape environment-index
# 使用当前算法强制重建派生索引时：
uv run --locked sleepscape environment-index --rebuild
```

`environment-index-verify`只读源库，在私有临时副本两次重建并比较事实哈希和映射结果，最后删除副本，更新私有`data/reports/environment-index-verification.json`；不迁移源库。失败返回非零退出码及固定原因。

`environment-index`首次运行先创建并核验备份，再显式建立可选派生表和回填；已启用时只补更新，无需重复全量备份。`--rebuild`也先验证备份，再强制重建。Garmin/环境原始事实、单调时间和锚点保持不变。启用后新环境记录及锚点在接收事务内更新索引，提交后才ACK；损坏或未完成的索引不会被GET悄悄修复。

`EnvironmentViewService.timeline(start,end,limit=10000)`提供带时区的至多48小时查询，由统一时间轴及`GET /api/v1/environment/timeline`复用。未映射、冲突和待更新时刻只显示状态，不补0或猜UTC。长时间不重启的单boot若映射发生冲突，整boot重建可能短时持有写事务，量级与限制见核验报告。

查看已采集环境：打开看板“数据状态”，从最近环境采集窗口进入；也可在夜晚页选择“自定义区间”，输入实际采集起止。默认夜晚回退为前日18:00至当日12:00，下午桌面试采需要自定义窗口，即使没有当天Garmin记录也可查看。默认显示温度和声音均值，在图例开启湿度、光照或声音峰值；把光标移到分钟窗口可看min/max、有效/预期数量、估计时间、声音采样和事件检测覆盖。

详情较长时点选图表固定提示，再在提示框内上下滚动；按Esc或“取消固定”关闭。手机也可点选固定查看，普通页面滚动和图表缩放仍独立工作。

夜晚页顶部“来源与口径”逐卡独立展开；有效值不因当日日期要求人工确认。图表每轨标有指标、单位和数值刻度，上下横轴带日期时间；同指标跨启动周期只显示一个图例开关，原始数据仍分段。底部“这晚的关联记录”可筛选运动、午睡、声音摘要，并列出各类条数和所选时间范围；完整每分钟环境数据在上方曲线，声音摘要不是觉醒次数。需要查看摘要发生位置时点击“定位到时间轴”，不会重新采集或调用模型。

数据状态中的最后接收、最后已映射采集和缓存快照是不同证据，不能据近期接收断言设备仍在线。声音仅为相对dBFS；调试和卧室联网记录按各自档案展示，短采不代表整夜覆盖，通道通信缺测按原质量标记保留。页面读取失败可重试，不会调用模型、启动采集或自动改库。

可重现的合成年度容量检查会自动删除临时大库，不读取真实健康数据：

```sh
uv run --locked python tools/verify_environment_capacity.py
uv run --locked python tools/verify_environment_capacity.py --single-boot
```

## 日常同步与历史补采

日常使用：

```sh
uv run --locked sleepscape garmin-sync --plan-only
uv run --locked sleepscape garmin-sync
```

第一条只读运行元数据预览计划，不读Key、不联网；第二条才联网。
今日睡眠等有效数据直接展示，不因日期是今天标“待确认”；缺失、无效和失败旧值分别说明，全天统计只取到昨日。首次背景38日，日常7日，每隔7日宽回查38日；
详细默认7日，可加`--history-days 38`显式扩大背景。旧历史保留，看板和核验均回读所需30日基线。

指定历史窗口：

```sh
uv run --locked sleepscape garmin-plan --end YYYY-MM-DD --detail-days 7 --history-days 38
uv run --locked sleepscape garmin-collect --end YYYY-MM-DD --detail-days 7 --history-days 38
```

将日期替换为所需截止日，必须早于配置时区今天；不写`--end`则截止昨日。
详细窗口1—31日，背景至少等于详细天数、最多366日；首次验证7晚完整负荷基线建议背景38日。
请求分块、分页和采集覆盖策略见[Garmin方案](plans/garmin.md)；审计仍保留完整日截止及暂定资源字段，不要通过连续重启绕过采集锁或限流等待。

超过30个已结束日未成功同步时，`garmin-sync`返回`backfill_required`及准确分块命令并停止联网。
按输出逐块采集成功后再sync；未执行或失败的块不算补齐。重复采集同一窗口可取得晚到/修订，当前记录按来源分区更新，审计保留。

## 离线核验与故障处理

```sh
uv run --locked sleepscape garmin-verify
uv run --locked sleepscape garmin-audit
```

`verify`检查最近采集窗口与所需存量负荷历史，`audit`只读源库并用私有临时库重放两遍，结束清理临时副本。
这些是正式数据验收命令，不连接账号，不用真实库注入合成修订；其输出不是手表准确度证明。
报告覆盖当前`data/reports/garmin-verification.json`和`garmin-acceptance.json`，含个人数据的报告保持私有。
`audit`使用24小时新鲜度阈值；过期不等于数据库损坏，应先判断是否需要同步。

| 现象 | 处理 |
| --- | --- |
| 页面没有今天的记录 | 先同步手表到Connect，再执行garmin-sync；不要用旧夜晚替代今天 |
| token到期 | 适配器用有效Key换新，无需手工复制token |
| 认证持续拒绝 | 按错误码检查区域、Key有效性和文件权限，不反复登录或换区 |
| 限流/网络/服务故障 | 遵守最早重试时间，等待后再运行；失败保留旧值且标陈旧 |
| HTTP成功但字段为空 | 检查资源状态和缺失原因；空值、有效空列表、零及失败各自处理 |
| 核验非零退出 | 查看安全错误码及本地私有报告，不能只凭HTTP200或旧报告判断成功 |

## 看板密码

在私有`.env`设置`SLEEPSCAPE_WEB_AUTH_FILE=.secrets/dashboard-auth.json`后运行：

```sh
uv run --locked sleepscape web-password
```

交互输入用户名和至少12字符密码并确认，程序仅保存加盐PBKDF2哈希。重启Web后在`/sleep`使用HTTP Basic首次认证；HTTPS下的后续持久登录见下节。
看板密码与Garmin Key独立。远程健康数据访问必须HTTPS；设置密码不意味着可以公开明文8000端口。
不要将密码写入shell参数。

### 看板入口与持久登录

正式入口为`https://39.107.47.198/sleep`（`/sleep/`也可用），根入口已取消，访问`/`返回404，不跳转、不弹出登录。静态资源、只读API和设备上传路径不变。首次使用现有Basic账号密码登录，HTTPS响应设置有效期30天的安全Cookie（属性见[数据契约](data-contract.md)）；之后同一浏览器可凭Cookie访问。有效期从签发起固定计算，不随浏览自动延长，服务重启仍有效。更换看板密码记录立即使旧Cookie失效；清除站点Cookie也会结束Cookie登录，浏览器自行缓存的Basic凭据由浏览器管理。隐私浏览或主动清除站点数据无法保留30天。

## 本地备份与恢复检查

```sh
uv run --locked sleepscape db-backup
uv run --locked sleepscape db-restore-check "backups/实际备份文件名.sqlite3"
```

第二条使用第一条输出的`backup_path`，相对路径以data目录为基准。
备份用SQLite Online Backup API包含已提交WAL，经完整性、外键、raw哈希/JSON、环境表、已存在的晨起睡感、完整性检查、逐夜报告及修订、7 日复盘和失败归档的结构、摘要和引用验证后保存为新 UUID 文件，不覆盖旧备份。旧有效备份没有检查表或报告表也可核验；任一可选组半套表、错误版本、损坏载荷/引用或发布矛盾均拒绝。
恢复检查只在私有临时目录恢复、核对数量和完整性，结束清理，不覆盖运行库；当前未提供自动切换生产库命令。
成功后更新私有`backup-status.json`；状态页表示最近本地验证成功，不保证文件永久存在、已离机或已加密。
上述手动命令仍只处理单库备份，不自动清理它们。P6成套维护、导出与保留见下节；云端定时器安装按Linux部署步骤执行，当前验收状态见[核验报告](verification.md)。真实离机目标尚未配置。

## 自动维护与离机配置

P6程序已完成。本机成套备份及恢复核验的安全摘要位于私有`data/reports/maintenance-verification.json`；云端安装、迁移和运行证据另见[核验报告](verification.md)。手动备份成功、定时器已启用和到点自动执行是不同证据；真实离机目标未配置时保持导出disabled。

```sh
uv run --locked sleepscape maintenance-run --plan-only
uv run --locked sleepscape maintenance-status
# 显式创建、核验、导出（若已配置）及按策略保留：
uv run --locked sleepscape maintenance-run
# 失败原因处理后重试当日未完成任务：
uv run --locked sleepscape maintenance-run --retry
# 使用成功输出中的backup_id：
uv run --locked sleepscape maintenance-restore-check 实际备份ID
```

status/plan-only只读，不建目录、不备份、不清理、不联网、不哈希整库。同日completed会跳过；失败须显式retry，重试可复用已验证的同一时间点备份，不能把重试时间当作快照时间。CLI成功为0，失败/partial/blocked为非零，固定错误码不含私有路径。恢复检查在数据盘的临时目录执行，完成后清除临时副本；不覆盖生产库。

| 私有.env配置 | 默认与用途 |
| --- | --- |
| `SLEEPSCAPE_MAINTENANCE_KEEP_COUNT` / `MAX_BYTES` | 3份 / 4294967296字节（4GiB）；仅受控成套包。表中缩写均保留相同SLEEPSCAPE_MAINTENANCE_前缀 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_MODE` | disabled，另支持path、ssh；未配置不发外部请求 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_PATH` | path为已挂载的私有绝对目录；ssh为远端已存在的私有绝对目录 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_HOST` / `EXPORT_USER` / `EXPORT_PORT` | SSH主机、用户、端口（默认22），仅私有配置 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_IDENTITY_FILE` / `EXPORT_KNOWN_HOSTS_FILE` | 专用SSH私钥与已核验主机密钥文件，均600且不可为符号链接 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_TIMEOUT_SECONDS` | 300秒总传输时限，允许5—1800秒 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_MAX_BYTES` | 17179869184字节（16GiB）目标保留预算 |
| `SLEEPSCAPE_MAINTENANCE_EXPORT_DAILY_KEEP` / `EXPORT_WEEKLY_KEEP` | 最近7个有备份的日期、4个ISO周的最新包，同包去重；空间不足仍保留最后有效包并提示 |

完整变量名及无敏感值示例维护在[.env.example](../.env.example)。目标设置改变后旧的导出核验失效；重新运行才更新结果。

**路径目标**：先由用户确认挂载成功、目标为专用目录且权限700，再配置path。程序不替你挂载、不创建缺失目标根目录，不把“同机目录可写”当作已离机或静态加密。若需要离机保障，应使用实际独立设备/远端挂载，并在部署时实查；目录已存在也不能证明挂载仍在。为防挂载丢失，建议将私有目标子目录放在挂载介质内，卸载后不留下同名可写目录。

**SSH目标**：远端需OpenSSH服务与Python3.8+，独立备份用户、权限700的专用目录；私钥与known_hosts保存在`.secrets/`并保持600。通过可信渠道核对主机密钥，不能用关闭StrictHostKeyChecking解决连接错误。默认不交互询问密码，不转发代理；本轮未生成或上传真实密钥。程序仅确认加密传输和逐文件摘要，目标磁盘静态加密需另行配置。SSH选项语义参考[OpenSSH官方说明](https://man.openbsd.org/ssh_config)。

本地`data/maintenance-state.json`保存最多31条运行摘要及256个受控包登记（256KiB上限），锁为`.maintenance.lock`；每轮旧手动文件盘点最多256项，超限停止并要求整理，不继续盲扫。备份保存在`data/backups/managed/<ID>/`。删除仅针对已登记的旧包，旧手动SQLite副本、未知文件、凭据及原始事实不自动删除。状态损坏/路径不安全时保留文件排障，禁止删清单重置状态。

**恢复边界**：先停相关写入进程，从同一个已验证包取SQLite和三份存在的运行账本，按包清单确认缺省文件；维护清单不在单个包里，迁移整环境时与整个managed目录配套保存，配置/凭据另行迁移。不要用更旧备份覆盖仍可用的新账本来减少当日模型次数或缩短冷却；无法确认恢复当天已用模型额度时，当天保持模型禁用，次日再启用。当前没有自动覆盖生产库的恢复命令。定期恢复演练在临时副本执行，真实灾难恢复需核对备份时间点后人工操作。

磁盘水位正常/预警/限制/临界及故障行为见[维护方案](plans/maintenance.md#容量规则与写入保护)。预检不能排除并发耗尽或配额变化；设备收到503及Retry-After时不删未确认队列。P6不会清理业务事实、手工WAL或自动VACUUM。

首次安装或更新维护服务时，在Linux服务器的`/opt/sleepscape`执行；已有配置不得因重复安装而重置：

```sh
sudo install -m 644 deploy/maintenance.service /etc/systemd/system/maintenance.service
sudo install -m 644 deploy/maintenance.timer /etc/systemd/system/maintenance.timer
sudo systemd-analyze verify /etc/systemd/system/maintenance.service /etc/systemd/system/maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now maintenance.timer
sudo systemctl list-timers maintenance.timer garmin-sync.timer
```

04:00与09:00采用不同服务，Persistent只补一次错过的触发，不逐天回放。使用`systemctl list-timers`核对实际安装和下次触发；看板`installation_state=unknown`是跨平台只读接口的固定边界，不表示服务器未安装定时器。迁入的当日completed账本会使首次手动启动合法跳过，不能清账本或改日期强迫重复备份。11月18日04:00维护及备份核验、09:00采集首次自然执行分别通过，已有独立的服务及账本证据；停机唤起及超时恢复另验，范围见[核验报告](verification.md)。参数含义参见[systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)。path导出必须在maintenance.service的单独override中添加确切目标的ReadWritePaths，不能把整个文件系统设为可写；SSH只读取本地密钥，网络发到显式目标。

[日志限额示例](../deploy/journald-sleepscape.conf.example)为专用服务器提供256MiB持久日志、64MiB运行日志、2GiB保留空间及14日目标。该配置影响宿主全部journald服务，安装前须核对已有策略；它尚未应用到本机或云端。journald只会按其规则清理日志，并不替代应用数据盘保护，行为参考[官方说明](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)。Caddy保持不启用访问日志，程序错误不带健康正文；不清除库内来源审计。

## Linux部署与公网IP HTTPS

当前部署布局为Ubuntu22.04上的独立`/opt/sleepscape`目录和`sleepscape`服务用户；Caddy单独监听80/443，应用只监听127.0.0.1:8000。已有Nginx的5688看板保留，不改其配置、不重启其服务。下列是当前安装和更新步骤；实际签发、迁移、服务启动及外网验收结果只维护在[核验报告](verification.md)。本轮P4报告上线与真实运行结果同样见核验报告。

**目录与生产依赖。** 先核对80、443、8000未被其他服务占用、系统时钟已同步，再准备独立用户和目录。源码及`uv.lock`通过已核验的发布文件传入；服务器不安装Node、浏览器或ESP-IDF。uv位于root管理的`/opt/sleepscape-tools/uv/bin/uv`，Python安装在`/opt/sleepscape-tools/python`，避免venv解释器指向服务用户无法遍历的`/root/`。当前工具与安装来源如下，具体校验证据见核验报告：

| 内容 | 当前生产安装 |
| --- | --- |
| Python | 3.12.10；官方python-build-standalone 20250517发布包与SHA256SUMS核对后传输，再核对服务器完整文件SHA-256；解释器目录`/opt/sleepscape-tools/python/cpython-3.12.10-linux-x86_64-gnu` |
| uv | 0.7.6；镜像wheel中的ELF与官方GitHub发布包内ELF的SHA-256一致，安装到上述专用工具目录 |
| Python依赖 | 本机从唯一`uv.lock`导出带全部哈希的临时requirements，经SSH传入；服务器从阿里云PyPI镜像下载并强制`--require-hashes`核验 |
| SleepScape程序 | 本机构建release wheel，服务器核对传输前后SHA-256后`--no-deps`安装；当前为非editable安装，修改服务器源码不会自动改变运行包 |

首次创建用户前检查是否已存在，不替换同名已有用户：

```sh
getent passwd sleepscape || sudo useradd --system --user-group --home-dir /opt/sleepscape --shell /usr/sbin/nologin sleepscape
sudo install -d -m 755 /opt/sleepscape /opt/sleepscape-tools/python
sudo install -d -o sleepscape -g sleepscape -m 700 /opt/sleepscape/data /opt/sleepscape/.secrets
cd /opt/sleepscape
```

新机器按下述发布流程创建venv并安装。工具和解释器保持可读、可执行，代码/工具不需要授予服务用户写权限。生产命令直接使用`.venv/bin/sleepscape`，不会在定时任务中重新下载依赖。

重建当前发布方式时，在开发机用`uv export --locked --no-dev --no-emit-project --output-file 临时requirements路径`导出依赖、用`uv build --wheel`构建程序。导出默认保留全部锁定哈希，不加`--no-hashes`。将临时依赖文件、wheel及其SHA-256清单私下传入服务器，核对摘要，再在服务器执行。以下子进程的`umask 022`仅用于公开程序和依赖安装：

```sh
cd /opt/sleepscape
sudo sh -s -- '临时requirements路径' '已核验的wheel路径' <<'SH'
set -eu
umask 022
cd /opt/sleepscape
/opt/sleepscape-tools/uv/bin/uv venv --python /opt/sleepscape-tools/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12 .venv
/opt/sleepscape-tools/uv/bin/uv pip install --python .venv/bin/python --require-hashes --index-url https://mirrors.aliyun.com/pypi/simple -r "$1"
/opt/sleepscape-tools/uv/bin/uv pip install --python .venv/bin/python --no-deps "$2"
SH
sudo chown -R root:root .venv
sudo chmod -R a+rX .venv
sudo -u sleepscape .venv/bin/python --version
```

将占位路径替换为本次发布的实际文件；已有venv升级时不重复创建或清空环境。镜像只替代下载位置，`uv.lock`仍是依赖唯一来源，不能手工改导出文件绕过哈希。安装核验后清理临时requirements、下载包和构建产物，不提交第二套依赖清单；源码、锁文件和现有私有数据保留。

仅更新应用wheel时，同样在专用子进程限定安装掩码；项目版本号未变化时显式重新安装，不能让安装器沿用旧程序：

```sh
sudo sh -s -- '已核验的wheel绝对路径' <<'SH'
set -eu
umask 022
cd /opt/sleepscape
exec /opt/sleepscape-tools/uv/bin/uv pip install --python .venv/bin/python --no-deps --reinstall "$1"
SH
```

安装后必须从生产工作目录，以实际服务用户核验模块可导入及应用可创建；只用root执行成功不能证明服务可启动：

```sh
cd /opt/sleepscape
sudo -u sleepscape env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'from sleepscape.app import create_app; create_app(); print("application preflight passed")'
```

预检成功后才重启`sleepscape.service`并检查本机健康端点、受保护API和公网HTTPS。预检失败时先修复公开包权限或回退至已核验程序，不让服务反复失败。此前一次发布因root的`umask 077`生成仅root可读的包目录/文件导致服务用户无法导入，已回退修复；本节流程用于避免复发，具体证据见核验报告。

同步源码和文档时，以开发机 Git 跟踪文件为清单，在临时目录准备待发布文件。先比对云端差异，确认来自已知历史版本，再按清单更新。分别核对生产工作目录内的文件与可执行位、服务账号实际导入的包内全部资源及构建时加入的模型默认配置。仅更新 wheel 不会同步工作目录。私有配置、数据库、报告、录音与备份沿用各自实例的内容。

权限调整只限公开程序、解释器和依赖目录，不对`/opt/sleepscape`整体执行递归放宽权限。`.env`、凭据及实际私有文件继续600，私有目录700；运行服务的`UMask=0077`保持不变，不因安装使用022而降低数据保护。

后续也可在本项目完整源码目录使用常规`uv sync --locked --no-dev --no-editable --python /opt/sleepscape-tools/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12`进行部署更新，使用上述专用uv的完整路径；安装子进程同样限定`umask 022`并完成服务用户预检。它会按锁文件同步环境并重新安装本项目，须作为显式发布执行；保留`--no-editable`才能维持当前生产安装方式。不得省略`--locked`偷偷更新依赖，也不要把sync放入服务启动或定时任务。更新前先备份并停止本项目写入和Web服务，更新后重新验证再启动；不得停止其他站点或覆盖其目录。

**私有配置与一致迁移。** `.env`和`.secrets/`只经SSH私传，所有者为`sleepscape`、文件600、私有目录700；不经Git。`.env`配置中国区Key、`SLEEPSCAPE_WEB_AUTH_FILE=.secrets/dashboard-auth.json`、实际公网IP白名单以及`SLEEPSCAPE_GARMIN_LOOKBACK_DAYS=7`；调试文档保持关闭。白名单可用`SLEEPSCAPE_WEB_ALLOWED_HOSTS='["198.51.100.10"]'`的格式，必须把文档保留地址换成真实IP，不加通配符。既有模型配置和预算账本保留；普通部署和GET不调用模型。按用户要求配置SLEEPSCAPE_LLM_ENABLED=true与SLEEPSCAPE_LLM_AUTOMATIC_ENABLED=true，完成报告显式迁移后09:00日任务才进入受预算控制的报告用例。未提供离机目标时保持`SLEEPSCAPE_MAINTENANCE_EXPORT_MODE=disabled`。

迁移按以下次序执行，不用文件名或mtime猜测副本是否一致：

1. 暂停源端采集、USB摄取及模型写入，取得或选择已通过恢复核验的P6成套快照。复用已有包时，确认其全库逻辑内容及已存在的三份运行账本仍与源端一致；若不一致，重新取得一致快照，不能把旧库与新账本混配。
2. 私下传输整个data目录，包括维护清单、managed备份目录、旧手动备份和报告；在**传输副本**中排除活动SQLite主文件、WAL/SHM及运行锁，用已验证包的SQLite及配套`model-runtime.json`、`daily-runtime.json`、`collection-cooldown.json`恢复副本，缺省文件按包清单处理。不得删除源端WAL或改写源数据来完成迁移。
3. 维护清单不在单个包中，必须与全部managed目录配套迁移；检查清单登记、包校验、全库逻辑摘要和账本字节摘要。锁是进程状态，不迁移持锁状态，不清账本绕过同日幂等、模型预算或Retry-After。
4. 目标机保持业务服务未启动，用服务用户执行`maintenance-restore-check 实际备份ID`和`environment-index-verify`，只在临时副本验证。既有索引随库迁入，不盲目重建；全新空库才按环境索引章节显式初始化。
5. 核对权限与安全配置，执行`doctor`、`daily-run --plan-only`、`daily-status`、`maintenance-status`；这些检查不调用真实Garmin或模型。首次设置看板密码使用`sudo -u sleepscape .venv/bin/sleepscape web-password`，不要复用SSH密码或把密码放入命令参数。

**独立Caddy和公网IP证书。** 使用Caddy2.11.9官方Linux amd64发布包，先与同版本官方SHA-512清单核对，再把二进制安装为root拥有的`/usr/local/bin/caddy`；不得运行未校验的临时下载程序。版本与发布入口为[Caddy官方release](https://github.com/caddyserver/caddy/releases/tag/v2.11.9)，日后升级要重新核验版本、清单和IP签发能力。它使用专用`caddy-sleepscape.service`，不替换系统`caddy.service`或Nginx。

首次部署创建专用配置目录。当前生产 Caddy 同时承载其他站点，应用发布保留其路由和导入；下列单站示例只用于首次安装。已有 `site.env` 只编辑，不覆盖。

```sh
getent passwd caddy || sudo useradd --system --user-group --home-dir /var/lib/caddy-sleepscape --shell /usr/sbin/nologin caddy
sudo install -d -o root -g caddy -m 750 /etc/caddy-sleepscape
sudo install -d -o caddy -g caddy -m 700 /var/lib/caddy-sleepscape
sudo test -e /etc/caddy-sleepscape/site.env || sudo install -o root -g caddy -m 640 /dev/null /etc/caddy-sleepscape/site.env
sudoedit /etc/caddy-sleepscape/site.env
sudo test -e /etc/caddy-sleepscape/Caddyfile || sudo install -o root -g caddy -m 640 deploy/Caddyfile.example /etc/caddy-sleepscape/Caddyfile
sudo install -m 644 deploy/caddy-sleepscape.service /etc/systemd/system/caddy-sleepscape.service
```

`site.env`仅含`SLEEPSCAPE_PUBLIC_IP=实际固定公网IPv4`，不放应用URL、Key或密码。该值必须与应用白名单一致。[Caddy配置](../deploy/Caddyfile.example)显式指定Let's Encrypt生产ACME端点和`profile shortlived`，不使用`tls internal`。`default_sni`同样使用该IP，使按IP访问且不发送SNI的客户端获得正确证书；管理API只监听`/run/caddy-sleepscape/admin.sock`，不公开管理端口。证书、私钥及ACME账户保存在权限700的`/var/lib/caddy-sleepscape`，不能每次更新都删除该目录。

校验配置时明确传入同一IP，不能直接执行遗漏环境变量的`caddy validate`。以下命令在root管理终端执行；`site.env`只能由root修改，且只允许上文一项配置：

```sh
. /etc/caddy-sleepscape/site.env
sudo -u caddy env SLEEPSCAPE_PUBLIC_IP="$SLEEPSCAPE_PUBLIC_IP" /usr/local/bin/caddy validate --config /etc/caddy-sleepscape/Caddyfile --adapter caddyfile
```

Let's Encrypt公网IP证书约160小时有效，由Caddy持续检查并提前自动续期；续期后在内存中替换证书，**无需外部续期cron或手工重载钩子**。`systemctl reload caddy-sleepscape`用于配置更新和重载路径核验，不等于已经经历自然到期续签。首次签发、后续证书序列号/有效期变化和新连接加载各自记录，不能以一次reload冒充自然续期证据。公网TCP80/443及服务器至ACME的出站HTTPS须持续可用，系统时间须正确；不靠自签、`curl -k`或关闭固件证书校验绕过错误。官方依据见[Let’s Encrypt的IP与短期证书说明](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)及[Caddy自动HTTPS](https://caddyserver.com/docs/automatic-https)。

**安装服务并验收。** 从`/opt/sleepscape`安装当前唯一Web、每日采集和维护单元；不另建第二套采集cron：

```sh
sudo install -m 644 deploy/sleepscape.service /etc/systemd/system/sleepscape.service
sudo install -m 644 deploy/garmin-sync.service /etc/systemd/system/garmin-sync.service
sudo install -m 644 deploy/garmin-sync.timer /etc/systemd/system/garmin-sync.timer
sudo install -m 644 deploy/maintenance.service /etc/systemd/system/maintenance.service
sudo install -m 644 deploy/maintenance.timer /etc/systemd/system/maintenance.timer
sudo systemd-analyze verify /etc/systemd/system/sleepscape.service /etc/systemd/system/garmin-sync.service /etc/systemd/system/garmin-sync.timer /etc/systemd/system/maintenance.service /etc/systemd/system/maintenance.timer /etc/systemd/system/caddy-sleepscape.service
sudo systemctl daemon-reload
sudo systemctl enable --now sleepscape.service caddy-sleepscape.service
sudo systemctl reload caddy-sleepscape.service
sudo systemctl status --no-pager sleepscape.service caddy-sleepscape.service
```

云端先使用独立客户端核对可信链、IP SAN和有效期，再验`/health`匿名200、根路径`/`及`/?view=night`在匿名和已登录时均404（无跳转、无登录挑战）、`/sleep`页面及健康API匿名401、正确Basic可读并签发Cookie、仅携带有效Cookie仍可读、错误Host被拒绝、无公开raw/data/.env路径、HTTP重定向HTTPS、8000不可公网连接。Caddy保留Authorization给应用，不再配置第二套Basic；不把仓库或数据目录作为静态目录。Web仅信任127.0.0.1代理头，不能扩大为任意来源。检查前后现有Nginx配置摘要及5688响应保持一致。

接收硬件时保留迁入的USB设备及旧测量配置，对原`local-only`身份执行以下命令生成联网凭据；以实际设备ID和新的私有文件名替换示例，原令牌立即失效：

```sh
sudo -u sleepscape .venv/bin/sleepscape environment-rotate-token --device-id 实际设备ID --token-file .secrets/environment-cloud-token.json
```

随后在`.env`设置`SLEEPSCAPE_ENVIRONMENT_INGEST_ENABLED=true`并重启`sleepscape.service`。只用正确设备Bearer向精确入口`POST /api/v1/ingest/environment-batches`提交无效空对象，可核验认证后返回422而不创建合成事实；无令牌或看板Basic必须返回401。将生成的私有token文件安全取回Mac供配置工具读取，不显示其正文。正式摆位另登记不可变profile，保留旧队列引用的profile；设备2.4GHz Wi-Fi、完整HTTPS地址和实机上传验收见硬件章节。接收服务可用不能代替设备联网通过。

发布完整性策略前先验证备份。目标库通过既有检查存储核验后可直接复查；只在首次初始化时运行 `analysis-prepare`。发布后可按[双源完整性检查](#双源完整性检查)中的指定日期命令保存范围内的新结果。旧审计保留，复查使用已有事实。

通过迁移、访问及恢复核验后，可手动启动同一生产任务入口，再启用定时器：

```sh
sudo systemctl start garmin-sync.service
sudo systemctl start maintenance.service
sudo systemctl enable --now garmin-sync.timer maintenance.timer
sudo systemctl list-timers garmin-sync.timer maintenance.timer
sudo systemctl show garmin-sync.service maintenance.service -p Result -p ExecMainStatus
```

手动启动采集会访问真实Garmin；迁入当日已完成账本则合法跳过，不为演示删除账本或重复拉取。09:00任务始终按上海当天严格N日查询，Persistent补一次漏跑、不扩窗；窗口外缺口另行显式补采。成功但未同步留待次日，长Retry-After持久延后；2小时采集服务上限覆盖采集与至多30次有界报告调用，不代表已验收全部超时恢复情形。采集锁释放后复查同一N日窗口，按`llm_automatic_enabled`决定是否生成合格且未发布的报告；云端已按用户授权启用。维护服务仍使用独立的20分钟上限。

Web单worker、512MiB上限，采集/维护各768MiB、Caddy512MiB均是保护上限，不能相加当作实测内存，也不等于2核2G长期验收通过。按[资源验收标准](architecture.md#资源预算与部署验收)继续观察并发、暖查询、磁盘增长、真实写满/重传、长期定时运行和证书续签；短时部署检查与长期验收分别记录。离机副本需单独目标，不把同机备份称为离机保障。

## 本机模型格式批测

批测使用`tests/model_batch_cases.py`生成可复现的合成证据，固定seed并通过生产完整性/输入检查；`tests/model_batch_runner.py`复用生产`_request`、model_output Schema与完整业务校验。system、固定生成指令、模型参数及Schema保持一致，每例只替换证据JSON。无资格的数据应在输入层拒绝，不混入模型通过率分母。

普通`make check`会跳过`tests/test_live_model_batch.py`的50项真实调用。只有明确取得本次调用额度后，由任务级启动器将`BatchEvaluator`注入pytest配置的`_sleepscape_live_batch`才执行；评估器本身不读取Key、不联网、不调整生产预算。当前测试入口固定50例，不新增生产CLI绕过入口。

授权启动器先核对本机/线上公开参数、Prompt及Schema，并在受控进程用一次性摘要核对URL和Key是否一致，只保存一致性结论。所有调用仍通过`model_runtime`先预留再执行，授权额度仅限本次任务；生产默认上限不变。每例最多一次调用、失败不补调、不在中途修改Prompt；已预留但中断的案例也算消耗，恢复时不能重复调用。429继续尊重冷却。再次开展付费批测需新的明确授权。

本轮输入、授权与逐条安全结果保存于忽略目录`data/reports/model-batch/`，汇总入口为`data/reports/model-batch/summary.json`。结果分别记录JSON解析、模型Schema、完整业务校验；只有完整通过才代表这次返回满足项目发布规则。Schema失败不能冒充已执行后续业务检查。usage只汇总可获得的值，未知失败消耗未计入，不等于总费用。批测不写睡眠数据库，也不发布合成报告；不保存原始失败响应或正文。

## 回归、构建与清理

```sh
make check
make browser-install
make browser-check
uv build --wheel
make clean
# 如需释放浏览器及Node开发依赖占用
make clean-dev
```

保留tests中的必要合成回归；浏览器验收先确认demo=true，不使用真实数据截图。
`browser-install`重建Node/Chromium开发依赖，`browser-check`启动隔离合成服务；这些依赖不进入生产运行。
`clean`删除测试报告/截图、构建产物和代码缓存，`clean-dev`另删除node_modules与.browser-cache；下次浏览器验收先重新安装。
`clean`也删除可重建的`firmware/build/`、生成的`sdkconfig`及旧构建配置；刷写前需要重新`make firmware-build`。
清理入口不删除.venv、.firmware-tools、源码、tests、私有凭据、实际数据库、原始观测、生产核验报告或备份。
不要手工只拷贝正在写入的SQLite主文件做备份，不通过删除WAL或原始观测“清理”运行库。

## 硬件构建与检查

先阅读[采购清单](procurement.md)与[接线方案](plans/hardware.md)。以下入口已经实现；采购前软件验证、接线照片核对、主控芯片/Flash识别、首次刷写与USB状态检查的证据见[核验报告](verification.md)。
云端HTTPS和设备接收已部署，真实设备令牌已私下保存至Mac的`.secrets/environment-cloud-token.json`。当前样机已通过USB configure保存真实2.4GHz Wi-Fi、HTTPS和设备令牌，床头档案已写入，Mac及移动电源供电下四通道连续上传短验通过，曾使用移动电源独立采集，最近一次升级使用 Mac USB 供电。原充电头供电路径曾有I²C缺测，具体原因未定，暂不用于正式供电；常驻电源已换用，三晚连续覆盖与故障恢复继续验收。本机USB新记录不会自动同步云端；下文USB采集仅适用于未配置网络的设备，当前联网样机没有无损切回USB采集的命令。各路径的实测进度见核验报告。

在开发电脑仓库运行；服务器不需要ESP-IDF。Mac还需Apple命令行开发工具提供Git/clang；若提示未安装，先运行`xcode-select --install`并完成系统安装界面。项目脚本不代替这一系统步骤：

```sh
make firmware-setup
make firmware-build
make firmware-check
```

setup固定ESP-IDF v5.5.6提交及子模块，使用项目Python3.12对应的基础解释器，把SDK依赖、CMake3.30.2、Ninja1.12.1装入`.firmware-tools/`，不修改系统Python或安装IDE。
build产生`firmware/build/sleepscape_environment.bin`并强制应用镜像≤1.8MiB。check运行C主机算法/队列测试以及强制的C→Python协议互通；缺SDK或互通依赖会失败，不能以跳过代替通过。
CI的固件任务独立执行setup/build/check；普通Python/浏览器测试与它分开。源码和固定版本是交付基础，构建产物可清理后重建。

### 到货后先检查主控USB识别

1. 拆开外包装，不拆主控外壳；核对主控底部标签及包装型号。传感器和分线板先不接，墙上电源也暂不使用。
2. 在Mac插入主控前后分别运行下面命令，对比新增端口。它只列设备路径，不打开串口、不发送指令：

   ```sh
   .venv/bin/python -c 'from pathlib import Path; print(*sorted(str(p) for p in Path("/dev").glob("cu.*")), sep="\n")'
   ```

3. 连接顺序为Mac → [采购表中的USB转接线](procurement.md) → FIT0641 → 主控；FIT0641的USB-C小插头接主控，USB-A大插头接转接线插口。若macOS询问是否允许配件连接，允许本次主控连接。不要把仅充电线当数据线。
4. 核对实际新增的USB串口，例如`/dev/cu.usbserial-…`；不能猜端口，也不要选择Bluetooth或debug-console。若无新增，先检查插接、数据线及系统USB识别，再按实际芯片核对官方驱动，不预先安装无关驱动。
5. 尚未首次安装SleepScape的新板仍运行出厂程序，不能用SleepScape的`firmware-status`协议判断主控是否正常。USB识别只验证这一步连接，未证明芯片/Flash型号、传感器、麦克风或上传通过。本项目当前样机已完成首次刷写，不必重新执行整片擦除。

确认后**拔掉主控USB**，按[硬件方案的逐步接线](plans/hardware.md#接线步骤)核对C008-C、SEN0528、SEN0228及DFR0759：两块传感器接分线模块，分线模块接主控底部的3V3/GND/G21/G25，全程断电并按文字标签核对。型号、引脚和接线确认后，再接USB执行首次安装。

### 首次安装与后续升级

先确认目标串口，例如macOS的`/dev/cu.usbserial-…`，把下面占位值换成实际端口。工具只操作明确选定的串口，不会自动选择其他USB设备。

**刚购买的板卡第一次安装SleepScape**使用：

```sh
make firmware-first-flash PORT=/dev/cu.实际串口
```

工具先校验串口、SDK和构建镜像清单，再显示整片Flash清除提示。只有你在交互终端准确输入`FIRST-INSTALL`后，才清除板上的厂商预装固件、配置及全部数据，并写入启动程序、分区表和应用。默认115200波特率。此步骤为首次接入本项目准备干净的NVS和SPIFFS区域，避免出厂程序的存储布局影响初始化。本项目样机已完成首次刷写、普通升级、USB四通道本机短采及独立供电四通道Wi-Fi/HTTPS上传短验，记录见[核验报告](verification.md)；样机此后经普通升级至 0.4.0。常驻供电首夜已有记录，后续核对连续三晚及白天覆盖。

首次安装完成后运行只读状态检查，不需要打开串口终端手写协议：

```sh
make firmware-status PORT=/dev/cu.实际串口
```

当前源码与样机已核验的版本均为 `0.4.0`。现有样机升级见[声音事件回听升级](#声音事件回听升级)。新安装后应有`status=ready`、对应固件版本和`configured=false`；若版本不符，先核对构建与刷写结果。该命令只查询状态，不显示Wi-Fi或令牌；`configured`表示网络配置是否有效，不表示USB本地采集是否正在运行。后续可选择本地USB采集，或完成网络配置后验收Wi-Fi上传。

当前工具还显示固件提供的`collection_mode`：`idle`表示空闲、`usb`表示USB采集模式，另有`starting/cloud/failed`。联网模式不等于上传已成功，仍须核对接收结果；旧响应缺该字段时不猜测模式。

未配置且未收到`usb_start`时，固件等待命令；收到本地采集命令后才初始化传感器和采集任务。`ready`仅表示命令能响应，不能作为传感器通过证据。不填假Wi-Fi、地址或凭据绕过网络配置。


**已经使用SleepScape后的普通升级**改用：

```sh
make firmware-flash PORT=/dev/cu.实际串口
```

普通flash同样校验三个允许地址，只写启动程序、分区表和应用，保留NVS配置与离线队列，不整片擦除。不要把`first-flash`当作升级、换Wi-Fi或故障重试步骤；已有数据的板卡若需清除，必须另行确认数据保留/补传和清除目的。换Wi-Fi使用后文configure即可。
串口找不到时先核对数据线和对应USB驱动；设备未进入下载模式时按原厂板卡说明操作，不通过清除Flash试错。

## USB本机采集

这条路径适合尚未配置网络时先验证传感器、接线和入库：传感器→主控→现有USB数据线→Mac→同一SQLite环境表。无需家中Wi-Fi、HTTPS域名、云服务器或设备令牌；无需另购附件。USB 统计采集支持固件 `0.3.0` 和 `0.4.0`，测量档案须与实际设备版本一致。已有有效网络配置的设备会拒绝USB采集，当前未提供无损切回命令，不能用整片清除绕过。

连接硬件并确认实际串口后，从项目根目录运行：

```sh
.venv/bin/python -B tools/firmware.py collect \
  --port /dev/cu.实际串口 --duration 180
```

默认先收集180秒，用于短时验收。默认设备标识`bedroom-01`、档案标识`usb-bench-030`、摆位`bench-usb`表示桌面接线调试，不能把这些数据标成正式卧室夜间观测。更换摆位时使用新档案标识与真实摆位：

```sh
.venv/bin/python -B tools/firmware.py collect \
  --port /dev/cu.实际串口 --duration 3600 \
  --device-id bedroom-01 --profile-id bedroom-usb-030 \
  --placement-id bedside-fixed
```

同名档案不能悄悄修改固件版本或摆位。首次本地登记前，程序对已有数据库创建备份并恢复核验，再登记不具备HTTP认证能力的本地身份；不生成网络令牌，不打开HTTP写入口。复用现有数据目录和独立环境表，保留Garmin数据。

保持Mac唤醒和USB连接。首次完整分钟需要等待，收到数据后终端显示各通道统计及质量；温湿度每10秒、光照每秒采样，声音持续采样只保留相对强度统计，均按分钟保存。没有读数时保留缺测，不填0。实际保存状态以收到的记录和提交结果为准。

达到时长或按Ctrl+C时，程序尝试发送`usb_stop`并复位主控回等待命令状态，不清除Flash队列；未定稿的末尾窗口可能丢失。若停止响应未确认，程序会报告设备可能仍在采集，不能当作已停止。Mac退出或断线后设备已提交、未确认的队列仍保留，下次按相同身份启动可补收；USB断电期间不采样。

时间锚点标为`host_usb`，来源为Mac时钟，误差未知；不把接收时刻直接写成采样时刻。当前boot握手不能校正其他boot的旧缓存。P3环境看板和本机短采查询已验；联网部署后沿用同一合同切换到下文HTTPS方式，真实缓存补传和长期稳定性仍须分别验收。

## 环境设备登记与HTTPS接收

以下服务端命令必须在**实际负责接收上传的服务器及其数据库配置**下运行；在本机登记不会自动登记到云服务器。
先完成上文公网IP HTTPS与Host白名单部署，保留8000仅本机监听，再在服务端私有`.env`中设置：

```dotenv
SLEEPSCAPE_ENVIRONMENT_INGEST_ENABLED=true
```

重启Web服务使设置生效。未启用时环境入口关闭；公网入口必须使用HTTPS，不能用伪造代理头或普通HTTP绕过。
设备走独立Bearer认证，不使用浏览器Basic密码或Garmin Key。Caddy必须把Authorization透传给应用，不加第二套覆盖设备认证的Basic。

首次准备设备档案，保留已有私有目录与文件，只复制尚不存在的文件：

```sh
mkdir -p .secrets
chmod 700 .secrets
cp -n firmware/profile.example.json .secrets/environment-profile.json
chmod 600 .secrets/environment-profile.json
```

编辑私有档案中的`placement_id`，用实际固定摆位的标识替换占位文字，例如`bedside-left-2026-11`。
其他硬件、固件版本、`i2c_bus`、`light_measurement`、声音处理方式和阈值字段必须与刷入程序匹配。SEN0228使用项目固定弱光档，不能只替换传感器名称却沿用其他光照档位；以当前`firmware/profile.example.json`和[数据契约](data-contract.md#当前光照测量配置)为准。没有校准就保留`uncalibrated`，不能为了通过校验虚填测量能力。
若此前登记过其他传感器档案，不覆盖旧档案；使用新的profile_id登记本组合，再让设备配置指向它。旧记录与原档案继续保留作为观测证据。
下面ID为示例，可保持一致使用，不能把自己的秘密填入命令参数：

```sh
uv run --locked sleepscape environment-register \
  --device-id bedroom-01 --profile-id bedroom-veml-01 \
  --profile-file .secrets/environment-profile.json \
  --token-file .secrets/environment-device.json
uv run --locked sleepscape environment-status
```

若目标机已有数据库，首次登记前先执行上文`db-backup`并确认恢复检查成功。登记命令校验现有基础表，再以事务建立环境表及设备记录；它不会自动生成备份，Garmin事实不被替换。令牌随机生成，只把哈希存库；
`--token-file`必须是不存在的新文件，输出为JSON，包含device_id、profile_id和token，权限600、父目录700。终端只报告成功状态，不显示令牌。
status只读本地环境接收状态，不联网、不凭空创建数据库；“已登记”不代表设备已经上传。

如果把USB采集数据库迁到服务器，`bedroom-01`可能已经是仅本地登记的设备，不应再次`environment-register`或删除旧数据。先按下文登记正式摆位的新profile，再用`environment-rotate-token`为既有设备生成真实网络令牌；令牌文件留在私有目录。当前样机已生成并配置有效云端令牌，单纯换摆位不必再次轮换。历史USB记录、原档案和主机时钟锚点继续保留。

## USB配置与首次上传

USB操作在连接硬件的开发电脑上执行。若令牌在云端生成，通过已有加密SSH通道安全复制到本机私有目录，并核对文件600/目录700；
不要把内容粘贴进聊天、日志、终端命令或公共配置。也不要拿Garmin Key尝试设备令牌字段。

```sh
uv run --locked python tools/firmware.py configure \
  --port /dev/cu.实际串口 \
  --token-file .secrets/environment-device.json
```

工具从注册JSON自动读取device_id/profile_id/token，只交互询问完整上传地址、2.4GHz Wi-Fi名称和隐藏密码。
上传地址为`https://你的实际公网IP/api/v1/ingest/environment-batches`，不带查询参数、片段或用户名密码；Wi-Fi密码为空或8–63字节。设备令牌与模型Key、Garmin Key互不混用。
它通过115200 UART发送单行配置，仅收到“已写入NVS”的ACK才报告保存成功；设备随后重启，串口输出不会回显秘密。
没有可隐藏输入的终端时会拒绝回显降级；可改用权限600、目录700的私有完整配置JSON：

```sh
uv run --locked python tools/firmware.py configure \
  --port /dev/cu.实际串口 --config-file .secrets/environment-usb.json \
  --token-file .secrets/environment-device.json
```

这里USB配置JSON填写server_url、ssid、password，可省略由令牌文件提供的device_id/profile_id；**不能再包含token**。
显式填写的设备/配置ID必须与令牌JSON一致，否则拒绝。若不使用`--token-file`，完整配置JSON需包含全部六项。
不用文件也可运行`make firmware-configure PORT=/dev/cu.实际串口`，手动填写ID、地址、Wi-Fi，令牌和密码均隐藏输入。

配置成功后再运行：

```sh
make firmware-status PORT=/dev/cu.实际串口
```

此时版本应与实际刷入的当前固件一致，且`configured=true`。该布尔值只说明固件能读到有效的已保存配置，配置ACK只证明NVS写入；均不证明Wi-Fi连接、TLS或上传成功。还需在服务器`environment-status`确认新增真实记录，并检查同一设备/boot的NTP锚点、实际采样窗口与各通道覆盖/故障。看板应能查询本次新记录，不能以页面能打开代替真实上传。

随后改接USB充电头并保持登记摆位，从云端观察约10–12分钟、至少两个正常上传周期：新分钟序号及批次持续推进、启动暖机后各通道有效、设备队列未持续增长、丢失与隔离计数没有异常新增。该短时结果与整夜/断网/掉电验收分别记录；不要求每个状态快照的队列为零。configure会主动重启，断电换电源也产生新boot；串口打开可能受USB串口控制线影响，独立运行观察时不反复打开串口，用云端记录判断持续性。启动首分钟缺测按原质量标记保留，不补零或删除。

独立供电后，Mac可断开或关机；设备继续按原Wi-Fi和HTTPS配置运行，断电期间不采样。换Wi-Fi可再次USB配置，不必重刷程序。

## 换摆位、令牌轮换与撤销

改变摆位或测量设置时先准备新档案：

```sh
cp -n firmware/profile.example.json .secrets/environment-profile-next.json
chmod 600 .secrets/environment-profile-next.json
```

编辑新档案记录实际摆位/设置，再登记新的profile_id；已登记档案不可原地覆盖：

```sh
uv run --locked sleepscape environment-profile \
  --device-id bedroom-01 --profile-id bedroom-veml-02 \
  --profile-file .secrets/environment-profile-next.json
```

新档案必须填写实际变更。位置、传感器朝向和距主控距离等细节保存在私有摆位记录；未提供的值写未知，不猜测，也不把垂直高差当作与枕头的直线距离。设备需接回Mac一次，通过USB configure切换到同一新ID，完成后可改接充电头；这不是重刷固件，不必因为换摆位轮换有效令牌。旧记录保留原配置，普通配置不清除Flash队列。

注册JSON内若仍写旧profile_id，工具会拒绝与新配置混用；可以更新该私有JSON中的profile_id为已经登记的新ID。已有轮换令牌JSON仅含device_id/token时，在私有USB配置JSON填入新profile_id即可。仅确实需要替换凭据时才执行下面轮换流程。

```sh
uv run --locked sleepscape environment-rotate-token \
  --device-id bedroom-01 --token-file .secrets/environment-device-rotated.json
```

轮换立即使旧令牌失效，已有历史与待补传记录保留；新JSON包含device_id/token，USB配置时需填写已登记的profile_id，
或从私有USB配置JSON读取。再执行configure并指向新令牌文件即可，不在命令行传令牌实值。
完成切换后删除已经失效的本地旧令牌文件，避免保留多份无用敏感配置；不要删除数据或设备档案记录。

```sh
uv run --locked sleepscape environment-revoke --device-id bedroom-01
uv run --locked sleepscape environment-status
```

撤销立即禁止该设备上传，历史仍保留。计划重新启用时执行令牌轮换，再USB配置新令牌；不能通过清库或更改设备ID绕过拒绝。

## 硬件运行与故障边界

| 现象 | 当前行为与处理 |
| --- | --- |
| 冷启动时没有网络/SNTP | 继续采样和记录boot_id/单调时间；UTC未知，取得SNTP后才允许TLS上传；不能用接收时刻补齐 |
| Wi-Fi中断 | 1–300秒退避重连；持续供电时目标缓存48小时，容量验证不等同实机验收 |
| 401/403 | 保留记录、提示认证拒绝并延长重试；核对登记/撤销状态，必要时轮换再USB配置 |
| 429或普通网络错误 | 遵守有界Retry-After并退避；没有完整且匹配的逐记录ACK就不删除待发记录 |
| 首次空白存储 | 仅确认整个区域全空白才初始化文件系统；普通刷写保留已有数据 |
| 文件系统损坏 | 不自动格式化、不整片擦除；启动时停止采集初始化并报告存储异常，保留现场后诊断 |
| 拒收记录隔离区达到64KiB | 停止继续隔离/上传推进并保留数据，不因此停止其他采样任务；需检查存储状态后处理 |
| 单通道缺失/异常 | 记录缺失、有效覆盖及质量，不用零值伪装成功；其余通道按真实状态继续。I²C两传感器共线，整体总线故障可能同时影响温湿度与光照 |
| 持续出现I²C `bus_error` | 与首次暖机分开处理；先保留采样/质量证据，再断电核对主控跳线、分线板及传感器插接和受拉情况，复测持续覆盖。网络或声音正常不能证明I²C接线稳定；不能仅凭此标记确定为某根线、供电或驱动故障 |
| 光照低光、暖机或饱和 | 按测量配置保留低光提示、暖机缺口或`out_of_range`；饱和不能当550 lx的真实观测，不自动换量程或修改校准 |
| 看板没有环境曲线 | 检查实际采集是否在所选窗口、图例是否开启；看数据状态的索引/未知/冲突/待更新原因。新库先显式启用索引，GET不会自动迁移；下午试采从最近采集窗口或自定义范围查看 |

### 供电与通信异常的对照排查

1. 先确认USB插头已完全插入、没有明显受拉。接Mac时可按[USB识别步骤](#到货后先检查主控usb识别)仅枚举新增设备；不要为确认供电反复打开串口，以免控制线干扰对照。设备Reset用于重启，不清除已保存的Wi-Fi、令牌和测量档案配置；重启产生新boot，仍须从云端核对实际恢复。
2. 保持主控、传感器的位置和连接线不动，只改变USB供电来源，例如在Mac、合适的5V USB充电头和移动电源间对照。Mac供电时保持开机、不合盖，不执行`collect`、`configure`或刷写，设备仍通过原Wi-Fi/HTTPS上传。只换电源不改变测量档案，不需要新profile或再次USB配置；把供电类型、切换时间及对应boot记入私有验收日志。
3. 在云端按各boot检查至少两个正常上传周期，分别对照温湿度、光照、声音的有效计数、质量、序号推进、丢失/隔离状态与时间索引。正常首次光照暖机及声音启动缺口保留原标记；反复`bus_error`是通信异常，不能归为正常暖机。`low_light_limit`本身表示弱光测量局限，有效计数完整且无错误时仍可使用该分钟读数，但不能据此承诺绝对弱光精度。
4. 电源对照能缩小故障范围，不能凭某一次变化就认定某件损坏；USB插接、线材、供电路径及接线稳定性仍可能影响结果。保留异常和正常两组记录，不用后一次正常覆盖前一次失败，也不以零丢失计数代替传感器有效覆盖。
5. 移动电源短采通过只证明该时间段能运行，不代表整夜续航或长期插电方案已验收；后续需要稳定独立电源下的整夜覆盖和断电/重连验收。

普通NVS不提供抗物理读取保证；USB及统计队列都不保存或上传PCM录音。断电期间不能采样，未完成/未提交窗口可能丢失。
当前USB通信、首次刷写、四通道短采与真实Wi-Fi/HTTPS链路已通过；Mac及移动电源供电对照均完成连续两个正常上传周期，启动后四通道有效计数完整、未见I²C bus_error，首分钟暖机及声音启动缺口保留。原位使用已核验的独立电源采集时，Mac 无需保持连接。

原充电头供电路径及插接检查后的复验曾出现通信缺测，具体原因未定，未证明某件损坏；该路径暂不作为正式供电。样机已换用常驻电源并开始连续观测；后续电源对照应保持原位及传感器接线不动；仅换USB供电来源无需改profile或再次configure，以私有供电记录和新boot区分。移动电源的整夜续航和长期插电方式、受控响应、准确度、8小时并发采样、三晚覆盖、48小时离线补传和随机掉电仍待实测。核验报告区分软件回归、短时实物与长期通过。

## 声音事件回听升级

设备接入 USB 后，先用 `make firmware-status PORT=实际串口` 核对身份和现有固件。按本手册已有的 profile 登记与 configure 流程，以原摆位、设备 ID 和当前测量参数为基础，在云端新增 firmware_version 为 0.4.0 的不可变 profile。使用普通 `make firmware-flash PORT=实际串口` 后配置该 profile，保留既有有效设备凭据、Wi-Fi 和离线观测队列。不要执行首次整片擦除。确认新 boot 的四通道持续上云，并在夜间事件、次日同步及裁剪后登录夜晚详情试听。旧固件和历史摘要没有可回听片段。
