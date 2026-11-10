# 数据契约与展示口径

状态：当前Garmin采集、标准化与只读展示合同，更新于2026-11-10。
中国区多日采集已完成；最新独立对账和完整验收结果见 [核验报告](verification.md)。
看板与API已在本地实现；云部署、环境硬件尚未验收，以下环境条款是后续接入约束。

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
`recoveryHeartRate`也不是恢复时间。环境首版计划采集温度、湿度、光照、相对声音汇总和事件；
选型、采样和上传合同见下文“环境采集合同（计划）”，当前尚无硬件实现或实测环境数据。

## 来源、标准化和修订

原始JSON独立保存。标准化记录带 `resource`、`kind`、记录键、来源日期、UTC时刻、
抓取时间和快照关联；数据保存 `source` 映射、参与映射的 `raw` 值和 `quality` 标记。
请求审计保存转换版本、字段问题及响应状态；当前未另行查询设备/固件资料。

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

## 今日暂定、滚动窗口与完整性

常规`garmin-collect`默认7日详细/31日背景，截止昨日；7晚完整负荷验证建议7/38。
`garmin-sync`按配置时区今天拆分资源：sleep、HRV、心率、SpO2、Body Battery和wellness详细数据可含今天，
daily及背景摘要、压力、步数和正式活动仅到昨日。今天的数据标`pending`，夜晚视图`is_provisional=true`，不是全天定稿。

同步计划保存`complete_day_through`、`provisional_dates`、`provisional_resources`和策略；请求审计保存`collection_state`。
这些是请求覆盖及暂定口径，不证明上游同步无遗漏或手表判断正确；不修改raw来“完成”今日数据。
首次背景38日、日常7日回查、每隔7日宽回查38日；旧历史保留。仅整轮成功推进同步成功窗口，失败/无效响应保留旧值。
短停机扩大窗口，超过30个已结束日未成功采集则返回明确补采窗口与命令并停止；未执行成功不能标为已补齐。
看板与verify均从存量记录及请求状态回溯所需30日基线；verify以`load_evaluation_window`单列评估范围，本轮coverage统计仍按采集窗口。

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
state区分`available/missing/invalid/stale/pending/unavailable`；具体字段缺失、整次请求失败、当前日期暂定分别说明。
最近请求失败且留有旧值标stale；状态页36小时未见成功数据给新鲜度提示，不推断用户忘记同步。

| API | 主要合同 |
| --- | --- |
| `/api/v1/nights` | 1—92日窗口，limit为1—92；摘要列表含无记录日，返回`next_cursor/available_dates` |
| `/api/v1/nights/{D}` | `cards/sleep/stages/activities/load_comparison/is_provisional`；无该来源日记录返回404 |
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

页面及业务API只读且同源，响应不缓存；应用HTTP Basic校验私有PBKDF2哈希，Caddy保留Authorization。
未设密码仅允许本机直接访问，远程健康数据请求需要HTTPS；`/health`不含健康数据。`/docs`始终关闭，debug仅可开放受保护的`/openapi.json`，默认关闭，不加载CDN；云端TLS与代理验收单独记录。

## 备份状态合同

`db-backup`在线复制SQLite到新UUID文件，核验完整性、外键和raw；`db-restore-check`只在临时目录恢复检查，不覆盖运行库。
两者成功写私有`backup-status.json`，仅含`last_success_at/status="verified"/record_count`；状态API只返回`state/last_success_at`。
这表示最近一次本地核验成功，不证明副本离机、已加密、当前仍存在或已安排定时备份。
当前无自动清理、备份调度、加密/离机传送或磁盘门限保护；保留策略须由用户另行管理。

## 环境采集合同（计划）

本节是后续固件、写入API、存储和视图的共同设计，**尚未实现、未用真机验收**；不表示当前接口已经接受硬件上传。
合同按测量能力定义，不绑定某一硬件型号；当前测量配置示例为M5 Atom Voice（C008-C）、Gravity AHT20（SEN0528）和DLight（U136）。
只启用温度、相对湿度、光照、相对声音；首版没有CO₂或VOC推算CO₂通道。替换型号必须登记新测量配置并验证对应驱动。
采购、接线和资源可行性见[硬件方案](plans/hardware.md)。本节维护字段与口径，不重复采购步骤。

### 来源隔离、配置与身份

环境观测、时间锚点、测量配置、写入审计分别保存，使用独立环境表；不写入、覆盖或重新解释Garmin事实表。
服务器以UTC关联两类来源，睡眠归属日期仍采用已有Garmin规则。

- `device_id`是登记后的设备标识，由设备令牌绑定；客户端不能用一个设备令牌替别的设备写入。
- `boot_id`每次启动生成新的随机标识，断电后不能复用。单调时钟只在同一`boot_id`内有效。
- 记录身份为`(device_id, boot_id, record_type, seq)`；`seq`为该启动周期、该记录类型下从0递增的非负整数，最大`2^53-1`，不能倒退或复用。
- `measurement_profile_id`引用不可变测量配置：硬件型号/修订、传感器通道与单位、固件版本、采样与聚合策略、PDM转换/滤波、固定增益、事件算法/阈值、摆放位置与校准信息。配置必须先登记；未登记配置不进入正常展示。
- 更换硬件、增益、滤波、采样率、事件算法或位置均生成新配置；配置变化时关闭当前窗口，以`partial_window`标记，禁止一个窗口混用两个配置。
- 主板、传感器或配置可替换，历史观测及旧配置仍保留作为测量证据；这不是保留旧版程序。dBFS不能跨配置默认作绝对比较。

### 上传信封和认证

唯一计划写入口为`POST /api/v1/ingest/environment-batches`，JSON请求使用`Content-Type: application/json`。
`schema_version=1`是JSON整数，独立于当前只读API的字符串`contract_version="1"`。请求允许补传和乱序，不以到达顺序作为采集顺序。

| 信封字段 | 约束 |
| --- | --- |
| `schema_version` | 固定整数1；不支持的版本明确拒绝，不静默按当前版本解读 |
| `device_id` / `boot_id` | 非空设备/启动标识；单批只允许一个设备和启动周期 |
| `records` | 分钟和事件记录数组；`record_type`分别为`minute`、`event`，最多各60条 |
| `time_anchors` | 独立时间锚点数组，最多60条；无锚点时为空数组 |
| `device_status` | 可选、每批最多一个有界设备运行快照，与观测分开保存，仍计入64 KiB请求体 |

单次请求体上限为**64 KiB（65,536字节）**，包含整个UTF-8 JSON信封；不接收压缩请求体。
即使条数未达上限，超过字节上限仍须继续拆批，按字节优先。平时每5分钟上传已完成记录，断网后有界退避重试，恢复后分批补传。
空请求、未知记录类型、非有限数、字段类型/单位错误、非法计数和超出容量的字符串均拒绝；标识最长64个ASCII字符，单记录质量标记最多16个已登记代码，不接受自由文本日志作为记录。

写入口只在HTTPS下接受专用设备Bearer令牌，令牌不放URL、不写日志、不进入Git。
看板HTTP Basic与设备Bearer分别校验：设备令牌不能读健康数据，看板密码不能写设备数据。
实现时必须将当前全局GET/HEAD限制**仅对上述精确路径的POST窄化**，不能对整个`/api`目录放行写方法；尾斜杠、相似前缀和其他路径不成为旁路。
现有UI、静态资源和只读API继续按原访问规则运行，不开放跨域。

### 单调窗口、时间锚点与UTC映射

每条记录保存`measurement_profile_id`、`seq`、`record_type`、`mono_start_ms`、`mono_end_ms`和`quality`。
时间是从本次启动开始的单调毫秒数，使用64位整数并限制在JSON安全整数范围；窗口为左闭右开，结束必须晚于开始。
正常窗口长60,000 ms，以启动后的单调时间为基准连续分桶；校时不重新分桶。启动、关机或配置切换导致的不完整窗口保留实际时长并标记。
`received_at`由服务器记录为上传接收UTC，绝不代替采集时间。

时间锚点包含`anchor_id`、`monotonic_ms`、`utc_at`、`source`、`uncertainty_ms`和质量代码：

- `utc_at`明确带`Z`，`source`记录实际校时来源，例如`ntp`；`uncertainty_ms`是非负误差估计，没有可靠估计时为null，不能填0伪装精确。
- 锚点身份为`(device_id, boot_id, anchor_id)`，内容不可变；校时得到新结果时追加新锚点，不修改原记录或旧锚点。
- 查询映射返回`utc_start/utc_end`、`time_quality`、`time_uncertainty_ms`、所用锚点ID和`time_mapping_version`。`time_quality`区分`synced`、`estimated`、`unknown`、`conflict`；未经校时的原始记录无需编造UTC。
- 同一启动周期内可根据锚点与单调差值估计UTC；离锚点越远，须考虑已验证的时钟漂移。未验证漂移时误差保持未知并标`estimated`，不声称秒级精确。
- 多锚点不一致或UTC跳变时保留全部证据，标`conflict`并暂停冲突区间精确叠加；不得因NTP调整产生重复分钟、倒序窗口或覆盖旧值。
- 无电池RTC的设备冷启动且无网时为`unknown`，不沿用上一启动周期的单调映射。只有该启动周期后续获得有效锚点才可能补映射；无锚点周期永不借接收时间伪造采集UTC。
- 已同步后断网的记录可以估计时间，但48小时缓存能力不等于48小时精确时钟保证。未知UTC记录保存并计入状态页，不绘制到Garmin时间轴。

独立映射使后续校时可改善查询时间而不改变记录身份或原始载荷；重复上传始终比较原始内容，避免“同ID、校时后改UTC”的幂等冲突。

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
PCM只在短RAM缓冲中用于聚合，不写Flash、不上传、不输出串口，不启用含音频RAM的Flash core dump。
固定增益，不启用AGC、语音增强或动态降噪；处理链改变必须换测量配置。

令有效PCM归一化幅度为`x/32768`，每个有效1秒窗口的均方能量为`E_i=mean((x/32768)^2)`，
`RMS_dBFS_i=10*log10(E_i)`；`energy_mean_dbfs=10*log10(mean(E_i))`，不能直接算dB数值的算术平均。
`max_1s_rms_dbfs=max(RMS_dBFS_i)`仅是1秒RMS最大值，不是瞬时峰值声压。实现采用足够精度的累加器，防止平方和溢出。
全零且完整收到的PCM窗口仍以零能量参与分钟能量平均，单秒dB值记null并标`below_numeric_floor`，不输出`-Infinity`；整分钟均为零时分钟dB值也为null。这与未收到PCM的`missing`不同。
该值是当前设备和处理链下的相对数字强度，不能改名为dBA、用于WHO限值判断，或默认跨设备比较。

### 相对声音事件（工程默认，待真机验证）

`event`记录只描述相对声音变化，不识别人声、打鼾或声源，不输出导致觉醒的结论。
事件算法使用固定版本和以下默认参数；这些阈值是为实现和验收制定的工程规则，**不是科学证实的睡眠影响阈值**。

1. 使用当前1秒窗口之前最近60个有效、有限dBFS的1秒窗口中位数作为背景值，不把当前窗口放入自身基线；启动先积累60个有效窗口，期间不产生事件。缺失窗口不补0；至少需要60秒，存在缺失时暖机时间更长。
2. 当前值相对背景升高至少10 dB开始，开始时冻结本次事件的背景值；至少一个有效1秒窗口才能形成事件。事件期间继续维护滚动历史，下一事件重新取当时背景。
3. 相对冻结背景低于6 dB连续3个有效秒时结束，结束点取这3秒开始处；该尾部安静段不计入事件活跃时长。缺失或非有限窗口立即结束当前事件并标`incomplete`，重新积累连续60个有效基线窗口后才恢复检测。
4. 相邻候选事件间隔不超过2秒时合并；跨分钟事件按单调分钟边界拆成可关联片段，保留连续/截断标记。分钟记录与事件计数最多延后3秒定稿；断电未定稿窗口按实际可恢复部分标不完整，不改写已确认载荷。
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
设备只删除ACK明确确认的对应缓存项，不能用“最大已收seq”跳过中间缺失项；ACK丢失则原样重发，不重新采样、生成新ID或修改时间字段。
遇到409保留冲突项并标需处理，其他待传批次继续；413拆小批；429/503按有界退避重试；认证失败暂停网络重试风暴而继续本地采集。

断网缓存目标48小时，以实际编码后的记录、索引、CRC、事件、日志和文件系统占用验收；容量预算统一见[硬件方案](plans/hardware.md)，不把本地二进制长度当作JSON长度。
分钟数据、事件和日志分别限额；事件不足时保留分钟统计并计`dropped_count`。已定稿记录不能为更新丢弃计数而重写，后续淘汰用独立`device_status`记录。
设备状态保存`status_seq`、`monotonic_ms`、缓存待传条数、按记录类型区分的累计丢失数和最多16个丢失范围（记录类型、启动ID、序号及单调起止）；超出明细上限保留累计量并标`ranges_truncated`。
状态身份为`(device_id, boot_id, status_seq)`，每次生成新快照时递增；相同身份内容不可变。它不进入Garmin时间轴、不能携带自由文本日志，也不把服务器尚未接收误判为采集失败。
缓存超过已验证保留范围时按明确的最旧优先策略淘汰，累计丢失记录数及单调区间，不静默覆盖。服务器状态页显示采集缺失和传输延迟的区别。
分钟记录提交Flash后才属于断电可恢复数据；写入应有版本、校验和与可恢复边界，启动扫描不能把半条记录作为有效观测。
音频DMA、网络和存储分开调度，但不能据此承诺Flash擦写绝不影响音频；实现须统计丢样，并验证接近容量上限、TLS重连、断网补传和断电恢复。

## 后续分析边界

- 入睡前运动、活动和压力与睡后数据分开，不能把结果发生后的数据当原因。
- 晨起评分、Body Battery等综合指标可能包含同夜睡眠/HRV，不能作为独立原因。
- 声音或亮灯可能发生在醒来之后，时间重合不证明环境导致觉醒。
- 主观恢复感、咖啡因、酒精等生活记录仍有帮助，第一版不输出因果结论。
- 环境设备未校时记录不得冒充准确时间，补传保留采集时刻和校时状态。
- 未校准声音指标不标为准确dBA，不用于判断是否符合专业噪声标准。
- 原始响应与标准化一致只证明程序转换一致，不证明手表测量或睡眠判断准确。
