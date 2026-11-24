# 数据契约与展示口径

更新于 2026-11-23。本文定义 Garmin、环境设备、看板、晨起睡感、逐夜解读、7 日复盘和运行账本的字段与计算口径。当前部署见 [README](../README.md#当前状态)，实采与软件证据见[核验报告](verification.md)。

## 当前数据范围

13类资源与窗口见 [Garmin方案](plans/garmin.md)。

| 数据 | 当前来源与含义 | 边界 |
| --- | --- | --- |
| 主睡眠 | 起止、总时长、阶段累计时长、觉醒次数、评分 | 总睡眠不含午睡，当前分期时间线不可得 |
| 午睡 | 睡眠中的 `dailyNapDTOS`、`napTimeSeconds` | 与主睡眠分开，存在字段不保证每天有记录 |
| 心率 | 原生读数、睡眠 `avgHeartRate`、静息心率 | 不用日最低值、静息值或全天均值替代夜均心率 |
| HRV | 夜间读数、夜均值、7日均值、个人基线 | 不伪造逐心搏数据，不用7日均值替代昨夜均值 |
| 活动 | 全部运动类型、起止、时长、单次负荷、心率和训练效果 | 单次负荷不等于急性负荷，高负荷不必然表示高强度 |
| 压力和步数 | 旧/v2日汇总及独立日统计 | 没有完整压力时序，生理压力不同于心理评分 |
| 呼吸 | 睡眠及日汇总字段 | 不声称有完整夜间曲线，清醒均值与全日均值分开 |
| Body Battery | 日摘要、原生时序、睡眠充电量和醒来值 | 设备估算，不作为同夜睡眠的独立原因 |
| SpO2 | 单点、小时均值和日摘要 | 原生和小时均值分开，未记录可为空 |
| 日常活动摘要 | wellness activity summary中的时段与指标 | 不代替正式运动列表，不保证午睡或体动细节 |

完整压力时序、独立呼吸时序、训练准备度、恢复时间和完整体动曲线不是当前已支持能力。
`recoveryHeartRate`也不是恢复时间。环境首版采集温度、湿度、光照、相对声音汇总和事件；
选型、采样和上传合同见下文“环境采集合同”；已有USB桌面短采及正式卧室联网记录，按各自测量档案保留，备用移动电源独立短采已通过，常驻供电已确认，备用电源续航和长期覆盖仍待验收，具体证据见核验报告。

## 来源、标准化和修订

原始JSON独立保存。标准化记录带 `resource`、`kind`、记录键、来源日期、UTC时刻、
抓取时间和快照关联；数据保存 `source` 映射、参与映射的 `raw` 值和 `quality` 标记。
请求审计保存转换版本、字段问题及响应状态；当前未另行查询Garmin设备/固件API资料。

相同 `(resource, request_key, content_hash)` 不重复保存原始快照，变化的响应保留观测修订。
当前记录主键为 `(resource, kind, record_key)`，有效响应替换对应来源分区；
无效响应或请求失败保留旧记录，由核验提示可能过期。
`sleep`和`sleep_summaries`、`daily`和`daily_summaries`等来源分开保存，不能重复合计。
完整原始响应可能带活动位置、owner信息或账户标识，即使展示不用这些字段，数据库和详细报告仍保持私有。
只读API通过字段白名单生成视图，不返回完整raw、owner、坐标、凭据或绝对文件路径；HTTP读取不触发Garmin请求。

## 日期、时间戳和采样

| 字段/概念 | 契约 |
| --- | --- |
| 请求 `start_date/end_date` | 来源日历日期范围，用于补采、替换和请求审计 |
| 样本 `date` | 来源接口日期分区，优先响应日期；单日接口缺日期时可用请求日期 |
| 样本 `source_date/source_calendar_date` | 保存来源日期，可与实际发生日期不同 |
| 样本 `timestamp/measured_at` | 实际UTC时刻，ISO字符串明确带 `Z` 或 `+00:00` |
| 样本 `local_date` | 实际时刻在配置时区的日期，默认 `Asia/Shanghai` |
| 睡眠归属日期 | 优先Garmin `calendarDate`，缺失才按结束时刻的配置时区日期回退 |
| 活动归属日期 | 优先有效 `startTimeLocal` 的当地日期，与API筛选分区相同；缺失才回退到配置时区并标记 |

来源分区不能当作样本实际发生日期。跨午夜读数可属于某一睡眠日，却发生于前一天：
用 `date` 管理来源分区，用 `timestamp` 与环境数据对齐。
活动另存 `source_calendar_date/source_date`、配置时区换算的 `local_date` 和 `timezone`。
旅行导致二者日期不同时发出警告，不凭无偏移字符串推断来源时区。按来源日的负荷比较保留此警告；
精准“睡前运动”联动须使用UTC时刻裁剪，不能只用日期连接。

`*GMT`字符串无时区后缀时仍按UTC解释；数值时间戳按毫秒解析并检查合理范围，
不把秒级值静默乘以1000。官方睡眠字段类型与描述有矛盾，因此保留原值；
含义不清的本地数值时间戳标质量问题，不猜测时区。

HR、Body Battery、SpO2数组按descriptor的index/key解码，不固定列序。
SpO2的 `native` 与 `hourly_average` 使用不同序列类型和记录键，不能混成同频数据；
小时均值聚合窗口为3600秒，原生序列的未知窗口保持空值。
保存样本数和有效样本数；观测间隔不等于已确认的设备采样标准。
预期频率未确认时，时间覆盖率保持空值，不用样本数量冒充连续覆盖率，不默认插值。

## 晨起睡感记录

在现有私有 SQLite 中使用独立表 `morning_feeling`，每个看板醒来日期一行。仅保存以下三项：

| 字段 | 定义 |
| --- | --- |
| `night_date` | 主键，取点击时看板显示的日期（YYYY-MM-DD），与采集记录无外键依赖。 |
| `feeling` | 必填枚举，`poor` 为较差、`fair` 为一般、`good` 为较好。无行表示未填。 |
| `filled_at` | 服务端记录的 UTC 时间，表示最近一次实际填写或改选时间。补填仍归所选日期，时间记录实际写入时刻。 |

使用 `GET /api/v1/nights/{day}/morning-feeling` 读取，成功时返回该日记录或空值。使用同一路径的 `PUT` 提交，仅接收 `feeling`；日期取路径，沿用夜晚入口日期范围。首次插入，改选在事务内更新值及时间，同值重复提交返回现有记录并保留时间。多个客户端同时改选时取最后成功提交的值。返回成功以 SQLite 提交为准；表未准备好或读写失败须返回错误。只保存当前选择与最近填写时间。睡感独立于设备事实和分析资格，可作为[逐夜](#睡眠分析合同)及[7 日复盘](#7-日复盘总结合同)的可选证据。已发布报告保持生成时的快照。

## 看板睡眠日期展示边界

可选`SLEEPSCAPE_DASHBOARD_START_DATE`为严格`YYYY-MM-DD`日期，范围2000-01-01至2100-12-31；空值默认不裁剪。其含义为最早可展示的**睡眠归属日**，不是采集或存储起点。

- `GET /api/v1/status?navigation=true` 仅读取导航使用的 timezone、demo、dashboard_start_date、latest_night、latest_success_at、stale。夜晚详情的 published_summary 为已校验发布报告的摘要正文或 null，首次展示时当前来源仍待核对；完整报告和来源变化状态由原有分析接口提供。
- `GET /api/v1/status.dashboard_start_date`返回配置日期字符串或`null`；`latest_night`与`earliest_night`仅统计允许范围内的睡眠，无可见睡眠时均为`null`。
- 夜晚列表按该下界裁剪，保留允许区间内的空日；查询或游标完全早于下界时，`items`及`available_dates`为空，`next_cursor=null`。未指定结束日且无可见睡眠时，使用配置时区今天与起始日的较晚者。
- 早于下界的夜晚详情、夜晚时间轴和分析状态统一返回404及`{"error":"night_before_dashboard_start"}`；前端日期选择、翻页和旧URL同时遵守下界。
- 睡眠日2026-11-18仍按实际起止关联11月17日晚的环境和身体读数，不按18日零点裁断跨夜窗口；昨日负荷、30日基线、Garmin N日回查及全部原始历史均不改变。
- 独立`/api/v1/environment/timeline`的自定义UTC查询保留原合同，用于调试和查看实际采集窗口；展示下界不构成原始数据删除或更强的访问隔离。

部署值与修改操作只在[运行手册](operations.md)维护。

## 历史复盘

交互及验收见[看板方案](plans/dashboard.md#7-日睡眠复盘)。`GET /api/v1/nights` 接收可选布尔参数 `include_review`，默认关闭。启用时在 `items[]` 增加 `morning_feeling`，并在响应顶层增加 `review`。只汇总快照中本次分页实际返回的日期。日期按展示起点裁剪并保留空日。分页游标、排序和 `available_dates` 沿用原合同，其中 `available_dates` 仍只反映 Garmin 睡眠记录。

| 字段 | 合同 |
| --- | --- |
| `items[].morning_feeling` | 当前醒来日期的 `poor`、`fair`、`good` 或 `null`。由 `review.feeling_state` 区分尚未填写与不可读取。只返回枚举，提交时间留在原单日接口。 |
| `review` | 有返回日期时为对象，空页为 `null`。 |
| `review.start_date/end_date/night_count` | 本页最早和最晚的醒来日期，以及实际返回的日历日数。单夜分母为一，跨展示起点按裁剪后的天数计。 |
| `review.feeling_state` | `available` 表示睡感表读取成功，`not_prepared` 表示库或表未准备，`unavailable` 表示睡感读取或校验失败。后两者只降级睡感，列表和客观统计仍正常响应。 |
| `review.feeling_counts` | 睡感可读时包含 `poor/fair/good/unfilled` 四项，均为非负整数，总和等于 `night_count`。不可读时为 `null`。 |
| `review.medians` | `sleep_seconds/average_heart_rate/night_average_hrv/sleep_start_offset_seconds/sleep_end_offset_seconds` 分别返回 `{value, valid_n}`。中位数按原数值计算，偶数夜取中间两个值的算术平均。没有有效值时为 `{value: null, valid_n: 0}`，分母统一取 `night_count`。 |

- 主睡眠时长用现有 `sleep_seconds`，心率用睡眠夜均心率，HRV 用昨夜夜均值。各项独立纳入状态为 `available` 且数值有限的对应卡片。最近采集失败的旧值仍保留在历史行，排除于统计。睡感、环境完整性和报告状态都独立于这些指标的纳入。
- 入睡与醒来只使用同一已选睡眠来源的有效闭合窗口。起止需有明确 UTC 偏移，满足起点早于终点、实际跨度至多 24 小时且结束时间已到。来源标记正在睡、窗口无效或冲突、最近对应来源请求失败、当地结束日与睡眠归属日不同均排除这两个时间统计。使用已归一化的原始起止，不取图表回退窗口。
- 时间中位数使用配置时区的**当地日历日偏移 × 86400 + 当地时分秒**。基准是该条记录的睡眠归属日，当日 00:00 为 0，前一天 23:50 为 -600，当日 00:10 为 600。展示按当地钟表时间到分钟，前一天标「前日」，更早的按实际日期偏移提示。睡眠时长仍取既有秒数，实际窗口跨度按 UTC 确认。
- 当查看日期范围的统计时，行筛选或排序不改变分母。补填按所选醒来日归档，重新查询可看到最近已提交结果。睡感读取和睡眠列表同一 SQLite 只读快照，继续沿用看板访问保护和无缓存策略。

## 7 日复盘总结合同

本节定义 7 日 LLM 总结。现有历史页统计见[历史复盘](#历史复盘)，生成流程见[LLM 方案](plans/llm.md#7-日复盘总结)。

### 窗口及分母

- 固定最近七个自然日，自动生成的截止醒来日取现有日任务的上海当日。实际起点取「截止日减六日」、支持的最早日期和配置展示起点中的最大值。截止早于有效起点为空。报告按实际起止日期归档，历史页可以按已选截止日读取对应结果。
- 可见日数是裁剪后的闭区间日历日数，空日保留。11 月 20 日覆盖 11 月 18–20 日三日，11 月 21 日覆盖 11 月 18–21 日四日，11 月 24 日起可满七日。非空窗口内每个日期都需通过已实现的逐夜双源质量核验，即 `eligible_n == visible_n`。当前允许的 `with_gaps` 保留限制，逐夜回查过期状态与核心质量分别处理。任意日期未通过时，返回按日期排序的未通过日期及已有核验缺项，零外呼。核验读取失败单独表示。
- 每项有效夜数由实际进入该项计算的可比夜决定。睡感填写数独立，未填和读取失败分别标记，属可选项。普通历史页的展示和统计依指标实际可用性，独立于 LLM 资格。筛选、排序和分页不改变报告范围或分母。

### 输入与输出

实际范围整体通过后，程序逐日构造有界证据，再填入请求。发送实际起止、可见/合格夜数、质量限制、程序计算的聚合和比较，以及实际范围内全部合格夜的白名单日期事实，最多七晚。

- `window` 包含 `start_date/end_date/visible_n/eligible_n`。 `quality` 包含允许缺采、不可用状态、睡感可用性与填写分布，限制采用固定码。
- `aggregates` 给指标、单位、中位数及 `valid_n`。 `comparisons` 给指标、可比组、双方分母和程序计算的方向/差值。
- `nights` 给合格醒来日、质量、同一夜的有限指标、允许的睡感及匿名测量组。 `examples` 只引用其中最多两晚的证据 ID。聚合、比较和夜晚各有可引用 ID。
- 新复盘摘要至少引用一项睡眠时长或作息聚合、睡感分组比较或可引用的例证夜晚。生成校验拒绝常见的采集状态开场，已存报告按原合同读取。

| 证据 | 口径 |
| --- | --- |
| Garmin | 主睡眠时长、入睡/醒来、夜间平均心率、昨夜 HRV、前日完整活动负荷。各项中位数按本段有效夜计算，作息沿用[历史复盘](#历史复盘)的跨午夜偏移。 |
| 环境 | 睡眠窗口内有效温度、湿度、照度和相对声音的夜均值及质量，沿用[逐夜分析合同](#睡眠分析合同)加权口径。声音先在每晚能量域求均值，再比较可比夜的夜均值。配置仅作匿名分组，同设备、同测量配置和摆位内比较。 |
| 晨起睡感 | 授权范围内仅附合格夜的较差/一般/较好/空枚举及各组计数。睡感不可读时提供限制并省略睡感比较。填写时间和不合格夜的睡感留在本地。 |
| 比较和回看 | 较好与较差若各有至少两晚使用同一有效指标，程序才给该指标分组中位数和差值。回看实例至多两晚，优先选最近的「较差且时长不低于本段中位数」及「较好且短于中位数」的有效夜，剩余名额用最近合格夜填充。每项聚合、比较、回看实例有固定证据 ID。 |

白名单排除原始响应、来源和设备身份、实际摆位细节、定位、活动标题、自由文本、日志、凭据和逐分钟读数。证据输入最多 24 KiB，完整消息最多 32 KiB。返回概况一段、观察一至三条、待验证线索和后续行动各至多一条。各条引用当前证据，线索附明确替代解释。日期、数字、分母、质量等由程序从生成时证据展示。机器结构见[包内 Schema](../src/sleepscape/resources/sleep-review.schema.json)。

### 请求、状态及持久化

| 项目 | 合同 |
| --- | --- |
| GET /api/v1/sleep-reviews/{end_date} | 认证只读，返回所选范围、执行状态与范围完全一致的已保存报告。若上次因数据不完整而未生成，按当前快照重核，全部合格时返回 `ready`；仍有缺项时返回当前日期及缺项，重核失败时返回 `readiness_unavailable`。此时 `checked_at` 是本次成功重核时间。展示起点改变时使用新的实际范围。查询零模型调用，历史执行记录保留。 |
| 日任务结果 | 在现有 `daily-run` 中增加独立复盘阶段结果。归属日沿用本轮日任务，执行发生在采集、完整性复查和逐夜分析之后。日期完整度不足、核验不可读、预算阻塞、执行失败和成功各有安全状态，采集终态及逐夜结果保持各自语义。 |
| 输入与来源变化 | 持久输入记录语义来源摘要、发送输入摘要和生成配置版本，摘要不公开。重复抓取时间和窗口外事实不改变语义摘要。发布后数据改变提示来源更新，原报告与生成时证据持久保留。 |
| 执行与幂等 | 逐夜与复盘共用 P1 的上海自然日 30 次物理额度、锁、容量准入及 Retry-After。可恢复失败和输出校验失败在同日剩余额度及任务时限内重试，每次单独计数，成功即止。单次调用沿用 180 秒上限。失败和中断保留固定安全码、元数据与已占预算。 |
| 存储与恢复 | 可选 `sleep_review_schema`、`sleep_review_inputs`、`sleep_review_runs`、`sleep_review_reports` 及升级添加的 `sleep_review_failed_attempts`。重试前，在同一 SQLite 事务归档失败的请求 ID、截止日期、输入 ID、尝试时间和安全错误码，再更新当前状态。报告每截止日期唯一，保存实际起点及生成证据。显式迁移及备份核验新旧布局、摘要和引用；旧备份仍可恢复。 |

发布时使用同一 SQLite 连接上的 PRAGMA data_version。读前、结束只读快照后，以及取得短 BEGIN IMMEDIATE 后对照提交版本。发现变化时结束写事务，有限次本地重查。仍冲突则废弃候选，持久新鲜度继续用语义摘要。外呼阶段不持任何 SQLite 写事务。

## 今日采集、滚动窗口与完整性

常规`garmin-collect`默认7日详细/31日背景，截止昨日；7晚完整负荷验证建议7/38。
`garmin-sync`按配置时区今天拆分资源：sleep、HRV、心率、SpO2、Body Battery和wellness详细数据可含今天，
daily及背景摘要、压力、步数和正式活动仅到昨日。看板不再仅按日期将今天的观测标`pending`：有效值为`available`，缺失/无效/失败旧值按实际情况分别返回。夜晚保留兼容字段`is_provisional=false`，不代表上游数据永远不会修订。

同步计划保存`complete_day_through`、`provisional_dates`、`provisional_resources`和策略；请求审计保存`collection_state`。
这些仍是采集覆盖及暂定口径，离线核验也保留完整日边界，与看板可用性状态分开；不证明上游同步无遗漏或手表判断正确，不修改raw来“完成”今日数据。Garmin允许编辑已记录的睡眠时间，官方依据见[Garmin方案](plans/garmin.md#官方依据与能力边界)；继续保留补采、有效修订及原始证据。

未结束的全天活动日即使已经取完当前分页，仍不能作为完整日负荷：正式`value=null`、`state=missing`、`reason=activity_day_not_finished`，保留`known_load_sum/activity_count`表示已知观测。分页不完整、负荷字段缺失等原因继续单独说明；不能把未完成日或空响应填成0。正常夜晚的“昨日运动负荷”仍使用D−1的已结束日。
首次背景38日、日常7日回查、每隔7日宽回查38日；旧历史保留。仅整轮成功推进同步成功窗口，失败/无效响应保留旧值。
短停机扩大窗口，超过30个已结束日未成功采集则返回明确补采窗口与命令并停止；未执行成功不能标为已补齐。
看板与verify均从存量记录及请求状态回溯所需30日基线；verify以`load_evaluation_window`单列评估范围，单次coverage统计仍按采集窗口。

## 单位与语义

| 来源字段 | 原单位/含义 | 标准化处理 |
| --- | --- | --- |
| 睡眠 `*Seconds`、午睡 `napTimeSec`、活动时长 | 秒 | 保持秒，主睡眠不含午睡 |
| 旧 `daily` 的 `stressDuration/restStressDuration/...` | 秒 | 保持秒，不除以1000 |
| v2 `daily_summaries` 的 `stress.*DurationInMillis` | 毫秒 | 除以1000转为秒 |
| 旧 `durationInMilliseconds` / v2 `wellnessChronology.totalDurationInMillis` | 毫秒 | 除以1000转为秒 |
| 两版日汇总的强度分钟 | 分钟 | 保持分钟，不套用v2默认毫秒规则 |
| 活动 `beginTimestamp` | Unix epoch毫秒 | 转为UTC；缺失时使用 `startTimeGMT` |
| 活动和步数统计距离 | 米 | 保持米 |
| 心率 / 呼吸 / SpO2 | 次/分钟、次/分钟、百分比 | 分别保存，不与负荷或评分混用 |
| HRV读数与均值 | 毫秒 | 区分夜均值、7日均值和原生读数；接口/本地转换核验不等于测量准确性验证 |

旧 `bodyBatteryDuringSleep` 是睡眠期间充电量，`bodyBatteryAtWakeTime` 是醒来值。
v2 `heartRate.restingValue` 在缺少静息心率时可能回退为最低值，因此标准化名为
`resting_heart_rate_or_minimum`，不能无条件与旧版静息心率合并。
v2 `bodyBattery.endOfDayValue` 是睡前最后值，不是午夜值；当前保留在原始响应中。
旧 `avgWakingRespirationValue` 是清醒均值，不能改名为全日或夜间均值。

缺失、null、负值哨兵、非有限数、超量程和真实零分开处理，缺失不补零。
心率、HRV和SpO2中的零不当有效生理读数。有效空响应不同于请求失败；
来源标记为不可绘制的SpO2点保存质量信息，不计为有效点。

## 负荷和睡眠日期关联

`load_comparison` 针对最新完整活动日 `T`（sync使用`complete_day_through`，普通collect使用截止日）：合计当天全部已记录活动的 `activityTrainingLoad`，
与 `T-30` 至 `T-1` 的30个自然日日均比较，不把T纳入基线。
`sleep_day_load_comparisons` 则逐个针对睡眠日 `D`：目标活动日为 `D-1`，
基线为 `D-31` 至 `D-2`。不能用睡眠日当天、睡眠结束后发生的活动解释已结束的睡眠。

两个比较均要求30个基线日全部可用。分母为零、目标日未知或基线不足时，变化比例为空。
公式为 `(目标日负荷 / 前30日日均负荷 - 1) * 100%`，同时保存有效天数和日期范围。
没有更早存量数据时，默认7/31窗口只保证最新活动日自身基线；首次完整覆盖7个睡眠日的关联需7/38窗口。

只有活动分页完整、当天有可用日汇总且没有活动记录时，才把“API中没有已记录活动”的负荷记0。
这不证明用户休息、设备无漏记或同步绝对完整。有活动但任一负荷缺失时整日总负荷保持未知，
已知部分另列，不能把缺失活动负荷补0。
Web视图的`load_comparison`是当前所选睡眠日的关联比较，不是CLI报告的截止活动日比较；它明确返回`target_date`及基线范围。
展示保留旧值时可返回旧值计算的比例，但整体标`stale`，并分开返回`baseline_valid_days`和`baseline_fresh_days`，不能称为新鲜完整结果。

看板夜均心率优先使用已核实的睡眠 `avgHeartRate`；若后续自行计算，必须注明区间、夜醒是否纳入和覆盖率。
Garmin分期属于设备估算；当前缺少区间时展示阶段总量和明确缺口，不制作虚构时间线。

阶段累计与总睡眠秒数不强制改成相等。当所有阶段为整分钟、三阶段和等于总睡眠向上取整到分钟，
且差异严格小于60秒时标记 `sleep_summary_minute_rounding_consistent`（info）；
这表示数据与分钟/秒精度差异相容，不证明Garmin采用某一具体舍入算法。
其他不一致保留 `sleep_summary_totals_differ`（warning）。显示“约”与来源精度，
不用调整某一阶段凑齐总量，也不据此分配时间区间。

## 只读API与显示降采样

API共同字段为`contract_version="1"`、`timezone`、`generated_at`、`data_as_of`、`quality`。
`generated_at`是视图生成时刻，`data_as_of`是本地最近成功数据时刻，不等于手表测量时间或手机最后同步时刻。
数值带`value/unit/state/source_resource/source_field/source_date/fetched_at/reason/quality`；来源日、抓取时刻和实际测量时刻不可混用。
state按观测区分`available/missing/invalid/stale/unavailable`；具体字段缺失和整次请求失败分别说明。看板不再按当前日期产生`pending`或`current_day_provisional`；前端可兼容既有`pending`响应，但不能将它解释成人工确认流程。
最近请求失败且留有旧值标stale；状态页36小时未见成功数据给新鲜度提示，不推断用户忘记同步。

| API | 主要合同 |
| --- | --- |
| `/api/v1/nights` | 1—92日窗口，limit为1—92；摘要列表含无记录日，返回`next_cursor/available_dates`，支持[历史复盘](#历史复盘) |
| `/api/v1/nights/{D}` | `cards/sleep/stages/activities/load_comparison/is_provisional`；无该来源日Garmin记录且回退窗口无环境记录时返回404；缺睡眠但有环境记录可展示`environment_only`详情 |
| `/api/v1/nights/{D}/timeline` | `window/series/events/stages`；from/to成对、带时区，最多48小时，每序列2—2000点上限 |
| `/api/v1/status` | 资源尝试/成功、脱敏错误、暂停时间、能力、存储计数及本地备份状态 |

睡眠摘要优先`sleep`再`sleep_summaries`；昨夜HRV优先`hrv`再`hrv_summaries`。回退保留来源标记，不把多来源重复求和。
时间轴优先睡眠窗口前后各1小时；窗口不可用时回退前日18:00至当日12:00并标原因，不把回退窗口当真实睡眠。
`series_type=native/hourly_average`分开；小时序列`aggregation_window_seconds=3600`，未知原生窗口为空。
鼠标联动只匹配容许时间差内的实际点，显示真实时间与时间差；超出容许范围显示无观测。活动/午睡为独立事件。

超过显示点上限时，按`floor(max_points/2)`时间桶保留每桶有效min/max及真实时间，附`bucket_valid_count`；空桶不造点。
`display_resolution`返回方法、原始/有效/显示点数和桶宽；稀疏序列直接返回。数据不插值、事件不被降采样删除、raw和标准化记录不变。
睡眠`stages`累计量与timeline区间分开：当前官方数据只有前者，后者返回空列表；不把深/浅/REM累计条当夜间顺序。
浏览器使用散点并支持框选/平移/缩放；只有真实区间存在才可画分期，来源没有区间时明确降级。

页面入口为`GET /sleep`和`GET /sleep/`；根路径`/`的GET/HEAD在合法Host下返回404（含查询参数、匿名或已登录），无Location和WWW-Authenticate。静态资源、业务API、设备POST及`/health`路径不变。

页面查询只读，浏览器同源写入仅限晨起睡感，响应不缓存；应用HTTP Basic校验私有PBKDF2哈希，Caddy保留Authorization。HTTPS认证后可签发`__Host-sleepscape_session`：固定30天（2592000秒）、Secure、HttpOnly、SameSite=Lax、Path=/、无Domain；使用到期时间、随机nonce和HMAC签名，不含密码或健康数据。签名绑定当前私有密码记录，重启不失效，改密码记录立即失效；到期或篡改拒绝认证，访问不滑动续期。显式Authorization优先校验，设备POST不接受浏览器Cookie授权。
未设密码仅允许本机直接访问，远程健康数据请求需要HTTPS；`/health`不含健康数据。`/docs`始终关闭，debug仅可开放受保护的`/openapi.json`，默认关闭，不加载CDN；云端TLS与代理验收单独记录。

## 备份状态合同

`db-backup`在线复制SQLite到新UUID文件，核验完整性、外键和raw；`db-restore-check`只在临时目录恢复检查，不覆盖运行库。
两者成功写私有`backup-status.json`，仅含`last_success_at/status="verified"/record_count`；状态API只返回`state/last_success_at`。
这只表示该次手动SQLite备份的最近核验成功，不证明副本离机、已加密或当前仍存在。
P6成套备份、受控保留、磁盘准入和路径/SSH导出已实现，云端04:00定时器已启用；它们使用独立的维护状态合同，不由此处的`backup-status.json`推断。手动SQLite备份不参与自动删除；离机目标尚未配置，具体合同见下文[自动维护运行合同](#自动维护运行合同p6)。

## 环境采集合同

本节是固件、USB本地接收、写入API与独立环境存储的共同合同，HTTP设备入口默认关闭；各路径的实现和实机验收状态以[核验报告](verification.md)为准。UTC索引、只读查询和P3看板已实现。严格机器校验在`domain/environment.py`，可重放合成信封见`tests/fixtures/environment-batch.json`。
合同按测量能力定义，不绑定某一硬件型号；当前测量配置示例为M5 Atom Voice（C008-C）、Gravity AHT20（SEN0528）、VEML7700（SEN0228）及被动分线模块DFR0759。两传感器使用同一条3.3V I²C总线，SDA21/SCL25、100kHz。
只启用温度、相对湿度、光照、相对声音；首版没有CO₂或VOC推算CO₂通道。替换型号必须登记新测量配置并验证对应驱动。
采购、接线和资源可行性见[硬件方案](plans/hardware.md)。本节维护字段与口径，不重复采购步骤。

### 来源隔离、配置与身份

环境观测、时间锚点、测量配置、写入审计分别保存，使用独立环境表；不写入、覆盖或重新解释Garmin事实表。
查询服务根据同boot锚点生成独立UTC投影，保留原始单调时间和校时锚点，P3页面读取该投影。睡眠归属日期仍采用Garmin规则；环境自定义窗口不推断入睡时间。

- `device_id`是登记后的设备标识。HTTPS请求由设备令牌绑定，客户端不能用一个设备令牌替别的设备写入；USB本地采集由明确选定的串口及会话标识绑定，不要求网络令牌。
- `boot_id`每次启动生成新的随机标识，断电后不能复用。单调时钟只在同一`boot_id`内有效。
- 记录身份为`(device_id, boot_id, record_type, seq)`；`seq`为该启动周期、该记录类型下递增且不复用的非负整数，最大`2^53-1`，不能倒退或复用。
- `measurement_profile_id`引用不可变测量配置：硬件型号/修订、传感器通道与单位、固件版本、采样与聚合策略、PDM转换/滤波、固定增益、事件算法/阈值、摆放位置与校准信息。配置必须先登记；未登记配置不进入正常展示。
- 更换硬件、增益、滤波、采样率、事件算法或位置均生成新配置；配置变化时关闭当前窗口，以`partial_window`标记，禁止一个窗口混用两个配置。
- 主板、传感器或配置可替换，历史观测及旧配置仍保留作为测量证据；这不是保留旧版程序。dBFS不能跨配置默认作绝对比较。

### 上传信封和认证

环境统计批次的设备写入口为`POST /api/v1/ingest/environment-batches`，JSON请求使用`Content-Type: application/json`。USB本地路径通过本机程序调用同一校验和存储，不开放新的网络写入口。
`schema_version=1`是JSON整数，独立于当前只读API的字符串`contract_version="1"`。请求允许补传和乱序，不以到达顺序作为采集顺序。

| 信封字段 | 约束 |
| --- | --- |
| `schema_version` | 固定整数1；不支持的版本明确拒绝，不静默按当前版本解读 |
| `device_id` / `boot_id` | 非空设备/启动标识；单批只允许一个设备和启动周期 |
| `records` | 分钟和事件记录数组；`record_type`分别为`minute`、`event`，最多各60条 |
| `time_anchors` | 独立时间锚点数组，最多60条；无锚点时为空数组 |
| `device_status` | 可选、每批最多一个有界设备运行快照，与观测分开保存，仍计入64 KiB请求体 |

单次请求体上限为**64 KiB（65,536字节）**，包含整个UTF-8 JSON信封；不接收压缩请求体。
即使条数未达上限，超过字节上限仍须继续拆批，按字节优先。设备按固件的队列调度上传已完成记录，断网后有界退避重试，恢复后分批补传。
空请求、未知记录类型、非有限数、字段类型/单位错误、非法计数和超出容量的字符串均拒绝；标识最长64个ASCII字符，单记录质量标记最多16个已登记代码，不接受自由文本日志作为记录。

写入口只在HTTPS下接受专用设备Bearer令牌，令牌不放URL、不写日志、不进入Git。
看板HTTP Basic/会话Cookie与设备Bearer分别校验：设备令牌不能读健康数据，看板密码不能写设备数据。
设备认证仅用于环境批次和[声音事件回听](#声音事件回听)定义的两个精确 POST 路径。浏览器的同源晨起睡感 PUT 单独鉴权。
现有UI、静态资源和只读API继续按原访问规则运行，不开放跨域。

### USB本地传输

USB和HTTPS复用同一`EnvironmentBatch`及逐记录ACK合同、原始记录身份、质量标记和SQLite环境表；不生成另一套临时采样格式。USB只传环境统计值，绝不传PCM录音或Garmin数据。

- `usb_start`显式指定`device_id`和`profile_id`，启动无需网络凭据的RAM会话；同一身份重复启动幂等，不在同一启动周期切换身份。与联网上传任务互斥，避免两个消费者同时推进队列。
- `usb_poll`有界读取同设备、同启动周期、同测量配置的最多4条已提交队列记录；响应携带`batch_id`及完整信封。未确认批次可重读。
- 主机严格校验信封、身份、档案和类型后，以现有SQLite事务提交；提交成功后才发送`usb_ack`，携带相同`batch_id`和逐记录ACK。失败、中断或格式错误不确认、不删除设备队列；重读已保存记录走原有幂等逻辑。
- 主机的时间锚点另存、另确认，不能混入设备批次ACK；当前启动时钟不能用来解释队列里其他`boot_id`的旧记录。
- `usb_stop`用于结束本地会话并复位回等待配置状态，保留已提交Flash队列；尚在RAM内的未定稿窗口可能丢失，不伪造完整末分钟。拔线断电也无法继续采集。
- 本机登记保留不可变测量配置。仅USB设备的`token_hash`使用`local-only:`前缀哨兵，与HTTP认证计算的SHA-256值不可能匹配；不生成、传入或保存网络令牌，不启用HTTP写入口，也不能借本地登记重新启用已撤销设备。后续联网时显式轮换/生成真实设备令牌。

串口连接存在不代表采集正在运行，采集程序存在不代表已有完整分钟；须分别核对启动、收到的通道计数、数据库提交及设备ACK。运行时Mac需保持唤醒和USB连接，不能把这条路径写成已实现无人值守云端采集。

### 单调窗口、时间锚点与UTC映射

每条记录保存`measurement_profile_id`、`seq`、`record_type`、`mono_start_ms`、`mono_end_ms`和`quality`。
时间是从本次启动开始的单调毫秒数，使用64位整数并限制在JSON安全整数范围；窗口为左闭右开，结束必须晚于开始。
当前固件按启动单调时间的整分钟边界分桶，窗口长60,000 ms；首次不足一分钟仍保存该整分钟窗口，并通过通道计数和质量标记缺测，不补零。校时不重新分桶。USB改配置会重启并生成新boot_id；正在RAM中尚未定稿的窗口不保证掉电恢复。协议也允许带partial_window的短窗口，但不能将此能力写成当前固件已抢救未落盘尾段。
`received_at`由接收端记录为数据入库UTC，绝不代替采集时间。

时间锚点包含`anchor_id`、`monotonic_ms`、`utc_at`、`source`、`uncertainty_ms`和质量代码：

- `utc_at`明确带`Z`，`source`只接受实际来源`ntp`或`host_usb`；`uncertainty_ms`是非负误差估计，没有可靠估计时为null，不能填0伪装精确。
- `host_usb`由Mac与当前启动周期串口握手关联，不称为设备NTP校时；主机时钟准确度未经独立验证，误差保持null，查询只能按估计时间使用。仅接收到旧批次不能为其旧启动周期新增当前时钟锚点。
- 锚点身份为`(device_id, boot_id, anchor_id)`，内容不可变；校时得到新结果时追加新锚点，不修改原记录或旧锚点。
- 只读查询映射返回`utc_start/utc_end`、`time_quality`、`time_uncertainty_ms`、所用锚点引用、`time_mapping_algorithm`、`time_mapping_version`和`time_index_revision`。`time_quality`区分`synced`、`estimated`、`unknown`、`conflict`；未经校时的原始记录不编造UTC。存储读取保留原始锚点ID，公开视图只返回稳定匿名引用。
- 同一启动周期内可根据锚点与单调差值估计UTC；离锚点越远，须考虑已验证的时钟漂移。未验证漂移时误差保持未知并标`estimated`，不声称秒级精确。
- 多锚点不一致或UTC跳变时保留全部证据，标`conflict`并暂停冲突区间精确叠加；不得因NTP调整产生重复分钟、倒序窗口或覆盖旧值。
- 无电池RTC的设备冷启动且无网时为`unknown`，不沿用上一启动周期的单调映射。只有该启动周期后续获得有效锚点才可能补映射；无锚点周期永不借接收时间伪造采集UTC。
- 已同步后断网的记录可以估计时间，但48小时缓存能力不等于48小时精确时钟保证。未知UTC记录保留并计入查询和P3状态摘要，不绘制到时间轴。

独立映射使后续校时可改善查询时间而不改变记录身份或原始载荷；重复上传始终比较原始内容，避免“同ID、校时后改UTC”的幂等冲突。

### UTC派生索引与只读查询

当前算法`boot-fixed-offset-1`按`device_id/boot_id`隔离：以单调时间最早的可用锚点为固定偏移参考，时间相同时按锚点ID确定顺序；不逐分钟切换最近锚点而造成重叠或倒序。锚点带质量警告时不建立映射。UTC计算保留微秒，映射范围为2000年至2100年，2100年起点仅可作右开端点；超范围记录保持`unknown`。

两锚点偏移差超过**2,000 ms工程跳变阈值加双方已声明误差**时视为冲突；无误差估计不等于准确度为0，也不作为无限容忍跳变的理由。算法检查全体锚点误差区间，避免宽误差参考锚点掩盖其他锚点间的矛盾。当前保守地将整个boot标`conflict`并停止UTC叠加，保留参考及冲突见证；不猜测哪个锚点正确。该阈值不是精度校准，长期未验证的漂移也可能触发冲突。尚无经验证的振荡器漂移模型，因此现阶段有UTC的窗口均为`estimated`且`time_uncertainty_ms=null`，不输出`synced`或秒级精确声明。

原环境schema保持1。显式迁移新增`environment_index_schema`、`environment_index_boots`、`environment_time_index`，不修改Garmin或原环境事实。每个boot保存来源修订、已索引修订、映射版本和有界映射摘要；记录、锚点及状态插入触发来源修订推进。兼容新锚点或补传只更新必要投影和索引修订；参考偏移或冲突状态改变时重建受影响boot，映射版本推进。新配置记录保留独立配置身份，不混合声音统计。查询只接收来源与索引修订一致的boot，未完成回填的旧UTC不作为当前结果返回。

首次启用与强制重建先创建并恢复核验备份；只通过显式用例执行，启动、GET和doctor不迁移。已启用索引时，事实、来源修订和UTC投影在同一接收事务提交后才ACK；索引失败使整个批次回滚。重复批次不重复保存事实，可修复中断遗留的待索引状态。备份覆盖新增表，并校验来源引用、投影与纯计算一致性。操作入口见[运行手册](operations.md)，真实库是否已启用须看实际执行记录，影子核验不等于源库已迁移。

`EnvironmentReader`以独立只读事务提供最多48小时、最多10,000条记录的有界查询。协议中分钟与事件窗口均不超过60秒，因此UTC索引限定`start−60秒 ≤ utc_start < end`并再筛选`utc_end > start`，保留跨窗口起点的记录，不扫描此前全年记录；多取一条检测截断，截断时不计算缺口或覆盖。原始UTC区间、实际粒度、通道质量和完整分钟统计保持原样，不把裁剪交集伪装成重新聚合结果。

`EnvironmentViewService.timeline/status`是P3复用的白名单服务。输出匿名设备、boot、配置/摆位与锚点引用，分钟指标或受限事件、配置边界、缺口及重叠。覆盖仅表示已映射分钟窗口的时间并集，绝不等于四通道齐全或P4的ready；多来源不自动选源、跨配置不合成全夜均值。未知、冲突、待重建计数明确标`all_history_not_window`，不能推断其属于请求夜晚。

设备状态区分最近接收、最后可映射采样和最后收到的缓存快照。近期接收提示阈值为15分钟，仅用于新鲜度，不表示设备仍在线；`currently_collecting=null`。历史状态补传可能成为最后收到的快照，必须保留该含义；未迁移时不为获取最新状态扫描全库。最后映射采样按有界boot元数据与索引取得，超出查询保护上限明确降级，不返回伪造的最新状态。

### 看板环境投影（P3）

`GET /api/v1/metrics`返回`version/items`；唯一清单在`domain/metrics.py`，保留Garmin既有ID并加入`temperature_c`、`relative_humidity_pct`、`illuminance_lux`、`sound_relative`。注册项包含`id/label/unit/source/color/precision/default_visible/axis/series_types/aggregation/missing_policy`。同一注册驱动API序列、前端图例、纵轴和URL，未知URL项忽略。

统一夜晚timeline增加环境`series/events/environment`，独立`/api/v1/environment/timeline`复用同一投影，必填from/to、带时区、最多48小时，每序列`max_points`范围2—2000。每次最多8个来源/boot组、100个声音事件、32个来源，缺口/重叠各最多128段；上游最多10,000记录。所有截断明确标记，截断读集不计算完整覆盖。统计范围为返回的原始窗口，不因画面降采样而改变分母，也不作为睡眠完整性。

来源/boot组超过8个时，按本次查询读集内各组分钟记录的最新采集`utc_end`从新到旧选取8组，同刻按`(source_ref, boot_ref)`确定顺序；选择后保留原查询组序及各组原始点序。不得按接收时间选取，避免迟到的旧记录挤掉最新采集。`series_truncated=true`仍明确表示组数截断，不将未展示旧组解释为采集失败或删除；缩小自定义查询窗口可查看这些旧组。此规则只控制投影，原始存储和其他读取限额不变。

环境点的`timestamp=window_start`只是绘图定位，同时返回原始`window_start/window_end`、min/max、通道状态和有效/观测/预期数、质量、估计误差、received_at以及匿名source/boot/record/segment引用。显示降采样选桶中最低min和最高max对应的原始窗口，不创造瞬时值或新均值；前端只在真实窗口内匹配悬停，不跨缺口借值。不同配置/boot单独成组，缺值断线。

声音`minute_mean`为energy_mean_dbfs，`minute_peak`为max_1s_rms_dbfs，两者共用相对dBFS轴；点保留采样覆盖、DMA/削波和检测器覆盖/计数。事件返回start_at/end_at包络、活跃时长、峰值/基线及合并/持续/不完整标记，`envelope_is_continuous_sound=false`，不能把包络内间隙解释为连续声音。

设备状态展示最后映射分钟的通道健康与已知硬件/固件、不可变配置数值，不原样回显私有自由文本标识。最后接收快照保留其接收时刻、积压/丢弃/隔离计数与scope，不能当作实时设备状态。`status.latest_night`仍指Garmin最近睡眠日，`latest_environment_capture`独立用于定位试采；环境读取异常用unavailable隔离，不中断Garmin视图。所有新增GET沿用Host、密码、HTTPS、no-store保护，不写库、不触发采集或模型。

### 分钟指标与逐通道覆盖率

`minute`记录的`metrics`只包含已登记通道，至少保留以下字段；尚未接入通道应明确`unavailable`，不能用0表示未安装。

| 通道与采集频次 | 分钟值 | 数量、覆盖率和边界 |
| --- | --- | --- |
| `temperature_c`，每10秒 | 有效观测的`mean/min/max`，单位`degC` | 完整分钟`expected_count=6`；分别记录`observed_count/valid_count`；不使用上一分钟数值填补 |
| `relative_humidity_pct`，每10秒 | 有效观测的`mean/min/max`，单位`percent` | 完整分钟`expected_count=6`；温度失败不自动判定湿度也失败，反之亦然 |
| `illuminance_lux`，每1秒 | 有效观测的`mean/min/max`，单位`lx` | 完整分钟`expected_count=60`；0可为有效量化读数，但不证明绝对黑暗；超量程另标，不用最大值替代 |
| `sound_relative`，16 kHz单声道16 bit PCM持续采样 | `energy_mean_dbfs`、`max_1s_rms_dbfs`，单位`dBFS` | 先计算完整1秒RMS窗口，再做60秒汇总；完整分钟预期60个1秒窗口及960,000个PCM样本 |

每个通道分别保存`state`、`expected_count`、`observed_count`、`valid_count`和质量代码；`0 <= valid_count <= observed_count <= expected_count`。
重复读数不能增加计数。部分分钟按实际窗口内应有的采样槽确定预期次数，不能把分母仍固定为完整分钟。
采样覆盖率为`valid_count/expected_count`，无预期槽时为null；这只表示按配置取得数据的比例，不证明温湿度在两次读数之间保持不变。
每个通道无有效读数时值为null，其他成功通道正常保留。逐通道区分缺测、校验失败、超量程、总线错误和暖机。

声音另存`expected_samples/observed_samples/valid_samples`、`valid_duration_ms`、`valid_1s_count`、`dma_overflow_count`、`clipped_samples`。
任一丢样的一秒窗口不进入事件检测和完整窗口汇总；原始样本覆盖率、可用1秒窗口覆盖率分别返回。
原始 PCM 在短 RAM 缓冲中用于统计；联网固件可从中编码事件片段。原始 PCM 不写Flash、不上传、不输出串口，不启用含音频RAM的Flash core dump。
固定增益，不启用AGC、语音增强或动态降噪；处理链改变必须换测量配置。

令有效PCM归一化幅度为`x/32768`，每个有效1秒窗口的均方能量为`E_i=mean((x/32768)^2)`，
`RMS_dBFS_i=10*log10(E_i)`；`energy_mean_dbfs=10*log10(mean(E_i))`，不能直接算dB数值的算术平均。
`max_1s_rms_dbfs=max(RMS_dBFS_i)`仅是1秒RMS最大值，不是瞬时峰值声压。实现采用足够精度的累加器，防止平方和溢出。
全零且完整收到的PCM窗口仍以零能量参与分钟能量平均，单秒dB值记null并标`below_numeric_floor`，不输出`-Infinity`；整分钟均为零时分钟dB值也为null。这与未收到PCM的`missing`不同。
该值是当前设备和处理链下的相对数字强度，不能改名为dBA、用于WHO限值判断，或默认跨设备比较。

### 当前光照测量配置

SEN0228 档案使用 `light_sensor="dfrobot-sen0228-veml7700"`，固件版本按实物登记。仓库样例对应 `0.4.0`。唯一可复制样例为
[`firmware/profile.example.json`](../firmware/profile.example.json)，新增的`light_measurement`和`i2c_bus`随不可变档案保存。
当前组合的完整编译、原生驱动逻辑、接收回归和实物读数验证状态以[核验报告](verification.md)为准；短时可读不代表已完成校准或整夜稳定性验收。

| 配置 | 当前值与含义 |
| --- | --- |
| `gain` / `integration_ms` | 2倍增益、400 ms积分；每秒读取一次 |
| `als_conf` | 2176，即0x0880；与固定增益/积分时间对应 |
| `power_saving_enabled` / `auto_range` | 均为false；连续工作、无自动换量程 |
| `resolution_lux` / `conversion` | 0.0084 lx/count；16位小端原始计数乘该系数，线性换算 |
| `saturation_raw` | 65535；达到该计数即饱和，该次无效并标`out_of_range`，不写最大值或0代替 |
| `range_min_lux` / `range_max_lux` | 0至550.4856 lx，对应原始计数0至65534；不是0–120 klx全量程 |
| `low_light_limit_lux` | 0.1 lx；有效值低于它时保留读数并标`low_light_limit`，是工程提示，不是准确度或检出限认证 |
| `reference` | `vishay-84286-rev1.8-84323-20250306`，记录本次换算依据 |
| `i2c_bus` | `topology=shared`、`hub_model=dfrobot-dfr0759`、SDA21/SCL25、100000 Hz、3.3V；温湿度地址56（0x38），光照地址16（0x10） |

启动和传感器恢复初始化后须等待至少403 ms（400 ms积分加3 ms启动时间）；项目按1秒轮询，暖机不足时不产生假0或有效读数。
400 ms积分表示每秒取得一段积分结果，不能把60个有效秒槽理解为整分钟无间隙捕获所有短促闪光。
饱和标记为`out_of_range`及`sensor_error`，I²C失败为`bus_error`及`sensor_error`，正常启动预热为`warming`；三者均不进入有效统计。有其他有效样本时保留其分钟mean/min/max，并同时标明有效计数和质量。
整个分钟没有有效光照读数时数值为null。低光提示按该分钟有效值中的min是否小于0.1 lx判断，其他数据不能默认丢弃或归零。

换算依据为[Vishay数据表84286 Rev.1.8（2024-11-28）](https://www.vishay.com/docs/84286/veml7700.pdf)与
[应用说明84323（2025-03-06）](https://www.vishay.com/docs/84323/designingveml7700.pdf)。
DFRobot旧资料中的0.0036 lx/count以及旧库系数不作为当前实现参数；量化步长不等于测量绝对误差。当前固定档避免自动换档造成解释不明，
代价是较强室内光/白天可能超量程；需要更宽量程时另定配置和驱动规则并重新验证，不回写已有观测。

上传`schema_version=1`及环境SQLite事实表保持不变。旧测量档案缺少上述两对象时继续保留原样和原哈希，不补写伪造的参数；
新注册的VEML7700档案必须包含完整两对象。C008-C 固件 0.2.0、0.3.0 和 0.4.0 的档案均核对表中固定参数；不能登记另一套虽数学自洽却不符刷入程序的增益、量程或总线参数。接收端按已登记档案验证有效光照范围及低光标记一致性；历史无光照配置对象的记录不套用新阈值，缓存中已写入的质量标记不被新固件追溯重算。
更换硬件、光照档位或摆位必须使用新的`measurement_profile_id`；旧档案和已采数据属于历史证据，不能作为旧版程序清理。

### 相对声音事件（工程默认，待真机验证）

`event`记录只描述相对声音变化，不识别人声、打鼾或声源，不输出导致觉醒的结论。
事件算法使用固定版本和以下默认参数；这些阈值是为实现和验收制定的工程规则，**不是科学证实的睡眠影响阈值**。

1. 使用当前1秒窗口之前最近60个有效、有限dBFS的1秒窗口中位数作为背景值，不把当前窗口放入自身基线；启动先积累60个有效窗口，期间不产生事件。缺失窗口不补0；至少需要60秒，存在缺失时暖机时间更长。
2. 当前值相对背景升高至少10 dB开始，开始时冻结本次事件的背景值；至少一个有效1秒窗口才能形成事件。事件期间继续维护滚动历史，下一事件重新取当时背景。
3. 相对冻结背景低于6 dB连续3个有效秒时结束，结束点取这3秒开始处；该尾部安静段不计入事件活跃时长。缺失或非有限窗口立即结束当前事件并标`incomplete`，重新积累连续60个有效基线窗口后才恢复检测。
4. 相邻候选事件间隔不超过2秒时合并；跨分钟事件按单调分钟边界拆成可关联片段，保留连续/截断标记。分钟记录与事件计数延后3秒定稿；正常情况下断电可能损失尚在RAM的最后约63秒，不能伪造“恢复”这些观测，不改写已确认载荷。
5. 每分钟最多保存1条事件摘要。多个片段强制合并时`merged=true`，保存包络起止、`active_duration_ms`及`max_1s_rms_dbfs`；活跃时长只累计真实候选片段，不把片段间空白计入持续声音。摘要不包含片段细节时，UI不可把整个包络画成连续噪声。

分钟的声音通道保存`detected_count/stored_count/merged_count/dropped_count`：`detected_count`为按2秒规则处理后、切入该分钟的候选片段数；
`stored_count`为最终事件摘要条数（0或1），`merged_count`为被纳入摘要但合并掉的额外片段数，`dropped_count`为因容量限制未纳入摘要的片段数，
须满足`detected_count = stored_count + merged_count + dropped_count`。持续事件每分钟都可有一个片段，跨分钟相加不称为独立声源次数。
事件摘要另带`event_kind="relative_sound"`、`baseline_dbfs`（多背景合并时为null并标明）、`threshold_profile_id`、`merged`、`incomplete`及质量代码。
另存`event_detector_state`（`warming/ready/interrupted`）与`event_valid_duration_ms`，区分音频有读数和事件检测已经可用。没有可用检测区间时计数为null/未知，上述计数等式只对已知计数适用；部分可用时只统计可用区间并标覆盖不足，不能写成“整分钟0次、安静”。亮灯事件首版不单独检测，先展示光照分钟min/max与实际读数范围。

### 幂等、ACK、缓存与错误

服务器先完整校验批次，再在一个数据库事务内提交新记录、新锚点及设备状态；包含非法记录或同ID不同内容的批次整体拒绝，不进行静默部分写入。
以确定性规范化JSON计算内容哈希，忽略对象键顺序，不改写数值语义；原始环境记录和锚点不可变。
同身份同内容返回`duplicate`；同身份不同内容返回HTTP409 `record_conflict`、`anchor_conflict`或`status_conflict`，已有数据保留。
未知配置、非法字段或不支持的schema返回422；无效认证401、设备绑定错误403、体积超限413、容量/速率压力429或暂时故障503。
错误响应只含稳定代码和本批记录身份，不包含令牌、PCM或私人载荷。

只有持久化事务提交成功才返回HTTP200 ACK，逐项列出`record_type/seq/status=accepted|duplicate`和`anchor_id/status`，并包含设备、启动标识与服务器接收UTC；有设备状态时另确认`status_seq/status`。
正常退队只处理ACK明确确认的对应缓存项，不能用“最大已收seq”跳过中间缺失项；ACK丢失则原样重发，不重新采样、生成新ID或修改时间字段。永久拒收的例外为下述独立持久隔离，不能称为上传成功。
遇到400/409/413/422先缩至单条确认拒收，再将完整来源及原JSON、错误状态和CRC写入独立64KiB隔离文件，fsync成功才从待传队列移出，其他记录继续上传；这不计为服务端ACK。隔离满/受损时保留该条，暂停前移并提示需处理，本地采集继续。429/503按有界退避重试；认证失败每5分钟重试，继续本地采集。隔离文件的自动修复/重放与远程维护尚未实现，须保留证据人工排障。

断网缓存目标48小时，以实际编码后的记录、索引、CRC、事件、日志和文件系统占用验收；容量预算统一见[硬件方案](plans/hardware.md)，不把本地二进制长度当作JSON长度。
分钟与受限事件共享1MiB有界队列（最多64段，每段最多128条），隔离文件另限64KiB；没有自由文本运行日志写入采集队列。当前每分钟最多1条合并事件，已进入合并摘要的片段不算丢弃。已定稿记录不能为更新丢弃计数而重写，后续淘汰用独立`device_status`记录。
设备状态保存`status_seq`、`monotonic_ms`、缓存待传条数、按记录类型区分的累计丢失数、`quarantined_count`和最多16个丢失范围。当前固件每5分钟生成快照，保留累计丢失量；没有可可靠重建的逐boot/seq范围时输出空数组且`ranges_truncated=true`，不编造范围。隔离计数独立于缓存淘汰数。
状态身份为`(device_id, boot_id, status_seq)`，每次生成新快照时递增；相同身份内容不可变。它不进入Garmin时间轴、不能携带自由文本日志，也不把服务器尚未接收误判为采集失败。
缓存超过已验证保留范围时按最旧段优先淘汰，累计各类丢失记录数；当前没有完整逐条丢失时间范围，明确标范围不全。设备CLI和P3状态页已展示接收、最后映射采集及缓存快照；不能将传输延迟等同采集缺失，也不能把全历史丢弃量归入某晚。
分钟记录提交Flash后才属于断电可恢复数据；写入应有版本、校验和与可恢复边界，启动扫描不能把半条记录作为有效观测。
音频DMA、网络和存储分开调度，但不能据此承诺Flash擦写绝不影响音频；实现须统计丢样，并验证接近容量上限、TLS重连、断网补传和断电恢复。

## 分析边界

- 入睡前运动、活动和压力与睡后数据分开，不能把结果发生后的数据当原因。
- 晨起评分、Body Battery等综合指标可能包含同夜睡眠/HRV，不能作为独立原因。
- 声音或亮灯可能发生在醒来之后，时间重合不证明环境导致觉醒。
- 主观恢复感、咖啡因、酒精等生活记录仍有帮助；新增LLM解读按下节双源门槛执行，不输出确定因果结论。
- 环境设备未校时记录不得冒充准确时间，补传保留采集时刻和校时状态。
- 未校准声音指标不标为准确dBA，不用于判断是否符合专业噪声标准。
- 原始响应与标准化一致只证明程序转换一致，不证明手表测量或睡眠判断准确。

## 模型、完整性与报告栏目状态合同

GET /api/v1/nights/{D}/analysis沿用访问保护、日期边界及no-store；只读当前资格与已保存报告，不采集、不调用模型、不迁移/写库、不读取Key正文。来源局部失败不能清空已有报告或其他曲线。

| 字段 | 当前合同 |
| --- | --- |
| night_date | 请求的睡眠来源日D |
| implementation_stage | 固定reports |
| data_state | waiting_data / ready / expired_unanalyzed；当前资格不可读时not_evaluated |
| readiness / readiness_unavailable | 原分级检查白名单或null，配套布尔读取状态；含policy、quality_state、warnings、missing、optional_missing及覆盖统计 |
| missing | 当前阻塞必需缺项，与readiness.missing一致；不可读时为空不等同齐全 |
| execution_state | not_initialized / not_configured / disabled / invalid_configuration / waiting_data / expired_unanalyzed / queued / running / failed / discarded_input_changed / blocked_budget / blocked_attempts / blocked_input_budget / unavailable / complete等固定安全状态，独立于数据质量 |
| automatic_enabled | 日任务报告自动开关布尔值；不授权GET外呼 |
| attempt | 该夜晚最近一次尝试的status、固定error_code、retry_after截止时刻，以及跨输入、跨日累计的attempt_count；无尝试为null，不输出内部ID/原始错误 |
| model | 安全配置、最近模型请求状态、共享预算和运行元数据；last_check_*可能来自合成或报告请求，不等同已发布报告；无密钥/URL/正文。展示标明共享请求范围，失败原因不推断报告未发布；本晚发布状态依据已校验的report，与最近请求是否失败独立 |
| blockers | 固定初始化/配置/预算/读取错误码，不输出异常正文 |
| next_check | 上海09:00计划，不是定时器实际触发证明 |
| report | 可用的已发布报告白名单或null；字段如下 |
| notice | 固定说明：生成时证据不改写，刷新不调用模型 |

report包含night_date、generated_at、model_ref、prompt_version、input_quality_state、source_changed及source_changed_reasons。content为已校验输出去掉私有input_hash后的对象；evidence仅facts/timeline/events；required_limitations保留生成时限制。公开结果不返回完整模型输入、Prompt、内部input_id/attempt_id/request_id、私有指纹、设备身份或模型配置地址。

source_changed为true/false/null。true表示当前来源指纹已不同；若生成时的完整性策略已更新，安全原因标记为readiness_policy_changed。null及current_data_unavailable表示无法比较，不能冒称未改变。已发布报告读取优先，当前数据暂不可用仍保留可用原结论；原报告损坏时明确unavailable，不展示未验证文本。生成时间与当前检查时间分别显示，报告质量不覆盖当前数据质量。

历史GET /api/v1/nights的analysis_summary只读上次保存检查，含state、checked_at、scope=last_saved_check、unavailable、quality_state、policy_version、current_policy；未保存not_evaluated、失败unavailable。report_summary独立包含state、generated_at和unavailable，已发布为complete；它不逐夜重算来源变化。详情GET不能刷新历史审计或生成报告。

## 每日采集运行合同（P5）

`daily-run`以Asia/Shanghai自然日作为幂等键。`SLEEPSCAPE_GARMIN_LOOKBACK_DAYS=N`允许1—31，睡眠/HRV明细与摘要均在D−(N−1)…D；首日前一天仅额外补心率，独立负荷背景为D−(N+30)…D−1。停机不扩大详细窗口，窗口外补采必须显式指定。采集审计仍保留今日资源的覆盖边界，任务完成不改变上游完整性含义，也不为当日卡片统一设置待确认。

`GET /api/v1/status.daily`与CLI `daily-status`复用只读投影：

| 字段 | 含义 |
| --- | --- |
| `state` | `not_run / available / unavailable`，表示运行元数据是否可读，不代表睡眠已齐全 |
| `lookback_days` | 当前配置的N；`observations`对应当前上海日的N日范围，最近任务有独立window |
| `schedule` | `timezone=Asia/Shanghai`、`time=09:00`、`installation_state=unknown`、`next_planned_at`与`next_run_kind=planned_not_installed`；只给计划，不检查systemd是否安装/启用 |
| `latest` / `history` | 最近任务/至多31个任务日的安全记录；隐藏内部run_id，不包含测量值、凭据、源响应或私有路径 |
| 任务记录 | `day/status/phase/attempt/started_at/finished_at/window/request_counts/error_code/stop_reason/retry_not_before/analysis` |
| `request_counts` | `planned/completed/failed/http`，计划逻辑请求、完成资源、失败资源和实际HTTP分别计数；不可获得的物理次数为null，不拿计划数替代。completed与failed可因无效响应重叠，不用于计算“齐全率” |
| `observations` | 当前窗口逐日`{date,sleep}`；`available / missing / invalid / stale / request_failed / not_collected`，只表达睡眠观测及最新请求状态，不输出健康数值，不代表睡眠窗口闭合、所有Garmin指标齐全或P4双源ready |
| `observations_error_code` | 无法安全读取事实时为固定错误码，观察列表为空但不伪称没有缺项 |
| `analysis` | 状态查询为automatic_enabled/automatic_disabled；显式daily-run结果为独立reports.status，不改变采集终态 |

任务`status`为running/completed/partial/failed/interrupted/deferred；`phase`为preparing/collecting/finished/deferred。completed允许某日API有效空返回，此时该日仍缺失。同日终态默认不再执行；显式`--retry`只重试非completed终态，且不得越过共享Retry-After。被锁/坏状态阻止且未产生任务时，CLI返回blocked。状态GET只读推断已退出进程的遗留running，不写恢复结果；下次显式执行在共用采集锁内持久化恢复。数据库已完成而JSON未终结时，对照run_id恢复，不重复取数。

`storage/daily_runtime.py`管理私有`daily-runtime.json`（schema_version=1，至多31个任务日、128KiB）和`collection-cooldown.json`，原子写入、严格白名单、损坏/不安全权限拒绝执行。裁剪任务摘要不删除SQLite历史审计与原始数据；重试覆盖当日摘要、递增attempt，实际各次采集仍保留SQLite runs。账本与库分开保存，成套迁移与恢复规则见[运行操作](operations.md#每日采集编排)。缺项列表不枚举窗口外所有历史缺口；环境收据不受09:00触发控制。

## 自动维护运行合同（P6）

`GET /api/v1/status.maintenance`与`maintenance-status`复用只读投影；仅读取有界维护账本和磁盘统计，不扫描/校验备份正文，不创建目录、不联网或修复状态。GET失败只影响维护卡，保留其他数据展示。

| 字段 | 当前含义 |
| --- | --- |
| `state` / `error_code` | `not_run / available / unavailable`及固定错误码；损坏不重置，未知不是零备份 |
| `schedule` | `time=04:00`、`timezone=Asia/Shanghai`、`installation_state=unknown`、`next_planned_at`；不证明systemd已经启用 |
| `latest` | 最近任务的`day/status/phase/started_at/finished_at/error_code`；隐藏内部backup_id，状态含running/completed/partial/failed/interrupted，未入账阻止由CLI标blocked |
| `backups` | `verified_count/total_bytes/latest_verified_at/retention_over_budget/legacy_count/legacy_bytes`；为最近登记证据，不在GET重新计算哈希。账本不可读时数量/字节为null，不伪装成0 |
| `disk` | `state/measured_at/volumes/bytes/error_code`；系统与数据盘只返回role、总量、可用字节/比例、水位；固定文件类别为database/wal/shm/runtime，无私有路径。某运行账本大小不可读可为null，不替代其原结构校验 |
| `offsite` | `status/mode/last_success_at/error_code/location_verified/retained_count/retained_bytes/over_budget/location_state/transit_encrypted/at_rest_encryption`；mode为none/path/ssh。disabled映射none；未配置offsite_not_configured，配置变更not_checked，成功verified只代表目标复制核验 |

路径目标`location_verified=false`，不声称已离机；SSH可确认目标连接和传输加密，但不确认静态加密。主机、用户、目录、私钥、配置指纹和健康正文不进入状态。未配置目标的维护可以completed，同时明确没有离机副本。已配置导出失败时为partial/failed，保留旧副本并跳过本地保留清理。

维护账本`maintenance-state.json`使用schema_version=1，最多31条运行摘要、256个受控包登记、256KiB；任务与每日采集分离锁定。成套包清单记录SQLite与已有三份运行账本的SHA-256、字节数、表计数及存在性，运行关联须与同批数据库一致。单个包不包含凭据、.env或整个P6清单。保留决策只删除登记包，保留最新有效副本；未知/损坏文件不认领或清理。完整恢复与一致性边界见[维护方案](plans/maintenance.md#成套备份与恢复边界)。

容量策略在`domain/maintenance.py`唯一维护：预警25%、限制15%或2GiB、临界5%或512MiB，取系统/数据盘更严重者。restricted阻止新模型额度预留和大备份，critical/unknown阻止新事实写入。环境HTTP失败返回503及Retry-After=60且无ACK；已收到请求不等于提交成功。实际磁盘满同样不确认；保留已有只读能力，不删除观测腾空间。此合同不修改环境上传JSON、固件协议或Garmin字段。

## 睡眠分析合同

当前实现分级资格、同快照、N日复查/审计、最小输入、模型调用、唯一报告与来源变化提示。[包内JSON合同](../src/sleepscape/resources/sleep-analysis.schema.json)是当前发送输入与生成输出的唯一机器结构；检查结果仍由其独立合同维护，不混为模型输入。

### 双源完整性门槛

当前唯一计算策略为`dual-source-coverage-2`。分析资格与真实完整程度独立：保留实际缺样；达到以下门槛可以分析并附质量限制。99%与300秒是初始工程参数，不是医学或统计学准确度保证；修改参数须更新策略及验收，不靠界面四舍五入放行。

先确定睡眠来源日D及API已闭合主窗口`[start,end)`；必须`start < end <= 快照截止时刻`且不长于24小时，睡眠时长有效、没有尚在进行标记或未解决核心冲突。日期/is_provisional和HTTP200不能证明睡眠闭合。

| 组 | 放行要求 | 不满足时 |
| --- | --- | --- |
| Garmin睡眠 | 来源日、起止、总睡眠时长、平均心率、夜间平均HRV有效 | `missing_garmin_core`；不拿静息心率或周均HRV替代 |
| 昨日背景 | D−1完整日的运动负荷及平均压力；负荷0须有完整分页和日摘要佐证 | `missing_previous_day`；未知不能补0 |
| 时间与来源 | 相关boot有可用锚点、映射无冲突；同设备/配置/摆位，分钟无重叠/错位 | 原时间/配置错误继续阻塞；接收时间不能代替采样时间 |
| 各环境通道 | 每通道独立满足`usable_count / expected_count >= 0.99`，不合并不同通道的分母 | `insufficient_channel_coverage`；预期计数不可确定也不放行 |
| 局部连续问题 | 每通道连续受影响分钟范围不超过300秒，含整分钟缺记录和部分/无效窗口，按睡眠交集裁边 | `excessive_sampling_gap`；不能用整夜平均掩盖局部连续问题 |
| 数值与结构 | 单位/固定采样配置/计数/统计值自洽；无严重质量错误 | `invalid_channel`等阻塞；不把错误数值计为有效 |

采样分母来自不可变测量配置与真实boot网格，整分钟缺记录也计入预期。分子只使用经过合同与质量验证的有效样本：温湿光是有效采样点；声音是完整且有效的一秒窗口。声音`valid_samples`可能包含已收到却未形成有效秒的PCM，不能直接把它作为覆盖分子；保留`valid_count*16000 <= valid_samples <= observed_samples <= expected_samples`与`valid_duration_ms=valid_1s_count*1000=valid_count*1000`等校验。

只容忍明确计数的`partial_window/audio_gap/sensor_error/missing`等缺样，且部分窗口统计来自有效样本。计数合法且valid=0、统计全null可作为零有效采样缺口；valid>0但缺必要统计、非法单位/计数、数值越界、削波、DMA溢出、CRC错误和超量程等严重错误继续阻塞。低光工程提示与声音事件检测预热不影响完整有效读数的计数。

光照通信异常仅在以下条件同时满足时作为可计数缺样：完整分钟的`expected_count=observed_count=60`，状态为`partial`，其中58或59个样本有效，单位和min/mean/max等原有校验通过；光照质量必须同时含`bus_error`和`sensor_error`，可另含`partial_window`、`warming`、`low_light_limit`。分子只计固件已排除失败读数后留下的有效样本，该分钟计入受影响范围且不计入完全有效分钟。缺口以`light_bus_sampling_gap`保留时间窗、预期及有效数量，满足整晚门槛时作为质量提示。其他通道的总线异常、整条记录的严重标记、计数矛盾或同时出现其他严重标记仍阻塞。

每通道`max_affected_span_seconds`为相邻受影响分钟区间与整分钟缺口合并后的最长跨度。它是保守影响范围，不是精确连续漏采时长；当前分钟统计无法还原每个缺秒的位置。首尾相交分钟的采样计数来自整分钟、分子分母同口径；时长覆盖与连续影响范围才裁至睡眠窗口，保留边界限制。跨boot缺口导致应有网格无法确定时不猜预期数量，不据此通过99%门槛。

达到门槛但仍有缺口时：`core_ready=true`、`data_state=ready`、`quality_state=with_gaps`，轻微缺样列`warnings`及恢复性；没有环境质量缺口为`quality_state=complete`；仍有任何阻塞项为`quality_state=insufficient`，列`missing`。`complete`只描述当前必需数据的质量判定，不代表可选输入全部存在或测量医学准确。缺样不补零、不插值伪造原始数据；明确无法恢复的采样仍为unrecoverable，规则放行不改变该事实。

有一致锚点但误差未知可标estimated并加入clock_error_unknown，不支持秒级先后或噪声导致觉醒的判断。分期累计/时间线、评分、呼吸、SpO2、Body Battery、活动细节、历史基线、事件摘要和生活记录为可选项，缺失列optional_missing，不阻塞核心资格；检测覆盖不足不能写成整夜0次事件。

缺项及质量提示统一使用`code/source/metric/date/window_start/window_end/expected/observed/recoverability/last_checked_at`；恢复性为await_sync/await_upload/unrecoverable/unknown。核心Garmin最新请求失败仅余陈旧值时仍阻塞；可选资源失败只影响对应模块。模型不自行决定是否绕过门槛。

### 当前检查结果与存储

evaluate_readiness(day, night, environment, now, lookback_days)是纯函数；当前策略版本见上节。服务复用Dashboard已有来源选择和负荷规则，并用同一数据库只读事务读取原始环境投影，不消费显示降采样结果。睡眠时长还必须不超过真实窗口；来源日、单位、字段与资源须匹配睡眠/夜间口径，不能以静息心率或周均HRV替代。核心来源最近请求失败而只剩旧值时阻塞；可选请求失败不会污染核心。

window为UTC start/end及duration_seconds，时间非法、倒置或超过24小时为null。未结束但结构有效的窗口可保留用于解释缺项，不能ready。missing和warnings的每项为code/source/metric/date/window_start/window_end/expected/observed/recoverability/last_checked_at；expected/observed仅为有限非负数、null或固定状态码，不回显源错误。昨日负荷、压力和可选活动日期为D−1，窗口为空，不冒用当夜睡眠范围。相邻同类缺项合并区间，数字累加，不跨有效分钟填平；不截断真实缺项。

garmin_summary列必需项数、有效项数、窗口状态及各卡片状态/来源日，不包含请求正文。optional_missing单列阶段累计/分期、评分、呼吸、SpO2、Body Battery、活动细节、历史基线、声音事件与生活背景；来源不支持分期使用unsupported，与尚未记录not_provided区分。

environment_summary包含expected_windows、observed_windows、missing_windows、missing_windows_lower_bound、count_exact、expected_seconds、covered_seconds、missing_seconds、overlap_seconds、gaps和四通道channels。同boot按真实60秒网格数应有窗口，首尾只裁时间覆盖，统计仍来自整分钟。不同boot同配置可无缝衔接；跨boot缺口无法确定网格时，expected_windows/missing_windows为null、count_exact=false，保留精确缺时长与缺窗口下界，不编造启动边界。重叠和同boot错位网格不通过。缺配置/摆位或未选多来源时configuration_changed阻塞，channels各项为null，不把混合读数相加或报成全部传感器故障。

每通道保留expected_windows/valid_windows/invalid_windows、expected_count/observed_count/valid_count/missing_count和coverage_ratio；新增usable_count、effective_coverage_ratio、max_affected_span_seconds。usable_count是质量检查后可用于放行的数量，不能把源声明valid_count中的严重异常当有效；effective_coverage_ratio=usable_count/expected_count，未知为null。coverage_ratio按完整有效分钟与睡眠交集的并集计算；channel.missing_seconds表示未被严格有效分钟覆盖的时间，不是精确丢失音频秒数。声音audio_missing_seconds仅针对已观察整分钟中的已知缺秒，audio_missing_scope=whole_observed_minutes；不声称能在边缘交集中定位坏秒。所有预期计数从不可变采样配置校验，不接受设备自降expected_count来伪装完整。

全局历史unknown/conflict计数不能污染一晚已完整覆盖的有效记录；本晚缺覆盖且有未知/冲突/未更新的历史时间记录时，保留unknown_environment_time_possible限制并追加unknown_environment_time阻塞；无法证明缺口来源时保守不放行，也不能将那些记录强行归入该晚。估计UTC允许参与门槛但加入clock_error_unknown；事件检测预热/检测不足只列可选项，不把完整PCM声音通道判坏。低光工程标记保留，声音不声称校准dBA。

data_state为waiting_data/ready/expired_unanalyzed。当前上海日往前含今日N日是自动回查范围，默认7，范围1—31；窗口外为expired_unanalyzed，但core_ready仍保留实际判断，可能为true，绝不扩窗或自动调用模型。core_ready与阻塞missing为空相对应，warnings和可选缺项不直接否决资格。新增policy包含min_channel_coverage=0.99/max_affected_span_seconds=300；quality_state为complete/with_gaps/insufficient。历史摘要增加quality_state、policy_version和current_policy：旧审计不补造质量字段、不重算旧结论，只显示旧规则及待复查。

持久化是独立显式动作：可选analysis_schema(version=1)、analysis_readiness_checks（不可变检查审计）和analysis_readiness（每晚最新检查投影）三表。analysis-prepare先创建备份并临时恢复核验，再迁移；普通启动、GET和只读检查不建表。记录包含检查结果、策略、时间、run_id、规范JSON摘要与私有source_fingerprint。检查三表schema_version仍为1，无需改变其结构；报告四表按下节独立显式迁移。旧`dual-source-strict-1`与`dual-source-coverage-1`审计按原字段与原哈希读取，两版coverage载荷均须带policy/quality_state/warnings；未知策略或错误字段拒绝，旧算法不保留为可运行分支。重新检查新增不可变记录并切换最新投影，不修改旧检查。同run_id/日期/内容重复保存幂等，内容不同拒绝；当前投影按检查时间和检查ID指向最新审计。每条载荷最多4MiB、单批至多31晚；保存前容量准入，实际写满拒绝，批次不部分成功。旧表、原始观测、检查审计不作为冗余删除。

三表已经纳入SQLite及成套备份的结构、载荷摘要和引用核验；无三表的既有有效备份仍可恢复，半套/错误版本/损坏引用明确失败。持久审计没有默认自动删除策略，长期增长须计入磁盘和备份预算。

检查source_fingerprint代表本次使用的稳定来源和完整性语义，排除抓取/接收/检查时间及无事实变化的索引版本。报告服务在该指纹上加入同快照30日基线的稳定来源/质量，形成报告来源指纹；两者都私有，不能与发送input_hash混用。

### 状态、发布与持久化

数据资格与执行分离。只有自动窗口内ready才构造发送输入；模型配置、自动开关、独立锁、容量和请求/业务预算另检查。CLI显式--date只能位于当前配置N日内，analysis-check仍可检查更旧日期。日任务释放Garmin采集锁后检查并尝试报告，不在Garmin适配器或Web GET中外呼。

当前按2026-11-18用户指令使用qwen3.8-max并沿用私有URL/Key；这是模型配置变更，不改变输入/输出合同或发布门槛。配置指纹与返回模型名须核对，旧模型的连接证据不能继承到新配置；实际报告保留生成当时的模型元数据。

每个上海自然日共享P1最多30个持久物理请求占位，包含合成连接和真实报告，失败/超时/中断不退还。每个night_date跨所有input_hash和执行日期累计最多10次报告尝试，历史尝试继续计数；换输入、换日或重启均不清零。优先今日，再处理窗口内最早合格夜晚，不再额外限制每天不同夜晚数量。成功发布立即停止，该夜后续自动任务跳过；达到10次仍未成功则后续预检返回blocked_attempts，没有重置CLI。可重试网络错误与结构纠错共用夜晚剩余次数及每日总额度；Retry-After≤30秒可等待，较长留待后续且不得提前；认证/权限/模型能力错误停止本轮，不通过删审计重置。最后一次失败仍保留failed及原错误码，不能以稍后的blocked_attempts掩盖本次失败。

独立报告schema版本为1，四表与检查三表分别可选：

| 表 | 必需内容与保护 |
| --- | --- |
| sleep_analysis_schema | 当前报告表版本和创建时间，部分存在/错误版本拒绝 |
| analysis_inputs | input_id、night_date、input_hash、source_fingerprint、prompt_hash/version、schema_hash、model_ref、config_fingerprint、time_mapping_version、created_at、payload_json及payload_hash；按来源/配置组合幂等，记录不可更新/删除 |
| analysis_attempts | attempt_id、input_id/night_date、唯一request_id、started_at/finished_at、state、安全error_code与retry_after秒数；引用输入，禁止删除，只允许running转终态 |
| sleep_analysis_reports | night_date主键、唯一attempt_id、generated_at、payload_json/hash、model_json/hash；引用成功尝试，每晚唯一且不可更新/删除 |

显式重读使用可选表 `sleep_analysis_revisions`，引用首份报告的归属日和本次独立成功尝试，同样校验输入、正文、模型元数据、摘要与外键。事务内核对预期当前报告后追加一条不可改删的修订；详情和历史读取最近提交的修订。只有新 Prompt 的显式单夜重读可发布修订，每次至多一次物理请求，计入既有预算。失败时仍展示上次成功报告。恢复核验逐份检查原报告和全部修订。

尝试终态为complete、failed、discarded_input_changed、discarded_already_published或abandoned；模型原始失败正文/思维链不保存。业务请求ID仅用于关联共享预算，不公开。模型元数据长期保留实际model ID、finish_reason、白名单usage和耗时；不因共享model-runtime账本裁剪至30日而丢失。 成功结果若经过单层传输处理，可额外保存transport_normalization=json_string_envelope或single_object_array，分别表示字符串包装解析或单对象数组取出；仅允许这两个可选值，无处理时省略，既有缺该字段的合法元数据继续核验。不保存原始包装/失败响应。输入schema_hash为64位十六进制摘要，与prompt_hash及配置一起参与幂等身份/恢复校验；不保存第二份Schema文本。未知费用不编造。

sleep-analysis-prepare先在线备份并临时恢复核验，再显式创建报告表；启动/GET/daily-run无隐式迁移。入库有容量准入和私有权限，输入上限24KiB、输出上限256KiB；读取/恢复重新校验结构、规范hash、业务字段和引用。无可选报告表的旧有效备份仍可恢复；半套表、错误hash、孤立引用、终态/发布矛盾或同一尝试重复发布均失败，不自动修复旧审计。

调用前保存输入及预算占位/尝试，随后关闭SQLite事务才外呼。模型结果通过校验后重读ready、input_hash、私有source_fingerprint、模型配置与Prompt元数据；发布写事务内guard再次按同一连接核对，在来源不变且未有成功报告时唯一插入。生成中事实/来源/配置变化标discarded_input_changed，候选不展示；已有报告则丢弃重复候选。模型拒绝或缺失必需观察不发布；内部insufficient_input载荷仍是失败而非成功报告。结构校验按固定output_schema_*规则码分类；类型错另用固定output_type_*类别，根值区分string/array/null/scalar，仍不保存真实路径或值；领域业务校验为output_business_mismatch。失败只保留这些白名单码及安全运行元数据，不保存响应正文、校验异常文本、路径或实际值。实际失败随后触及夜晚累计尝试上限时，结果保留failed和原错误，整轮partial；调用前已经达限则blocked_attempts且不调用。

发布后按当前私有来源指纹比较，变化只显示source_changed及安全原因，保留生成时输入和原结论，不重写、不再次自动调用。读取失败将变化状态设未知。单独Prompt或模型升级不称为源数据变化；已发布报告仍显示自己的版本。输入/报告/尝试现无自动删除任务，未来保留策略不得破坏至少一年证据、每晚唯一或预算审计。

### 输入、指纹与输出校验

新逐夜摘要至少引用本晚睡眠总时长、累计阶段、评分或已填睡感中的一项。生成校验拒绝常见的采集状态开场，允许在具体睡眠判断之后说明局限。该检查仅用于新候选；已存报告和备份沿用发布时可读合同。

发送用JSON只在ready后构造。核心事实metric为睡眠总时长、平均心率、夜间HRV、前日负荷/压力及四项环境均值；机器名、单位/来源/周期唯一注册在domain/sleep_features.py。可选纳入有效阶段累计、评分、晨起Body Battery、步数、环境min/max/声音峰值。源活动标题、姓名/GPS、身份、URL/Key与自由文本不进入输入；数值由程序计算而非模型计算。

逐夜取证在同一快照按醒来日读取晨起睡感。有已提交的有效评级时，增加可选 `morning_feeling` 对象，`id` 固定为 `F000`，`value` 只能是 `poor`、`fair` 或 `good`。未填或读取失败时省略整个字段，不发送填写时间、其他日期评级或自由文本。该字段加入有值时的输入摘要与来源指纹，允许报告引用并在受保护看板查看生成时选项。旧载荷沿用原结构和摘要。已发布当夜报告若采用过该证据，后来改选可提示来源变化；当时未采用该证据的报告保持原来源比较范围。

睡眠/心率/HRV基线各自使用D−30…D−1来源日，过滤不合法、陈旧或来源错误的值，至少7个有效日才提供均值及本晚差值；不足则保留该指标valid_n、expected_n=30及null，不混用不同指标分母。负荷直接复用D−1与D−31…D−2完整30日比较，缺天、陈旧或基线为零不输出百分比。综合评分/Body Battery标composite_metrics_not_independent，不能作为独立原因。

环境均值来自当前合格来源的有效样本，温湿光按valid_count加权，声音按完整有效秒在能量域聚合，不对dB算术平均。整分钟统计与被睡眠窗口裁剪的时间覆盖分别保留，边缘分钟不假装能精确拆出内部样本。只有数值/计数/单位/严重质量检查通过的样本参与；缺少的样本不插值补0。

输入completeness包含policy_version/policy、quality_state=complete/with_gaps、garmin_core_complete=true、environment_eligible=true、environment_expected_windows/environment_observed_windows、time_quality、四通道channels、warnings和warning_interval_scope。每通道保留count_unit、expected_windows/valid_windows/invalid_windows、expected_count/observed_count/valid_count/usable_count、effective_coverage_ratio、coverage_ratio及max_affected_span_seconds。invalid_windows只计已观察但不完全有效的分钟，valid_windows+invalid_windows=observed_windows，可小于expected_windows；未收到分钟仍进入采样预期分母。资格不使用environment_core_complete=true冒充全部完整。

外发warnings仅保留code/source/metric/window_start/window_end/expected/observed/recoverability；source固定environment，原因限missing_environment_minutes/invalid_channel。省略date、last_checked_at和来源身份，warning_interval_scope固定affected_minute_ranges_clipped_to_sleep；受影响范围不定位缺秒。with_gaps保留warnings及sampling_gaps_present，complete没有缺采warning且所有采样有效。

F类事实、T类15分钟分段、E类事件ID在输入内唯一，均为固定匿名引用。分段按通道coverage保留同口径统计，允许有效覆盖低于99%和缺值null，不重复应用整夜门槛。可选事件最多8个可信声音摘要，优先峰值、同峰按时间，再按时间展示；缺摘要、检测未完整、stored/dropped计数未知或不一致时不能标none_detected。合并包络不表示全时段持续声音，不把摘要条数当声源/觉醒数。跨睡眠边界无法精确裁掉active_seconds的事件不编造裁剪值。

输入最多24KiB UTF-8紧凑JSON，事实最多128、分段最多96、事件最多8。超预算先移除可选分段，再移除非核心背景，分别加timeline_omitted_for_budget/optional_context_omitted_for_budget；不删核心、采样分母、warnings或限制，仍超限则blocked_input_budget。内部报告上限保持8个观测、3个假设/建议；当前模型返回收紧为4—6个观测、至多2个假设/建议，各引用数组1—6个唯一ID；消息总量32KiB、完整请求64KiB、输出4096 token/响应256KiB另受模型层限制。

唯一[system Prompt](../src/sleepscape/resources/sleep-analysis-system.txt)与[Schema](../src/sleepscape/resources/sleep-analysis.schema.json)在resources/随包分发。同一文件内`model_output`是当前模型四字段合同，`output`是内部完整报告合同；没有重复资源。system动态附加model_output，user包含白名单证据JSON及程序固定生成/纠错要求。当前显式json_object仅保证JSON语法；显式json_schema模式才将同一model_output按aliyun-compatible-1派生发送投影，不自动切换。

model_output仅含summary、observations、hypotheses、actions。模型填写文字、类别/因素、有效证据ID及替代解释/观察建议，不回显日期/hash/quality/limitations/status/evidence_level。resources.validate_model_output先按模型合同校验，再由纯领域函数用已核验输入组装完整报告：固定sleep-output-1、日期/hash/质量与全部限制来自输入、status=complete、观察/假设等级limited。完整output和领域规则仍再次校验；程序不能补写正文/引用或删减超额条目。缺类、空观察、无效证据或不合规正文均不发布。原存储、看板与恢复的完整报告格式保持兼容。

传输规范化只允许两个互斥单步：根字符串严格再解析一次为对象，或单元素且该元素为对象的根数组解出对象；之后继续全部校验。重复键/非有限数值、Markdown、空/多元素/嵌套数组、字符串与数组互相包装均不修复。最大输入加纠错的消息预算有回归；schema_hash覆盖唯一Schema文件内各合同，prompt_hash覆盖唯一Prompt，response_mode/profile进入config_fingerprint。[合成样例](contracts/sleep-analysis.example.json)展示内部报告，不代表实际模型返回。

input_hash=sha256(规范JSON)：移除input_hash/data_cutoff_at/is_provisional，采用ensure_ascii=false、sort_keys=true、separators=(",",":"), allow_nan=false的UTF-8。hash包含所发事实、质量/策略、通道计数/分母、warnings、限制和基线，不因再次读取时间变化而变。存储另对包含全部字段的payload原文计算payload_hash，用于完整性核验；不能用重算语义hash绕过字段/数值校验或每晚唯一。

报告source_fingerprint不外发模型，覆盖当前检查的稳定来源身份/质量/观测及同快照30日基线日期、睡眠来源和核心卡片；即使当前不ready也可计算。排除抓取/接收/检查时间及不改变映射的索引元数据。来源变化比较不依赖模型输入必须可生成，不能把缺数据解释为与旧报告一致。

输出schema_version固定sleep-output-1；含summary、observations、hypotheses、actions、limitations及input_quality_state，日期/hash/quality须对应输入。每个evidence_ids数组内部唯一；limitations由程序按原顺序完整复制required_limitations，保持不增加、不遗漏、不重复，本地继续强制唯一与集合相同。成功报告必须有四类观测、有效证据ID、对应类别/假设因素依据、保留全部限制，不能引用null不可用事实；with_gaps须在概况或环境观察说明缺采。报告complete仅表示生成完成，不改写为100%采样完整。insufficient_input必须空观察/假设/建议，并按失败处理。

Schema之外校验必需事实、白名单单位/来源/周期、计数关系/比率、基线分母及派生差值、时间边界、ID唯一、引用和字节预算。数值直接显示生成时证据卡，模型文字不写阿拉伯数字/具体时间/自行换算比例；禁止明显凭据、外链、注入和医疗处置文本。有限规则不能证明所有自然语言因果/方向判断，真实报告仍需对照证据人工核验，不将观测升级为诊断或确定因果。

## 声音事件回听

`POST /api/v1/ingest/event-audio` 单独使用既有设备 Bearer 和 HTTPS，Content-Type 为 application/octet-stream。首行为 ASCII 的 boot UUID、起点单调毫秒、采集 UTC 毫秒，空格分隔，换行后是 8 kHz 单声道 G.711 μ-law。每个采样 125 微秒。设备身份来自令牌，事件序号由单调分钟确定，按设备、boot、事件序号对应摘要。相同片段重传幂等；身份冲突拒绝。原观测协议和去重规则独立。

候选时段采用上海时间 18:00 至次日 12:00。每条摘要至多一个连续片段，每段最多十二秒，每个候选夜最多六十段，满额约 5.76 MB。压缩音频存于独立私有库，主库备份、模型输入和日志均排除音频。音频库页容量上限 200 MiB，另有 SQLite 临时事务开销。磁盘准入或容量不足则拒收。设备 Flash 不留音频。

09:00 采集后按醒来日读取已结束的 Garmin 主睡眠窗口，同时核对同一 boot/序号的有效 UTC 索引和事件包络。设备采集时钟与索引差超过两秒舍弃；在窗口边界内缩至少 250 毫秒，并按两时钟差和已知误差扩大余量，按采样点裁剪。该保护余量未校准为绝对时间精度。只有已发布片段能经看板鉴权的 `GET /api/v1/event-audio/{record_ref}` 读取为 WAV；播放前复核现行窗口，后续收窄不恢复已删除样本。关联失败显示原摘要。候选片段自采集起满 48 小时过期，正式片段满 30 天过期。Web 启动及运行时每分钟清理，设备上传、每日任务也执行过期清理。服务停机期间不提供读取，恢复运行后清理。
