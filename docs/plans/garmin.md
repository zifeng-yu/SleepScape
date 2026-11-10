# Garmin 数据采集方案

更新：2026-11-10。适用于Forerunner 265、中国区个人账号。
本文维护数据源与采集策略；[字段口径](../data-contract.md)、[操作步骤](../operations.md)和[实测结果](../verification.md)分别维护。

## 官方依据与能力边界

截至2026-11-10核验的[官方Swagger](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)为OpenAPI3.0.1、API1.0.0，包含中国区及国际区服务器。
[官方FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)说明个人Key换取access token，token最长24小时且无refresh token；
[官方Python SDK](https://github.com/garmin/connect-personal-api-python)提供认证与GET封装。程序始终使用配置区域，认证失败不自动换区。

当前范围为睡眠、HRV、心率、活动、日汇总、压力、步数、SpO2、Body Battery及日常活动摘要。
可用字段及稀疏程度由真实响应核验，不因接口存在就假定字段齐全。当前官方数据没有睡眠分期起止区间，
也没有已接入的完整压力/独立呼吸时序、训练准备度、恢复时间或体动曲线。
观测频率和原文一致性不证明设备准确度；睡眠窗口及累计时长的展示见[看板方案](dashboard.md)。

## 常规采集资源

`garmin-collect`默认截至配置时区昨日，详细7日、背景31日。初始化7个睡眠日且要求每晚完整负荷基线时使用38日背景。

| 资源键 | 常规窗口与请求结构 |
| --- | --- |
| `sleep`、`hrv`、`daily`、`spo2` | 详细窗口逐日请求 |
| `heart_rate` | 详细窗口及首日前一天，覆盖跨午夜 |
| `daily_summaries` | 背景窗口，程序每块最多365日，响应为`data[]` |
| `sleep_summaries` | 背景窗口，每块最多31日 |
| `hrv_summaries` | 背景窗口，程序保守拆28日，响应为`hrvSummaries[]` |
| `stress`、`steps` | 背景窗口，每块最多28日 |
| `body_battery`、`wellness_activities` | 详细窗口的范围资源；Body Battery最多31日 |
| `activities` | 背景窗口每7日分块，每页100条，每块最多50页 |

活动使用`excludeChildren=true`。不足一页才完成；重复页、重复ID、无效ID或达到页数上限均拒绝声明完整。
7日活动分块源于整段查询故障后的实现选择，不是官方上限。分页审计行数与实际HTTP次数分别记录，token与重试另计。
Body Battery和SpO2未额外取首日前一天，首夜跨午夜可能有缺口；不通过填值掩盖来源缺失。

## 今日暂定与滚动同步

`garmin-sync`按配置时区今天生成计划，无`--end`参数；常规`garmin-collect`仍拒绝今天和未来截止日。

| 日期范围 | 资源与状态 |
| --- | --- |
| 可包含今天的详细窗口 | sleep、HRV、心率、SpO2、Body Battery、wellness；今日数据标pending/is_provisional |
| 已结束日期，最多到昨日 | daily、背景摘要、压力、步数和正式活动；不将今天的全天统计宣称完整 |

同步策略：首次背景38日；日常回查7日；距成功宽窗口满7日再查38日。详细默认7日，心率额外补首日前一天。
`--history-days`可显式调整本轮背景；既有历史保留，采集窗口不限制展示或核验回读30日基线。
`verify`保留本轮覆盖统计，同时以`load_evaluation_window`单列读取存量历史与请求状态，今日全天负荷保持暂定。

仅整轮completed推进成功窗口。短停机扩大详细窗口至最多31日；超过30个已结束日未成功采集时，
返回`backfill_required`及分块补采命令并停止联网，执行成功后再sync。未执行补采不标记已补齐。
计划及请求审计记录完整日截止、暂定日期/资源和完成状态，不修改原文来“确认”今日数据。

已提供上海时间08:30、12:30的systemd定时模板，尚未安装。设备必须先同步到Garmin Connect；服务不能代替手表与手机的同步。
晚到数据和后续修订由重叠窗口回查取得，HTTP200不代表设备同步绝对完整。

## 采集保护与失败行为

- 路径及参数白名单，仅访问已纳入计划的官方资源；不额外查询profile、FIT、GPS轨迹或其他个人资料接口。
- 单响应读取上限10MiB，GET间隔至少0.2秒，文件排他锁阻止两轮采集同时执行。
- 暂时网络、429及指定5xx最多3次尝试；401单独刷新一次，403不盲目刷新，不循环登录。
- `Retry-After`不超过10秒可等待；更长则记`retry_not_before`并停轮，下一任务遵守等待。认证失败或限流耗尽停止整轮。
- 单资源失败可保留已有记录并继续其他资源；无效响应不清空原分区，视图与报告标记陈旧风险。
- 有效空响应与请求失败分开；有效修订原子替换当前来源分区，原始快照保留修订证据。

原始快照按`(resource, request_key, content_hash)`去重；当前记录按`(resource, kind, record_key)`定位。
滚动窗口改变请求键时仍可能新增原始快照，不能称为全局跨窗口去重。重复来源分开保存，不重复合计指标。
密钥/token只由适配器读取；终端仅输出安全状态、数量和问题码，不输出完整响应或凭据。

## 核验责任

`garmin-verify`离线检查原始字段、标准化、来源日期、单位、时序和显示数据前提；负荷公式只采用数据契约中的固定口径。
`garmin-audit`只读源库，在私有临时库重放两遍，核对raw哈希、JSON、去重与当前记录；故障/修订/回滚用合成场景另验。
临时库结束后清理，报告覆盖写入当前文件，原始观测与审计继续保留。
采集已完成、数据转换一致、浏览器已验收、云端持续运行和手表测量准确度分别记录，不相互替代。
