# 可替换模型连接层方案

更新：2026-11-20。通用连接、受监督调用和持久预算由 P1 维护。逐夜和 7 日复盘通过同一配置、适配器和上海自然日预算运行，业务重试及证据见[LLM 方案](llm.md)。模型地址和密钥留在私有配置。

采集、维护与完整性检查独立运行。P4报告业务复用本层协议和共享预算；日任务在自动开关开启后处理合格且尚未发布的夜晚，GET和单独analysis-check不调用模型。用户已确认现有私有配置用于本机测试及云端每日09:00调用；公开套餐说明另列于下。

## 参考与分层

已只读核对本机 `simulate-china-stocks/src/china_stock_sim/models/` 的 `base.py`、`settings.py`、`factory.py`、`openai_compatible.py`、`audit.py`，以及 `strategies/llm_call.py`。沿用接口分层，不复制交易账户、账本、盘前截止或交易重试规则，也不读取该项目真实模型配置和密钥。

| 已实现文件 | 职责 |
| --- | --- |
| `src/sleepscape/models/base.py` | `ChatMessage`、`ModelRequest`、`ModelResult`、类型化`ModelError`和`ModelClient.complete()`协议 |
| `models/settings.py` | 从现有`Settings`和非敏感模型配置加载稳定引用；配置校验、离线状态，不另写`.env`解析器 |
| `models/factory.py` | 根据provider创建客户端；业务只认识`sleep_analyst` |
| `models/openai_compatible.py` | 请求映射、响应大小/状态校验、结构化输出参数与usage提取，不自行重试 |
| `models/schema_transport.py` | 按显式schema_profile派生发送用Schema；不修改唯一规范合同，不替代本地校验 |
| `models/audit.py` | 明确的脱敏元数据白名单；不打印配置对象、请求正文、健康值、响应正文或私有URL |
| `configs/models.toml` | 将`sleep_analyst`映射到`openai_compatible / qwen3.8-max`，保存非敏感参数；打包时作为同一默认配置收入wheel |
| `services/model_call.py` | 有界子进程调用、总截止与进程回收；不让慢模型占住Web请求 |
| `services/model_health.py` | 离线状态与显式合成能力核验，不生成睡眠报告 |
| `storage/model_runtime.py` | 独立进程锁、原子持久占位、共享每日请求预算和白名单核验元数据 |

现有`domain/models.py`是Garmin领域数据模型，保持用途。新`models/`仅封装远端模型连接，不读取数据库、不决定睡眠结论、不放Prompt、不调度、不写报告。依赖方向为`services → domain/models/storage`，其中domain与storage不反向依赖模型客户端。

## 配置合同

现有Settings已读取：`SLEEPSCAPE_LLM_ENABLED=false`、独立`SLEEPSCAPE_LLM_AUTOMATIC_ENABLED=false`、`SLEEPSCAPE_LLM_MODEL_REF=sleep_analyst`、`SLEEPSCAPE_LLM_BASE_URL`或互斥的`SLEEPSCAPE_LLM_ENDPOINT`、`SLEEPSCAPE_LLM_KEY_FILE=.secrets/sleep-llm-key`、`SLEEPSCAPE_LLM_CONFIG_FILE=configs/models.toml`。`.env.example`仅列空地址与非敏感默认值。

同名Settings配置优先级为显式进程环境 > 私有`.env` > Settings默认；模型引用再从TOML读取provider、模型ID及受限参数，不另建环境解析器。models.sleep_analyst.response_mode当前显式设为json_object；省略时默认json_schema，仅允许这两个值，未知值报model_configuration_invalid。schema_profile仅允许aliyun-compatible-1或native，当前保留前者、省略时为后者；两种模式均校验其合法性，仅json_schema执行投影。模式与profile均参与配置指纹，更改后不能继承旧连接状态或混用生成中的配置。默认TOML在仓库缺席时可使用wheel内的打包副本，自定义配置缺失则报错。Key只读私有文件，不放TOML、命令行参数、Git或前端；要求父目录700、文件600、当前用户所有、普通文件且不经符号链接，大小不超过4KiB。适配器才读取正文；状态查询只核对文件元数据。允许文件末尾一个换行，拒绝正文内换行、空白和控制字符。

配置缺URL/Key为`not_configured`，完整但关闭为`disabled`，非法配置为`invalid_configuration`。配置完整仅表示可发起核验，`connection_state=verified`表示当前配置最近模型请求成功；地址、模型参数或密钥文件变化后显示`configuration_changed`，不能继承旧成功。基础采集和看板不因模型缺配置或失败而停止。last_check_at/last_check_status沿用共享运行账本的最近请求，可能是合成探针或真实报告调用；不应将其标注为独立合成验收，也不等同睡眠报告已发布。

URL必须HTTPS；拒绝用户名密码、查询串、片段、空host。`base_url`统一去末尾斜杠后只追加一次`/chat/completions`；完整endpoint模式要求已有此固定尾缀。不能同时填两种地址。仅访问配置的确切目标，不跟随重定向，不由模型响应决定host；私有网关若有特殊路由需显式适配，不猜测替换URL。

## 接口与调用边界

接口为`load_model_settings(Settings) → ModelSettings`、`create_client(ModelSettings).complete(ModelRequest) → ModelResult`；业务运行通过`call_model`进入受监督子进程。请求含消息、输出Schema、Schema名称及可选输出token上限；结果只含已校验文本、已核对模型名、finish_reason、白名单usage与固定传输规范化标记。耗时由运行层记录，不保留上游请求ID。错误只含稳定`code`、可选HTTP状态、可选`retry_after`及是否可重试，不含上游正文。输出Schema失败仅按可信校验关键词映射固定码：unique_items、contains、required、const、type、enum、additional_properties、min_items、max_items、min_length、max_length、pattern、format均使用output_schema_前缀；allOf/anyOf/oneOf/if/then统一为output_schema_condition。未知关键词仍拒绝并使用output_schema_other，非标准校验异常保留output_schema_mismatch。睡眠报告类型错误另映射固定output_type_*位置类别；根值可区分string/array/null/scalar，数组另有empty/multiple/single_nonobject固定类别；不返回实际路径、下标、字段内容或响应。不会保存ValidationError正文、instance、路径或校验值，也不会保存失败响应。领域业务验证另以output_business_mismatch区分，不能从规则码推断具体健康内容。401、403、404、429和指定5xx有固定分类；HTTP 400出现固定结构化输出/参数能力标记时归为`capability_unavailable`。

2026-11-18按用户明确指令将当前模型从`qwen4-max`改为`qwen3.8-max`，沿用既有私有URL/Key；这是显式配置变更，不是运行时自动回退。当前返回模型白名单只有`qwen3.8-max`；其他名字包括未知版本别名均报`model_mismatch`，仅在核对同模型关系后显式修改映射。精简协议下qwen3.8-max已有一份通过完整校验的真实结果；随后qwen4-max仍返回超额条目，因此按用户条件保留qwen3.8-max。重新生成也曾缺字段，不能宣称响应格式已稳定；实际请求、已验证候选发布和后续自然任务分别记录于核验报告。2026-11-17查阅的[qwen4-max官方模型页](https://help.aliyun.cn/zh/model-studio/qwen4-max)及其合成连接仅作为旧模型的带日期证据保留，不用于证明当前模型能力。

使用Chat Completions、非流式；response_mode决定明确的请求协议，不因某次失败自动切换。当前项目配置选择json_object，请求response_format={"type":"json_object"}，它只约束JSON语法，不保证字段类型或业务正确。 发出前要求system/user至少一条含不区分大小写的JSON字样，模型返回Schema说明已满足；缺少时本地拒绝请求。配置为json_schema时，才发送response_format.type=json_schema、strict=true及兼容投影；不支持时返回capability_unavailable。两种模式使用相同模型、私有URL/Key和完整本地发布校验。阿里云严格模式不支持部分Schema关键字，而JSON Object本身不承诺Schema一致性。[官方结构化输出说明](https://help.aliyun.com/zh/model-studio/qwen-structured-output)

客户端要求Schema根节点为object、根属性全部required且禁止额外属性；允许本地`$defs`指针，拒绝远程引用与重设解析基址。请求前校验Schema，响应后使用本地Draft 2020-12再次验证；JSON重复键、非有限数值、截断和非正常结束均失败。P4通过resources.contract_schema提取唯一包内合同的model_output四字段对象及可达$defs，并在两种响应模式下附在system消息中。模型只填观察文字及引用；业务层从已核验输入填入日期/hash/质量/限制等已知元数据，再校验内部output。仅json_schema模式还将其派生为response_format兼容投影；json_object不在该参数中发送Schema。所有表示均来自同一资源，不维护复制稿，不依赖网关的结构保证。发送前及收到后校验model_output，组装后及落库/恢复时继续按完整output校验。P1合成Schema不代替完整报告的真实验收，结果见核验报告。

响应内容先严格解析一次。原本为对象时直接校验；仅允许两类互斥的单层处理：根字符串再严格解析一次且结果必须为对象；或根数组长度恰为一、唯一元素本身为对象时取出该对象。处理后仍执行原完整Schema、敏感内容及领域校验。解析拒绝重复键和非有限数值；不剥Markdown、不猜补字段，不接受空/多元素/嵌套数组、字符串包数组、数组包字符串或多层字符串，不组合或递归处理。

仅发生上述处理且最终成功发布时，报告model_metadata可保存transport_normalization=json_string_envelope或single_object_array，分别标识字符串包装和单对象数组；未处理为none，持久元数据可省略该键，既有合法记录仍兼容。它记录实际采取的确定性处理，不保存原包装或失败响应。兼容代码存在不证明真实返回符合任一种包装，更不证明报告已通过；真实结果单独见核验报告。资格、引用、内容、唯一发布及正常调用预算均不变。

在显式json_schema模式下，aliyun-compatible-1仅在HTTP请求的response_format.json_schema.schema中移除uniqueItems、contains、minContains、maxContains、additionalItems、multipleOf、patternProperties、propertyNames。转换按Schema位置递归；properties/$defs等映射中的业务字段名保留，const/default/enum/examples内的实例值不改动，不删除名称恰与关键字相同的业务字段。native原样深拷贝；移除后若产生悬空$ref，外呼前拒绝为capability_unavailable，不发出失效引用。

此兼容处理不改变resources内唯一规范Schema、Prompt、输出质量规则或其审计摘要。适配器仍用转换前完整Schema检验响应，领域层继续检查唯一项、必需观察类别/引用等业务约束；缺失/重复即使传输端接受，本地仍拒绝，未验证候选不发布。schema_hash仍是规范Schema摘要，response_mode和profile通过config_fingerprint区分。兼容配置的离线测试与真实调用分别记录；当前真实复验进度仅见核验报告，不以历史简单合成探针成功代替完整报告兼容性验收。

默认输出总预算4096 token；Qwen4的思考内容与最终回复共享输出预算，不能套旧模型的max_tokens语义。[兼容性变更](https://help.aliyun.cn/zh/model-studio/api-compatibility-upgrade-notice)
不同时传max_tokens/max_completion_tokens。当前请求固定包含`enable_thinking=false`，能力核验同时检查该参数是否被入口接受；不支持时失败并报告，不自动移除或提高预算。其他参数如temperature、seed不发送。显式合成检查仅申请512输出token，报告业务请求上限4096。消息总量不超过32KiB、完整请求不超过64KiB；这不替代P4的24KiB睡眠事实输入预算。

单次硬截止不超过180秒（包含连接、读取和解析），响应上限256KiB；运行层以spawn子进程监督，超时终止、必要时kill并回收。独立模型文件锁保证同一数据目录单并发；外呼不持SQLite事务、数据库写锁或Garmin采集锁。适配器及当前合成检查均零自动重试；`retryable`和`Retry-After`仅提供受限状态，不自行再发请求。

## 合成检查、持久预算与使用范围

`model-status`、doctor、页面GET只读配置/已存状态，不读Key正文、不联网、不创建或修复运行文件。`model-check`必须显式带`--live`才进入检查；缺配置、关闭、预算耗尽或正在调用时不外呼。每次检查最多发一个合成请求，要求返回随机nonce和固定JSON值，不查询数据库或发送健康数据。命令与私有配置操作只维护在[运行手册](../operations.md)。

`data_dir/model-runtime.json`保存私有运行账本，按`Asia/Shanghai`自然日最多预留30次，失败、超时和进程中断均占用已预留次数，进程重启不清零。请求前在独立锁内原子写入占位，成功/失败后写回白名单状态、usage与耗时；响应正文、Prompt、Key、私有URL均不落盘。新增占位时将元数据裁剪至最近30个上海日期（含当天），上限1024条、1MiB；正常30×30最多900条，其余为此前明确授权批测记录保留有界余量。只读查询不执行清理，备份/恢复及导出同步接受模型账本1MiB，其他运行元数据保持原边界。状态文件损坏或权限不安全时阻止新调用，不按空账本重置预算。共享账本还强制遵守最近请求的Retry-After截止，后续sleep-analyze、daily-run或model-check不能提前绕过。P4复用这份共享物理请求预算，报告表关联长期尝试、成功报告实际模型ID/usage/耗时/结束原因；每个night_date跨输入、跨日累计最多10次，成功即停，不再另限制每天不同夜晚数量或同输入每日次数。P5仅在自动开关启用时进入报告用例。

已确认当前账户为Token Plan团队版。[官方使用规范](https://help.aliyun.com/zh/model-studio/token-plan-permissions-and-usage)限定交互式编程/Agent工具使用，不允许自动化脚本、自定义应用后端或非交互批量调用；团队版没有该范围的豁免。上述为此前查阅的公开范围，不等同对用户具体账户权益的核实。用户已明确确认现有URL/Key用于本机测试和云端每日09:00自动调用，本轮按该配置实施，不设置套餐类型推断阻塞、不静默切换入口；连接成功与供应商许可仍是不同事实。

用户显式指定新 Prompt 重读某晚时，调用也进入上述共享预算。当前报告在新候选通过发布核对后切换，具体操作与核验见[运行手册](../operations.md)和[核验报告](../verification.md)。

## 验收

离线实现已覆盖配置优先级/缺配置、两种URL互斥/尾缀、302不跟随、401/403/429/5xx分类、未知模型、JSON Schema不支持、超大响应、截断、子进程超时退出、预算重启保持、usage缺失和敏感信息隔离。返回正文注入Key或私有host会被拒绝，普通错误仅输出白名单代码。测试结果唯一记录在[核验报告](../verification.md)，不在本文复制计数。

当前 qwen3.8-max 的逐夜报告与 7 日总结已有真实发布证据。连接能力、输出结构及结论与证据之间的一致性分别验收，已发布正文与证据的核对结果见[现存报告全量核对](../verification.md#已发布报告全量核对2026-11-25)。

参考项目审计只能去掉Key/URL，不能自动去掉健康数据。SleepScape必须先由领域层构造最小输入；完整输入/报告只在私有库内留痕。价格取实际服务配置或账单，未知时费用显示未知，不能将token估计写成已知人民币费用。
