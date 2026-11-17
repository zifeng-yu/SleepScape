# 可替换模型连接层方案

更新：2026-11-17。**设计已明确，`src/sleepscape/models/`尚未实现；URL、Key及真实连接核验待提供。** 本文只维护通用连接层；睡眠输入、Prompt、报告和预算见[LLM方案](llm.md)。

## 参考与分层

已只读核对本机 `simulate-china-stocks/src/china_stock_sim/models/` 的 `base.py`、`settings.py`、`factory.py`、`openai_compatible.py`、`audit.py`，以及 `strategies/llm_call.py`。沿用接口分层，不复制交易账户、账本、盘前截止或交易重试规则，也不读取该项目真实模型配置和密钥。

| 待建文件 | 职责 |
| --- | --- |
| `src/sleepscape/models/base.py` | `ChatMessage`、`ModelRequest`、`ModelResult`、类型化`ModelError`和`ModelClient.complete()`协议 |
| `models/settings.py` | 从现有`Settings`和非敏感模型配置加载稳定引用；配置校验、离线状态，不另写`.env`解析器 |
| `models/factory.py` | 根据provider创建客户端；业务只认识`sleep_analyst` |
| `models/openai_compatible.py` | 请求映射、响应大小/状态校验、结构化输出参数与usage提取，不自行重试 |
| `models/audit.py` | 明确的脱敏元数据白名单；不打印配置对象、请求正文、健康值、响应正文或私有URL |
| `configs/models.toml` | 将`sleep_analyst`映射到`openai_compatible / qwen4-max`，保存非敏感参数及配置引用 |
| `services/model_call.py` | 有界子进程调用、总截止与进程回收；不让慢模型占住Web请求 |

现有`domain/models.py`是Garmin领域数据模型，保持用途。新`models/`仅封装远端模型连接，不读取数据库、不决定睡眠结论、不放Prompt、不调度、不写报告。依赖方向为`services → domain/models/storage`，其中domain与storage不反向依赖模型客户端。

## 配置合同

未来新增设置：`SLEEPSCAPE_LLM_ENABLED=false`、`SLEEPSCAPE_LLM_MODEL_REF=sleep_analyst`、`SLEEPSCAPE_LLM_BASE_URL`或互斥的`SLEEPSCAPE_LLM_ENDPOINT`、`SLEEPSCAPE_LLM_KEY_FILE=.secrets/sleep-llm-key`。这里只定义名字，当前Settings尚不读取这些项，不向现有`.env.example`写入看似已生效的参数。

优先级：显式进程环境 > 现有Settings读取的私有`.env` > TOML默认。Key只读文件，不放TOML、命令行参数、Git或前端；权限沿用目录700/文件600。缺少URL/Key时状态`not_configured`，基础采集与看板继续运行。用户本轮已指定向该服务发送必要睡眠摘要；启用仍以完整配置和非敏感连接检查成功为条件。

URL必须HTTPS；拒绝用户名密码、查询串、片段、空host。`base_url`统一去末尾斜杠后只追加一次`/chat/completions`；完整endpoint模式要求已有此固定尾缀。不能同时填两种地址。仅访问配置的确切目标，不跟随重定向，不由模型响应决定host；私有网关若有特殊路由需显式适配，不猜测替换URL。

## 接口与调用边界

请求：模型引用、system/user消息、输出Schema、`max_completion_tokens`及经探测支持的选项。结果：文本、服务返回模型名、finish_reason、白名单usage、耗时、请求ID（如非敏感）。错误：稳定`code`、可选HTTP状态、可选`retry_after`、是否可重试；响应原文不作为用户错误消息。

固定使用`qwen4-max`，不默默切到预览版或便宜模型。按2026-11-17的[官方模型页](https://help.aliyun.cn/zh/model-studio/qwen4-max)，该ID及OpenAI兼容接口、JSON Schema可用；这不证明用户稍后提供的网关也支持。接入时用合成消息探测模型ID、JSON Schema、usage、超时和参数；记录能力指纹。服务返回的版本化模型名须在已核对的同模型映射中，否则报`model_mismatch`。

优先Chat Completions、非流式、`response_format=json_schema`且strict；兼容网关不支持时标`capability_unavailable`，先报告，不静默退为自由文本。即使服务声称严格Schema，业务层仍做结构、证据与边界校验。[结构化输出说明](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-structured-output)

设计合同同时含输入和输出定义；发送给服务的response_format只提取`$defs.output`及其引用定义，根节点必须为输出对象，不直接发送只有`$defs`的整份合同。Schema方言/支持子集由合成能力检查核对，不能把本地Draft校验通过当作服务已支持。

默认输出总预算4096 token；Qwen4的思考内容与最终回复共享输出预算，不能套旧模型的max_tokens语义。[兼容性变更](https://help.aliyun.cn/zh/model-studio/api-compatibility-upgrade-notice)
不同时传max_tokens/max_completion_tokens。低成本默认请求`enable_thinking=false`，仅在能力检查确认支持后使用；其他参数（temperature、seed等）不盲目发送。不支持关闭思考时先以合成数据核验4096总预算能完成结构化输出，再明确记录实际配置。输出截断/无效JSON计失败，不把半段报告作为成功。

单次硬截止180秒（包含连接、读取和解析），响应上限256KiB；超时终止并回收子进程。单并发，适配器内部零重试；重试与每日预算统一由睡眠编排承担，避免SDK和业务重试相乘。外呼时不得持SQLite事务、数据库写锁或Garmin采集锁。

`model-status`、doctor、页面GET只看配置/已存状态。未来`model-check --live`仅发送一次合成提示并记录请求预算；无凭据时不发起请求。未收到URL/Key，本轮没有模型调用、费用或真实健康数据外发。

## 验收

离线：配置优先级/缺配置、两种URL互斥/尾缀、302不跟随、401/403/429/5xx分类、未知模型、JSON Schema不支持、超大响应、截断、子进程超时退出、usage缺失、敏感字段不进入普通日志。给响应注入Key或私有host也应脱敏。

联网：用户提供URL/Key后，仅用合成输入核对模型ID、参数、结构化输出、usage及总时限；再按授权进入真实摘要分析。协议支持、真实可用、报告正确性分别验收。

参考项目审计只能去掉Key/URL，不能自动去掉健康数据。SleepScape必须先由领域层构造最小输入；完整输入/报告只在私有库内留痕。价格取实际服务配置或账单，未知时费用显示未知，不能将token估计写成已知人民币费用。
