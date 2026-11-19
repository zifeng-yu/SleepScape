/* Read-only analysis, validated reports and generation-time evidence. No HTML from models. */
import { add, el } from "./dom.js";
import { reportSection, validReport } from "./report.js";

const EXECUTION = {
    not_configured: "模型未配置",
    disabled: "模型未启用",
    invalid_configuration: "模型配置有误",
    ready: "等待分析",
    waiting_data: "等待数据达到分析条件",
    expired_unanalyzed: "超出自动分析范围",
    queued: "等待生成",
    running: "正在生成",
    failed: "本次生成未成功",
    blocked_budget: "今日调用额度已用完",
    blocked_attempts: "尝试次数已达上限",
    blocked_input_budget: "证据数据超过发送上限",
    complete: "报告已生成",
    discarded_input_changed: "生成期间数据变化，等待重新检查",
    not_initialized: "报告存储尚未初始化",
    storage_unavailable: "报告记录暂不可用",
    unavailable: "报告状态暂不可用",
    blocked_storage: "报告存储暂不可用",
    blocked_provider: "供应商运行条件尚未满足",
    blocked_configuration: "模型配置尚未就绪",
    model_busy: "模型任务进行中",

};
const CONFIGURATION = {
    not_configured: "未配置完整",
    disabled: "未启用",
    invalid_configuration: "配置有误",
    configured: "已配置",
};
const CONNECTION = {
    not_checked: "尚无当前配置的已完成请求",
    verified: "最近请求通过校验",
    failed: "最近请求未成功",
    configuration_changed: "配置已变更，已有请求仅供参考",
};
const MODEL = {
    "qwen3.8-max": "qwen3.8-max",
    "qwen4-max": "qwen4-max",
};
const ERROR = {
    model_not_configured: "模型尚未配置",
    model_configuration_invalid: "模型配置有误",
    model_key_missing: "模型密钥未配置",
    model_key_permissions: "密钥文件权限有误",
    model_key_invalid: "模型密钥格式有误",
    model_disabled: "模型未启用",
    invalid_model_request: "模型请求未通过检查",
    authentication_failed: "认证失败",
    permission_denied: "访问被拒绝",
    rate_limited: "请求受限，请稍后重试",
    network_error: "网络连接失败",
    request_timeout: "连接超时",
    model_deadline_exceeded: "模型请求超过时间限制",
    model_mismatch: "返回模型与配置不符",
    model_unavailable: "模型不可用",
    capability_unavailable: "所需接口能力不可用",
    invalid_response: "响应未通过校验",
    invalid_json: "响应不是有效 JSON",
    output_schema_mismatch: "响应结构未通过校验",
    output_schema_unique_items: "响应包含重复引用或限制",
    output_schema_contains: "响应缺少必需限制",
    output_schema_required: "响应缺少必需字段",
    output_schema_const: "响应固定字段不匹配",
    output_schema_type: "响应字段类型有误",
    output_schema_enum: "响应包含未允许的选项",
    output_schema_additional_properties: "响应包含未允许的字段",
    output_schema_min_items: "响应列表项目不足",
    output_schema_max_items: "响应列表项目过多",
    output_schema_min_length: "响应文字长度不足",
    output_schema_max_length: "响应文字过长",
    output_schema_pattern: "响应字段格式不符",
    output_schema_format: "响应标准格式不符",
    output_schema_condition: "响应条件约束未满足",
    output_schema_other: "响应未满足结构约束",
    output_type_report: "模型报告应为 JSON 对象",
    output_type_report_string: "模型返回字符串而非报告对象",
    output_type_report_array: "模型返回数组而非报告对象",
    output_type_report_array_empty: "模型返回空数组",
    output_type_report_array_multiple: "模型返回多项数组，不能确定唯一报告",
    output_type_report_array_single_nonobject: "模型数组内没有报告对象",
    output_type_report_null: "模型返回空值而非报告对象",
    output_type_report_scalar: "模型返回单个数值或真假值",
    output_type_summary: "模型摘要字段应为对象",
    output_type_evidence_ids: "模型证据引用应为字符串数组",
    output_type_observations: "模型观测字段应为数组",
    output_type_observation_item: "模型单条观测应为对象",
    output_type_hypotheses: "模型假设字段应为数组",
    output_type_hypothesis_item: "模型单条假设应为对象",
    output_type_actions: "模型建议字段应为数组",
    output_type_action_item: "模型单条建议应为对象",
    output_type_alternatives: "模型替代解释应为字符串数组",
    output_type_limitations: "模型限制说明应为字符串数组",
    output_type_text: "模型说明文字应为字符串",
    output_type_observe: "模型观察建议应为字符串",
    output_type_identity: "模型日期与证据标识应为字符串",
    response_too_large: "响应超过大小限制",
    output_truncated: "响应被截断",
    budget_exhausted: "今日模型请求次数已达上限",
    service_unavailable: "模型服务暂时不可用",
    http_error: "模型服务请求失败",
    model_operation_failed: "模型调用失败",
    model_check_failed: "模型核验失败",
    model_busy: "模型请求进行中，请稍后查看",
    model_state_unavailable: "请求状态不可用，需检查运行状态",
    model_worker_failed: "模型进程失败",
    analysis_not_initialized: "报告存储尚未初始化",
    analysis_storage_unavailable: "报告记录暂不可用",
    report_storage_unavailable: "报告记录暂不可用",
    reports_not_initialized: "报告存储尚未初始化",
    analysis_input_invalid: "证据未通过校验",
    analysis_output_invalid: "结论未通过校验",
    output_business_mismatch: "结论与证据不一致",
    input_budget_exceeded: "证据超过发送上限",
    input_changed: "生成期间证据发生变化",
    model_refused: "模型未返回可发布结论",
    model_insufficient_input: "模型认为证据不足",
    attempt_limit_reached: "尝试次数已达上限",
    current_data_unavailable: "当前数据无法读取，暂不能比较报告依据",

};
const METRICS = {
    "sleep.window": "睡眠起止时间",
    "sleep.total_minutes": "睡眠总时长",
    "sleep.average_heart_rate": "睡眠平均心率",
    "sleep.night_average_hrv": "夜间平均 HRV",
    "previous.training_load": "前日运动负荷",
    "previous.average_stress": "前日平均压力",
    "environment.window": "整夜环境时间窗口",
    "environment.time": "环境时间映射",
    "environment.configuration": "环境设备与摆位",
    temperature_c: "温度",
    relative_humidity_pct: "相对湿度",
    illuminance_lux: "光照",
    sound_relative: "相对声音",
    "sleep.stage_totals": "睡眠阶段累计时长",
    "sleep.stage_timeline": "睡眠分期时间区间",
    "sleep.score": "睡眠评分",
    "sleep.average_respiration": "睡眠平均呼吸",
    "sleep.spo2": "血氧",
    "sleep.body_battery": "身体电量",
    "previous.activities": "详细运动记录",
    "history.baseline": "个人历史基线",
    "environment.sound_events": "声音事件摘要",
    "lifestyle.context": "咖啡因、酒精等生活记录",
};
const OBSERVATION = {
    missing: "缺失",
    invalid: "无效",
    stale: "待补取",
    pending: "暂未就绪",
    available: "有效",
    unavailable: "暂不可用",
    source_mismatch: "来源不匹配",
    date_mismatch: "来源日期不匹配",
    not_closed: "睡眠窗口尚未闭合",
    too_long: "睡眠窗口超过范围",
    conflict: "观测存在冲突",
    unknown: "未知",
    truncated: "查询记录不完整",
    overlap: "时间窗口重叠",
    configuration_changed: "测量配置或摆位变化",
    not_provided: "来源未提供",
    counts_incomplete: "检测计数或覆盖不足",
    grid_mismatch: "分钟窗口不匹配",
    unsupported: "当前来源不支持此能力",
};
const DATA_STATE = {
    waiting_data: "必需数据待补齐",
    ready: "数据可供后续分析",
    expired_unanalyzed: "超出回查期限，未分析",
    not_evaluated: "尚未检查",
};
const QUALITY_STATE = {
    complete: "可分析，数据完整",
    with_gaps: "可分析，有数据缺口",
    insufficient: "数据不足，暂不可分析",
};
const POLICY_VERSION = "dual-source-coverage-1";
const GAP_CODE = {
    missing_garmin_core: "必需睡眠数据未满足条件",
    missing_previous_day: "前一日背景数据未满足条件",
    unknown_environment_time: "环境时间尚不能确定",
    time_conflict: "环境时间存在冲突",
    configuration_changed: "环境来源、配置或摆位不一致",
    missing_environment_minutes: "环境分钟窗口存在缺口",
    invalid_channel: "通道存在缺测或无效采样",
    insufficient_channel_coverage: "有效采样覆盖不足或预期数量未知",
    excessive_sampling_gap: "连续受影响分钟范围超过上限",
    optional_unavailable: "可选数据未提供或不可用",
};
const EXPECTED = {
    available: "有效观测",
    closed_window: "已结束且有效的睡眠窗口",
    complete_window: "完整时间窗口",
    full_coverage: "完整覆盖",
    single_configuration: "单一设备、配置和摆位",
    known_time: "可映射的时间",
};
const RECOVERY = {
    await_sync: "等待手表同步后回查",
    await_upload: "等待设备补传",
    unrecoverable: "已确认无法由补采恢复",
    unknown: "能否恢复尚不确定",
};
const SOURCES = { garmin: "Garmin", environment: "环境设备", manual: "手工记录" };
const LIMITATIONS = {
    wearable_measurement_uncertain: "手表观测存在测量误差，数据齐全不代表医学准确。",
    relative_sound_uncalibrated: "声音为未校准的相对强度，不能当作标准环境分贝。",
    minute_statistics_include_boundary_outside_sleep: "睡眠起止处的统计来自完整分钟，可能包含窗口外的片段。",
    clock_error_unknown: "环境时间可映射，但绝对时钟误差未知，不能做秒级因果判断。",
    environment_count_lower_bound: "分钟数量无法精确确定时，仅展示已知下界和时间缺口。",
    unknown_environment_time_possible: "存在尚不能映射时间的环境记录，不能确定其是否属于这一晚。",
    event_detection_incomplete: "声音事件检测存在预热或覆盖不足，摘要数量不能代表整夜事件总数。",
    minor_environment_gaps: "本晚存在允许范围内的环境缺口，后续分析仍需保留这些限制。",
};
const CHANNELS = ["temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"];
const GARMIN_FIELDS = [
    ["sleep_seconds", "sleep.total_minutes"],
    ["average_heart_rate", "sleep.average_heart_rate"],
    ["night_average_hrv", "sleep.night_average_hrv"],
    ["training_load", "previous.training_load"],
    ["previous_stress", "previous.average_stress"],
];

const object = (value) =>
    value && typeof value === "object" && !Array.isArray(value) ? value : {};
const known = (mapping, value, fallback) =>
    typeof value === "string" && Object.hasOwn(mapping, value)
        ? mapping[value]
        : fallback;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const date = (value) =>
    typeof value === "string" && /^20\d\d-\d\d-\d\d$/.test(value) &&
        Number.isFinite(Date.parse(`${value}T00:00:00Z`)) &&
        new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value
        ? value
        : null;
const instant = (value) => typeof value === "string" &&
    /^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) &&
    Number.isFinite(Date.parse(value));
const quantity = (value) => typeof value === "number" && Number.isFinite(value) &&
    value >= 0 && value <= Number.MAX_SAFE_INTEGER;
const numberText = (value) => quantity(value)
    ? value > 0 && value < 0.01 ? "<0.01"
        : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value) : "未知";
const coverageText = (value) => quantity(value) && value <= 1
    ? value > 0.9999 && value < 1 ? ">99.99%（未满100%）" : `${numberText(value * 100)}%`
    : "未知";
const timestamp = (value, time) => instant(value) && typeof time?.dateTime === "function"
    ? time.dateTime(value, true) : "未知";
const outcome = (value, mapping) => quantity(value) ? numberText(value)
    : known(mapping, value, "未知");

function facts(rows) {
    const list = el("dl", "analysis-facts");
    for (const [label, value] of rows) add(list, el("dt", "", label), el("dd", "", value));
    return list;
}

function missingList(values, time, mode = "required") {
    const list = el("ul", "readiness-missing");
    const rows = Array.isArray(values) ? values : [];
    for (const item of rows.slice(0, 64)) {
        const row = object(item), entry = el("li", "readiness-gap");
        const label = known(METRICS, row.metric, "未识别的数据项");
        add(entry, el("h4", "", label), el("p", "readiness-gap-reason",
            known(GAP_CODE, row.code, "该项未通过检查，详细状态暂不可用")));
        const expected = row.code === "insufficient_channel_coverage" ? coverageText(row.expected)
            : row.code === "excessive_sampling_gap" ? `${numberText(row.expected)}秒`
                : outcome(row.expected, EXPECTED);
        const observed = row.code === "insufficient_channel_coverage" ? coverageText(row.observed)
            : row.code === "excessive_sampling_gap" ? `${numberText(row.observed)}秒`
                : outcome(row.observed, OBSERVATION);
        const details = [
            ["来源", known(SOURCES, row.source, "未知来源")],
            [mode === "warning" ? "完整基准／观测" : "要求／观测", `${expected} / ${observed}`],
            ["恢复性", known(RECOVERY, row.recoverability, "能否恢复尚不确定")],
        ];
        if (date(row.date)) details.push(["归属日期", row.date]);
        if (instant(row.window_start) || instant(row.window_end))
            details.push(["相关时段", `${timestamp(row.window_start, time)} — ${timestamp(row.window_end, time)}`]);
        if (instant(row.last_checked_at)) details.push(["检查时间", timestamp(row.last_checked_at, time)]);
        entry.append(facts(details));
        if (mode === "optional") entry.append(el("p", "analysis-footnote", "可选项，不阻塞数据可用性检查。"));
        if (mode === "warning") entry.append(el("p", "analysis-footnote", "本项作为质量提示保留，不单独阻塞后续分析。"));
        list.append(entry);
    }
    if (rows.length > 64) list.append(el("li", "analysis-footnote", "记录较多，仅展示前64项；此列表不是全部记录。"));
    return list;
}

function environmentSummary(value) {
    const data = object(value), panel = el("section", "readiness-environment");
    panel.setAttribute("aria-label", "四通道完整性");
    add(panel, el("h3", "", "四通道完整性"), facts([
        ["应有／已收到分钟窗口", `${numberText(data.expected_windows)} / ${numberText(data.observed_windows)}`],
        ["缺少分钟窗口", count(data.missing_windows) ? numberText(data.missing_windows)
            : count(data.missing_windows_lower_bound) ? `精确数量未知，至少 ${numberText(data.missing_windows_lower_bound)}` : "未知"],
        ["睡眠窗口／已覆盖", `${numberText(data.expected_seconds)}秒 / ${numberText(data.covered_seconds)}秒`],
        ["时间缺口／重叠", `${numberText(data.missing_seconds)}秒 / ${numberText(data.overlap_seconds)}秒`],
    ]));
    if (data.count_exact === false) panel.append(el("p", "analysis-footnote",
        "部分分钟网格无法精确确定；未知数量不按0处理，时间缺口和数量下界分别保留。"));
    const cards = el("div", "readiness-channels"), channels = object(data.channels);
    for (const metric of CHANNELS) {
        const channel = object(channels[metric]), card = el("article", "readiness-channel");
        card.dataset.metric = metric;
        const sampleLabel = metric === "sound_relative" ? "有效／预期1秒窗口" : "有效／预期采样次数";
        const rows = [
            ["有效采样覆盖", coverageText(channel.effective_coverage_ratio)],
            [sampleLabel, `${numberText(channel.usable_count)} / ${numberText(channel.expected_count)}`],
            ["最长受影响范围", `${numberText(channel.max_affected_span_seconds)}秒`],
        ];
        if (metric === "sound_relative") rows.push(["已收分钟内已知音频缺样", `${numberText(channel.audio_missing_seconds)}秒`]);
        add(card, el("h4", "", METRICS[metric]), facts(rows));
        const full = el("details", "readiness-window-reference");
        add(full, el("summary", "", "完全有效分钟参考"), facts([
            ["完全有效分钟窗口", `${numberText(channel.valid_windows)} / ${numberText(channel.expected_windows)}`],
            ["非完全有效的已收窗口", numberText(channel.invalid_windows)],
            ["完全有效分钟覆盖", coverageText(channel.coverage_ratio)],
        ]));
        card.append(full);
        cards.append(card);
    }
    add(panel, cards, el("p", "analysis-footnote",
        "有效采样覆盖＝质量有效的采样数量÷预期数量；声音按1秒窗口计，其他通道按采样次数计。睡眠边缘按相交的完整分钟核算，不假定缺样发生在分钟内哪个时刻。"),
        el("p", "analysis-footnote", "最长受影响范围是连续受影响分钟的合并时长，属于保守范围，不能当作真实连续丢失音频的时长。音频缺样秒数仅统计已收到的完整分钟；完全有效分钟覆盖另作参考，不作为有效采样覆盖率。"));
    return panel;
}

function garminSummary(value) {
    const data = object(value), metrics = object(data.metrics), panel = el("section", "readiness-garmin");
    const rows = [["睡眠窗口", known(OBSERVATION, data.window_state, "未知")],
        ["有效必需字段", `${numberText(data.available_count)} / ${numberText(data.required_count)}`]];
    for (const [key, name] of GARMIN_FIELDS) {
        const item = object(metrics[key]);
        rows.push([METRICS[name], `${known(OBSERVATION, item.state, "未知")}${date(item.source_date) ? ` · ${item.source_date}` : ""}`]);
    }
    return add(panel, el("h3", "", "Garmin必需数据"), facts(rows));
}

function modelRows(model, time, context = null) {
    const data = object(model),
        list = el("dl", "analysis-facts");
    const rows = [
        ["模型", known(MODEL, data.model, "暂无模型信息")],
        [
            "配置状态",
            known(CONFIGURATION, data.configuration_state, "暂无配置信息"),
        ],
        [
            "最近请求状态（共享）",
            data.runtime_state === "unavailable" ? "请求记录暂不可用"
                : data.last_check_status === "running" ? "请求进行中"
                    : known(CONNECTION, data.connection_state, "暂无请求信息"),
        ],
    ];
    if (context !== null) {
        rows.unshift(["本晚报告", context.report ? "已生成" : known(EXECUTION, context.execution_state, "报告状态暂不可用")]);
        if (context.report) rows.splice(1, 0, ["报告生成时间", time.dateTime(context.report.generated_at, true)]);
    }
    if (typeof data.enabled === "boolean")
        rows.push(["模型开关", data.enabled ? "已启用" : "未启用"]);
    if (data.last_check_status === "running")
        rows.push(["当前请求", "模型请求进行中，请稍后刷新"]);
    if (data.runtime_state === "unavailable")
        rows.push(["运行状态", "请求状态不可用，当前无法调用模型"]);
    if (
        typeof data.last_check_at === "string" &&
        /^20\d\d-\d\d-\d\dT/.test(data.last_check_at) &&
        Number.isFinite(Date.parse(data.last_check_at))
    )
        rows.push(["最近请求时间（共享）", time.dateTime(data.last_check_at, true)]);
    if (data.last_error_code)
        rows.push([
            "该次请求结果",
            known(ERROR, data.last_error_code, "模型请求未成功，请检查运行状态"),
        ]);
    const usage = object(data.usage);
    for (const [key, label] of [
        ["prompt_tokens", "输入 Token"],
        ["completion_tokens", "输出 Token"],
        ["total_tokens", "合计 Token"],
    ])
        if (count(usage[key])) rows.push([label, String(usage[key])]);
    if (Object.keys(usage).length) rows.push(["费用", "未知"]);
    const budget = object(data.budget);
    if (count(budget.used) && count(budget.limit))
        rows.push(["今日模型请求次数", `${budget.used} / ${budget.limit}`]);
    if (count(budget.remaining))
        rows.push(["今日剩余请求次数", String(budget.remaining)]);
    for (const [label, value] of rows)
        add(list, el("dt", "", label), el("dd", "", value));
    return list;
}

function modelDetails(data, time) {
    const panel = el("details", "readiness-details model-details");
    return add(panel, el("summary", "", "模型配置与请求记录"), modelRows(data.model, time, data),
        el("p", "analysis-footnote", data.report
            ? "本晚报告已成功生成。共享调用记录中的失败不撤销已发布报告；最近一次请求可能来自探针或其他报告任务。"
            : "共享调用记录包含探针和各夜晚报告请求，不能据此判断本晚是否已生成报告；请以本晚报告状态为准。"));
}

function analysisPanel() {
    const panel = el("section", "card analysis-card");
    panel.id = "sleep-analysis-panel";
    panel.setAttribute("aria-label", "数据完整性与睡眠分析");
    panel.setAttribute("aria-live", "polite");
    panel.setAttribute("aria-busy", "true");
    add(
        panel,
        el("h2", "", "本晚睡眠解读"),
        el("p", "analysis-note", "正在读取本晚报告…"),
    );
    return panel;
}

function renderAnalysis(panel, payload, requestedDate, time) {
    const data = object(payload);
    if (data.night_date !== requestedDate || !date(requestedDate) ||
        data.implementation_stage !== "reports" || !Object.hasOwn(EXECUTION, data.execution_state)) {
        renderAnalysisUnavailable(panel);
        return;
    }
    if ((data.execution_state === "complete" && data.report === null) ||
        (data.report !== null && !validReport(data.report, requestedDate))) {
        renderAnalysisUnavailable(panel);
        return;
    }
    if (data.readiness_unavailable === true && data.readiness === null) {
        const model = modelDetails(data, time);
        panel.replaceChildren(reportSection(data, requestedDate, time, EXECUTION, ERROR),
            el("h2", "", "数据完整性检查"),
            el("p", "analysis-note", "当前完整性暂不可读取；不把读取失败当作数据缺失，已有报告仍按生成时证据展示。"), model);
        panel.setAttribute("aria-busy", "false");
        return;
    }
    const readiness = object(data.readiness), state = data.data_state;
    const policyValues = object(readiness.policy), quality = readiness.quality_state;
    const evaluated = state !== "not_evaluated";
    if (!Object.hasOwn(DATA_STATE, state) ||
        (evaluated && (readiness.policy_version !== POLICY_VERSION ||
            !Object.hasOwn(QUALITY_STATE, quality) ||
            !quantity(policyValues.min_channel_coverage) || policyValues.min_channel_coverage <= 0 || policyValues.min_channel_coverage > 1 ||
            !quantity(policyValues.max_affected_span_seconds) || policyValues.max_affected_span_seconds > 86400 ||
            readiness.night_date !== requestedDate || readiness.data_state !== state ||
            !instant(readiness.checked_at) || typeof readiness.core_ready !== "boolean" ||
            !Array.isArray(readiness.missing) || !Array.isArray(readiness.optional_missing) || !Array.isArray(readiness.warnings) ||
            (state === "ready" && (!readiness.core_ready || readiness.missing.length !== 0)) ||
            (state === "waiting_data" && readiness.core_ready) ||
            (readiness.core_ready !== (readiness.missing.length === 0)) ||
            (quality !== (readiness.missing.length ? "insufficient" : readiness.warnings.length ? "with_gaps" : "complete"))))) {
        renderAnalysisUnavailable(panel);
        return;
    }
    const missing = Array.isArray(readiness.missing) ? readiness.missing : [];
    const metrics = [...new Set(missing.map((item) => known(METRICS, object(item).metric, "未识别的数据项")))];
    const descriptions = {
        waiting_data: `尚有${missing.length}项阻塞：${metrics.slice(0, 2).join("、")}${metrics.length > 2 ? "等" : ""}。`,
        expired_unanalyzed: readiness.core_ready
            ? `本晚已超出自动回查期限；本次检查${quality === "with_gaps" ? "可分析，但有数据缺口" : "必需观测完整，可分析"}。`
            : "本晚已超出自动回查期限，仍未满足可用性要求。",
        not_evaluated: "本晚尚无可用的完整性检查，不能据此判断数据齐全或缺失。",
    };
    const label = state === "ready" || state === "waiting_data" ? QUALITY_STATE[quality] : DATA_STATE[state];
    const head = add(el("div", "analysis-heading quality-heading"), el("h2", "", "数据完整性检查"),
        el("span", `analysis-badge readiness-badge ${state}${quality === "with_gaps" ? " with-gaps" : ""}`, label));
    panel.replaceChildren(reportSection(data, requestedDate, time, EXECUTION, ERROR), head);
    if (descriptions[state]) panel.append(el("p", "analysis-note", descriptions[state]));
    if (evaluated) {
        const details = add(el("details", "readiness-details quality-details"),
            el("summary", "", "查看完整性检查详情"));
        panel.append(details);
        const window = object(readiness.window);
        details.append(facts([
            ["本次检查", timestamp(readiness.checked_at, time)],
            ["睡眠归属日", requestedDate],
            ["睡眠窗口", `${timestamp(window.start, time)} — ${timestamp(window.end, time)}`],
            ["窗口时长", `${numberText(window.duration_seconds)}秒`],
            ["检查策略", `覆盖与缺口规则（${POLICY_VERSION}）`],
            ["有效覆盖下限", coverageText(policyValues.min_channel_coverage)],
            ["连续受影响范围上限", `${numberText(policyValues.max_affected_span_seconds)}秒（保守范围）`],
        ]));
        const required = el("section", "readiness-required");
        required.setAttribute("aria-label", "阻塞项");
        add(required, el("h3", "", `阻塞项（${readiness.missing.length}项）`));
        if (readiness.missing.length) required.append(missingList(readiness.missing, time));
        else required.append(el("p", "analysis-note", "本次检查没有阻塞项；数据缺口单独保留在质量提示中。"));
        const warnings = el("details", "readiness-details readiness-warnings");
        warnings.setAttribute("aria-label", "数据缺口与质量提示");
        add(warnings, el("summary", "", `数据缺口与质量提示（${readiness.warnings.length}项，不单独阻塞）`));
        if (readiness.warnings.length) warnings.append(missingList(readiness.warnings, time, "warning"));
        else warnings.append(el("p", "analysis-footnote", "本次检查没有额外的环境质量提示。"));
        add(details, required, warnings, environmentSummary(readiness.environment_summary));
        const garmin = el("details", "readiness-details");
        add(garmin, el("summary", "", "查看 Garmin 必需字段及来源日期"), garminSummary(readiness.garmin_summary));
        const optional = el("details", "readiness-details readiness-optional");
        add(optional, el("summary", "", `可选缺项（${readiness.optional_missing.length}项，不阻塞）`),
            readiness.optional_missing.length ? missingList(readiness.optional_missing, time, "optional")
                : el("p", "analysis-footnote", "本次检查没有已知的可选缺项。"));
        add(details, garmin, optional);
        const policy = el("details", "readiness-details readiness-policy");
        add(policy, el("summary", "", "检查规则与测量局限"),
            el("p", "analysis-note", `睡眠窗口有效且已结束，5项 Garmin 必需字段有有效观测，环境时间与来源一致；四个通道各自有效采样覆盖至少${coverageText(policyValues.min_channel_coverage)}，最长连续受影响分钟范围不超过${numberText(policyValues.max_affected_span_seconds)}秒，才允许进入后续分析。`),
            el("p", "analysis-footnote", "这些阈值是初始工程参数，不是医学保证。小缺口允许保留，但不填0、不隐藏；核心字段、时间未知或冲突、配置不一致仍阻塞。声音事件、分期时间线等可选项不阻塞。"));
        const limitations = el("ul", "analysis-missing");
        const codes = Array.isArray(readiness.limitations) ? readiness.limitations : [];
        for (const code of new Set(codes.slice(0, 32))) {
            const label = known(LIMITATIONS, code, null);
            if (label) limitations.append(el("li", "", label));
        }
        if (limitations.children.length) policy.append(limitations);
        details.append(policy);
    }
    panel.append(modelDetails(data, time));
    panel.setAttribute("aria-busy", "false");
}

function renderAnalysisUnavailable(panel, unauthorized = false) {
    panel.replaceChildren(
        el("h2", "", "数据完整性检查"),
        el(
            "p",
            "analysis-note",
            unauthorized
                ? "分析状态未获访问授权，请重新登录后刷新。"
                : "暂时无法读取分析状态，请稍后刷新。",
        ),
        el(
            "p",
            "analysis-footnote",
            "当前无法判断完整性，不将读取失败当作数据缺失。其他已取得的观测继续显示；没有可展示的分析结论。",
        ),
    );
    panel.setAttribute("aria-busy", "false");
}

function modelStatusPanel(model, time) {
    const panel = el("section", "card analysis-card");
    panel.id = "model-status-panel";
    panel.setAttribute("aria-label", "模型连接与分析状态");
    return add(
        panel,
        el("h2", "", "模型连接与分析状态"),
        modelRows(model, time),
        el(
            "p",
            "analysis-footnote",
            "此处是共享模型请求记录，包含探针和各夜晚报告请求；单次失败不表示已有报告未发布。报告是否已生成请查看对应夜晚详情。每晚最多发布一份报告，页面读取不会触发模型调用。",
        ),
    );
}

function analysisSummary(summary, time) {
    const data = object(summary);
    if (data.unavailable === true) return "检查记录暂不可用，请稍后刷新";
    if (typeof data.state === "string" && Object.hasOwn(DATA_STATE, data.state)) {
        if (data.state === "not_evaluated") return "尚未检查";
        const checked = `上次检查：${timestamp(data.checked_at, time)}`;
        if (data.policy_version !== POLICY_VERSION || data.current_policy !== true) {
            const label = data.state === "ready" ? "必需数据已齐全" : DATA_STATE[data.state];
            return `旧规则检查，待按当前规则复查 · 当时结果：${label} · ${checked}`;
        }
        const quality = known(QUALITY_STATE, data.quality_state, "质量状态未知");
        const label = data.state === "expired_unanalyzed" ? `${DATA_STATE[data.state]} · ${quality}` : quality;
        return `${label} · ${checked}`;
    }
    return known(EXECUTION, data.state, "暂无分析状态");
}

export {
    analysisPanel,
    renderAnalysis,
    renderAnalysisUnavailable,
    modelStatusPanel,
    analysisSummary,
};
