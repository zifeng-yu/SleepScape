/* Validated model prose is always text. Evidence values come from the saved snapshot. */
import { add, el } from "./dom.js";
import { FEELING_LABELS } from "./morning-feeling.js";

const CATEGORY = { sleep: "本晚睡眠", body_signals: "身体信号", previous_day: "前日背景", environment: "环境观察" };
const FACTOR = { temperature: "温度", humidity: "湿度", light: "光照", sound: "声音", training_load: "运动负荷", stress: "压力", sleep_schedule: "作息" };
const ACTION = { record: "记录", observe: "继续观察", environment: "环境观察建议" };
const LEVEL = { limited: "证据有限", moderate: "有观测依据" };
const SOURCES = { garmin: "Garmin", environment: "环境设备", derived: "程序计算" };
const PERIODS = { night: "本晚", previous_day: "前一日", baseline30: "此前30日基线", comparison: "相对个人基线" };
const UNITS = { min: "分钟", bpm: "次/分", ms: "ms", points: "分", load_units: "负荷单位", degC: "℃", percent: "%", lx: "lx", dBFS: "dBFS（相对声音）", count: "次" };
const METRICS = {
    "sleep.total_minutes": "睡眠总时长", "sleep.average_heart_rate": "睡眠平均心率", "sleep.night_average_hrv": "夜间平均HRV",
    "previous.training_load": "前日运动负荷", "previous.average_stress": "前日平均压力",
    "sleep.awake_minutes": "清醒累计时长", "sleep.light_minutes": "浅睡累计时长", "sleep.deep_minutes": "深睡累计时长", "sleep.rem_minutes": "REM累计时长",
    "sleep.score": "睡眠评分", "sleep.average_respiration": "平均呼吸",
    "sleep.body_battery_at_wake": "醒来时身体电量", "sleep.unmeasurable_minutes": "无法判断阶段累计时长", "previous.steps": "前日步数",
    "baseline.sleep_total_minutes": "个人睡眠时长基线", "baseline.average_heart_rate": "个人睡眠心率基线", "baseline.night_average_hrv": "个人夜间HRV基线", "baseline.training_load": "前30日日均运动负荷",
    "comparison.sleep_total_minutes_delta": "睡眠时长较基线变化", "comparison.average_heart_rate_delta": "睡眠心率较基线变化", "comparison.night_average_hrv_delta": "夜间HRV较基线变化", "comparison.training_load_percent": "前日负荷较30日日均变化",
    "environment.temperature_mean": "平均温度", "environment.humidity_mean": "平均湿度", "environment.illuminance_mean": "平均光照", "environment.sound_energy_mean": "声音能量均值",
    "environment.temperature_min": "最低温度", "environment.temperature_max": "最高温度", "environment.humidity_min": "最低湿度", "environment.humidity_max": "最高湿度",
    "environment.illuminance_min": "最低光照", "environment.illuminance_max": "最高光照", "environment.sound_peak": "声音峰值",
};
const LIMITATIONS = {
    wearable_estimates: "手表数据是可穿戴设备估计，不能代替医学检测。",
    relative_sound_only: "声音为未校准的相对dBFS，不能当作标准环境分贝或识别声源。",
    no_stage_timeline: "未取得睡眠分期时间区间，不能定位深睡、浅睡或REM发生时刻。",
    unobserved_lifestyle: "未记录的生活因素保持未知，例如咖啡因、酒精、疾病或药物。",
    clock_error_unknown: "环境时间误差未知，不能判断秒级先后或环境导致觉醒。",
    baseline_insufficient: "部分个人历史基线不足，不能据此判断偏离平时的程度。",
    sampling_gaps_present: "环境有允许范围内的采样缺口；缺采不代表安静、黑暗或正常。",
    event_detection_incomplete: "声音事件检测覆盖不完整，摘要数量不能当作整夜事件总数。",
    event_context_truncated: "仅提供部分声音事件摘要，不代表完整事件列表。",
    context_truncated: "为控制输入大小，部分可选背景未提供给模型。",
    minute_boundary_overlap: "睡眠边缘统计包含相交的完整分钟，可能含睡眠窗口外片段。",
    minute_statistics_include_boundary_outside_sleep: "睡眠边缘统计来自完整分钟，不能定位该分钟具体缺失的秒。",
    no_causal_inference: "单晚观测不能确定影响因素或证明因果关系。",
    minute_boundary_estimate: "睡眠边缘统计包含相交的完整分钟，不能定位缺样具体发生的秒。",
    timeline_omitted_for_budget: "受发送大小限制，未向模型提供可选环境分段；整夜事实和缺口仍保留。",
    optional_context_omitted_for_budget: "受发送大小限制，部分可选背景未提供给模型；核心证据与限制仍保留。",
    composite_metrics_not_independent: "综合评分可能使用同夜睡眠或心率等数据，不能当作独立影响原因。",
    single_night_not_causal: "单晚观测不能确定最大影响因素，也不能证明因果关系。",
};
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = (value, max = 1000) => typeof value === "string" && value.length > 0 && value.length <= max;
const instant = (value) => typeof value === "string" && /^20\d\d-\d\d-\d\dT/.test(value) && Number.isFinite(Date.parse(value));
const finite = (value) => typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1e9;
const number = (value) => finite(value) ? value !== 0 && Math.abs(value) < 0.01 ? value > 0 ? "<0.01" : ">-0.01" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value) : "未知";
const stamp = (value, time) => instant(value) ? time.dateTime(value, true) : "未知";
const evidenceId = (value) => typeof value === "string" && /^[FTE]\d{3}$/.test(value);
const label = (map, key, fallback = "未知") => typeof key === "string" && Object.hasOwn(map, key) ? map[key] : fallback;
const array = (value, max) => Array.isArray(value) && value.length <= max;
const idList = (value, ids) => array(value, 12) && value.length > 0 && new Set(value).size === value.length && value.every((id) => ids.has(id));
const range = (row) => instant(row.start_at) && instant(row.end_at) && Date.parse(row.start_at) < Date.parse(row.end_at);
const knownCount = (value) => Number.isSafeInteger(value) && value >= 0;

function validReport(value, day) {
    const report = object(value), content = object(report.content), evidence = object(report.evidence);
    if (report.night_date !== day || content.night_date !== day || content.status !== "complete" ||
        !instant(report.generated_at) || !["complete", "with_gaps"].includes(report.input_quality_state) ||
        content.input_quality_state !== report.input_quality_state ||
        ![true, false, null].includes(report.source_changed) ||
        !array(evidence.facts, 128) || !array(evidence.timeline, 96) || !array(evidence.events, 8) ||
        !array(content.observations, 8) || !array(content.hypotheses, 3) || !array(content.actions, 3) ||
        !array(content.limitations, 20) || !array(report.required_limitations, 20)) return false;
    const feeling = evidence.morning_feeling;
    if (Object.hasOwn(evidence, "morning_feeling") && (object(feeling) !== feeling || feeling.id !== "F000" ||
        !FEELING_LABELS.has(feeling.value) || Object.keys(feeling).length !== 2)) return false;
    const entries = [...evidence.facts, ...evidence.timeline, ...evidence.events, ...(feeling ? [feeling] : [])];
    const ids = new Set(entries.map((row) => object(row).id));
    if (ids.size !== entries.length || ![...ids].every(evidenceId)) return false;
    if (!evidence.facts.every((row) => object(row) === row && /^F\d{3}$/.test(row.id) &&
        text(row.metric, 80) && Object.hasOwn(UNITS, row.unit) && Object.hasOwn(SOURCES, row.source) && Object.hasOwn(PERIODS, row.period) &&
        ["valid", "unavailable"].includes(row.quality) && (row.value === null || finite(row.value)) &&
        (row.valid_n === null || knownCount(row.valid_n)) && (row.expected_n === null || knownCount(row.expected_n)))) return false;
    if (!evidence.timeline.every((row) => /^T\d{3}$/.test(object(row).id) && range(row) &&
        ["temperature_c", "humidity_pct", "illuminance_lux", "sound_dbfs"].every((key) => row[key] === null || finite(row[key])))) return false;
    if (!evidence.events.every((row) => /^E\d{3}$/.test(object(row).id) && range(row) &&
        finite(row.active_seconds) && row.active_seconds >= 0 && finite(row.max_1s_rms_dbfs) &&
        typeof row.merged === "boolean" && typeof row.incomplete === "boolean")) return false;
    const summary = object(content.summary);
    if (!text(summary.text, 300) || !idList(summary.evidence_ids, ids)) return false;
    if (!content.observations.every((row) => Object.hasOwn(CATEGORY, object(row).category) &&
        text(row.text, 300) && Object.hasOwn(LEVEL, row.evidence_level) && idList(row.evidence_ids, ids))) return false;
    if (new Set(content.observations.map((row) => row.category)).size !== 4) return false;
    if (!content.hypotheses.every((row) => Object.hasOwn(FACTOR, object(row).factor) &&
        text(row.text, 300) && row.evidence_level === "limited" && idList(row.evidence_ids, ids) &&
        array(row.alternatives, 3) && row.alternatives.length > 0 && row.alternatives.every((item) => text(item, 180)))) return false;
    if (!content.actions.every((row) => Object.hasOwn(ACTION, object(row).kind) &&
        text(row.text, 220) && text(row.observe, 180) && idList(row.evidence_ids, ids))) return false;
    if (!content.limitations.every((code) => text(code, 80)) || !report.required_limitations.every((code) => content.limitations.includes(code))) return false;
    return report.input_quality_state !== "with_gaps" || content.limitations.includes("sampling_gaps_present");
}

function metricName(row) {
    if (Object.hasOwn(METRICS, row.metric)) return METRICS[row.metric];
    const base = row.metric.replace(/^(?:baseline|comparison)\./, "").replace(/(?:_baseline|_delta|_change_pct|_percent_change)$/, "");
    if (Object.hasOwn(METRICS, base)) return `${PERIODS[row.period]} · ${METRICS[base]}`;
    return "其他已核验指标";
}

function facts(rows) {
    const result = el("dl", "analysis-facts");
    for (const [name, value] of rows) add(result, el("dt", "", name), el("dd", "", value));
    return result;
}

function evidenceCard(row, kind, time, day) {
    const card = el("article", "report-evidence-card");
    card.id = `report-${day}-${row.id}`;
    card.tabIndex = -1;
    const values = [];
    let title;
    if (kind === "fact") {
        title = metricName(row);
        values.push(["观测值", row.quality === "valid" && finite(row.value) ? `${number(row.value)} ${UNITS[row.unit]}` : "未提供有效值"],
            ["来源", SOURCES[row.source]], ["对应周期", PERIODS[row.period]]);
        if (row.valid_n !== null || row.expected_n !== null) values.push([row.period === "baseline30" ? row.metric === "baseline.training_load" ? "有效完整日／预期日" : "有效夜晚／预期夜晚" : "有效数量／预期数量", `${number(row.valid_n)} / ${number(row.expected_n)}`]);
        if (row.period === "night") values.push(["睡眠归属日", day]);
        if (row.period === "previous_day") { const d = new Date(`${day}T00:00:00Z`); d.setUTCDate(d.getUTCDate() - 1); values.push(["来源日", d.toISOString().slice(0, 10)]); }
    } else if (kind === "morning_feeling") {
        title = "晨起睡感";
        values.push(["自填睡感", FEELING_LABELS.get(row.value)], ["睡眠归属日", day]);
    } else {
        title = kind === "timeline" ? "环境时段摘要" : "声音事件摘要";
        values.push(["时段", `${stamp(row.start_at, time)} — ${stamp(row.end_at, time)}`]);
        if (kind === "timeline") {
            for (const [key, name, unit] of [["temperature_c", "温度", "℃"], ["humidity_pct", "湿度", "%"], ["illuminance_lux", "光照", "lx"], ["sound_dbfs", "相对声音", "dBFS"]]) values.push([name, row[key] === null ? "未知" : `${number(row[key])} ${unit}`]);
            for (const [key, name] of [["temperature_c", "温度"], ["relative_humidity_pct", "湿度"], ["illuminance_lux", "光照"], ["sound_relative", "声音"]]) {
                const coverage = object(object(row.coverage)[key]);
                values.push([`${name}有效／预期`, `${number(coverage.usable_count)} / ${number(coverage.expected_count)}`]);
            }
        } else values.push(["实际活跃时长", `${number(row.active_seconds)}秒`], ["最高1秒相对声音", `${number(row.max_1s_rms_dbfs)} dBFS`], ["摘要范围", `${row.merged ? "包含合并片段" : "单个候选片段"}；${row.incomplete ? "覆盖不完整" : "已知检测区间"}`]);
    }
    add(card, el("h4", "", `${row.id} · ${title}`), facts(values));
    if (kind === "event") card.append(el("p", "analysis-footnote", "摘要包络不等于整段持续声音，不识别人声、鼾声、声源或觉醒。"));
    return card;
}

function references(ids, targets, details) {
    const box = el("div", "report-references");
    box.setAttribute("aria-label", "依据");
    box.append(el("span", "", "依据"));
    for (const id of ids) {
        const button = el("button", "report-evidence-link", id);
        button.type = "button";
        button.setAttribute("aria-label", `查看证据 ${id}`);
        button.addEventListener("click", () => {
            const target = targets.get(id);
            if (!target) return;
            details.open = true;
            target.focus({ preventScroll: true });
            target.scrollIntoView({ block: "center", behavior: "auto" });
        });
        box.append(button);
    }
    return box;
}

function reportSection(data, day, time, executionLabels, errorLabels) {
    const section = el("section", "readiness-execution sleep-report");
    section.setAttribute("aria-label", "本晚睡眠解读");
    const report = data.report, state = data.execution_state;
    const badge = report ? "报告已生成" : label(executionLabels, state, "生成状态暂不可用");
    add(section, add(el("div", "analysis-heading"), el("h2", "", "本晚睡眠解读"), el("span", `analysis-badge report-badge ${report ? "complete" : ""}`, badge)));
    if (!report) {
        const descriptions = {
            ready: "数据已达到条件，等待显式分析或已启用的每日任务。刷新页面不会调用模型。",
            queued: "已具备生成条件，等待任务执行；尚无已发布结论。",
            running: "生成尚未完成，暂不显示未经校验的中间回答。稍后刷新查看。",
            waiting_data: "先补齐下方阻塞项。允许范围内的小缺口仍会保留，不要求伪造100%完整。",
            expired_unanalyzed: "已超出自动回查范围；不会为了分析而扩大采集日期。",
            not_configured: "数据检查独立运行。模型配置齐备后，才能生成睡眠分析。",
            disabled: "当前模型或自动分析开关关闭；已有数据仍继续采集和展示。",
            failed: "本次未得到通过校验的报告。保留数据和失败状态，后续按预算与重试规则处理。",
            discarded_input_changed: "生成期间证据发生变化，本次回答未发布。等待下一次检查后再分析。",
            blocked_budget: "共享的今日模型调用预算已用完，后续任务再按规则处理。",
            blocked_attempts: "该晚累计尝试已达上限，跨天或数据更新不会清零，需排查失败原因后处理。",
            blocked_input_budget: "完整证据超过发送大小上限，本次没有调用模型，也没有删去缺口来凑齐输入。",
            not_initialized: "需完成显式报告存储初始化；页面刷新不会自动迁移数据库。",
        };
        const description = ["queued", "ready"].includes(state) && data.automatic_enabled === false
            ? "数据已达到条件，但自动分析未启用。可通过显式分析任务生成报告；刷新页面不会调用模型。"
            : label(descriptions, state, "本晚尚无可展示的分析报告；数据与生成状态分别保留。");
        section.append(el("p", "report-note", description));
        const attempt = object(data.attempt);
        if (attempt.error_code) section.append(el("p", "analysis-footnote", label(errorLabels, attempt.error_code, "生成未通过，需查看安全运行状态。")));
        if (knownCount(attempt.attempt_count)) section.append(el("p", "analysis-footnote", `已记录尝试：${attempt.attempt_count}次`));
        if (instant(attempt.retry_after)) section.append(el("p", "analysis-footnote", `最早再尝试时间：${stamp(attempt.retry_after, time)}`));
        return section;
    }
    if (report.source_changed === true) {
        const policyChanged = Array.isArray(report.source_changed_reasons)
            && report.source_changed_reasons.includes("readiness_policy_changed");
        section.append(el("p", "report-source-warning", policyChanged
            ? "报告发布后，完整性规则已更新。下方检查使用当前规则，报告保留生成时的结论和证据。"
            : "报告发布后，依据数据已变化。这里保留原报告及原证据，不自动重写结论；请结合下方当前数据查看。"));
    } else if (report.source_changed === null) {
        section.append(el("p", "report-source-warning",
            "当前数据暂不可核对，无法确认报告依据是否变化。已发布报告和生成时证据仍保留。"));
    }
    const content = report.content, evidence = report.evidence;
    const feeling = evidence.morning_feeling ? [evidence.morning_feeling] : [];
    const detail = el("details", "report-evidence");
    add(detail, el("summary", "", `生成时证据（${evidence.facts.length + evidence.timeline.length + evidence.events.length + feeling.length}项）`),
        el("p", "analysis-footnote", "这些记录来自报告生成时的证据。阶段累计不代表具体分期时刻。"));
    const cards = el("div", "report-evidence-grid"), targets = new Map();
    for (const [kind, rows] of [["morning_feeling", feeling], ["fact", evidence.facts], ["timeline", evidence.timeline], ["event", evidence.events]]) for (const row of rows) {
        const card = evidenceCard(row, kind, time, day); cards.append(card); targets.set(row.id, card);
    }
    detail.append(cards);
    const summary = add(el("div", "report-summary"), el("h3", "", "本晚概况"), el("p", "report-prose", content.summary.text), references(content.summary.evidence_ids, targets, detail));
    section.insertBefore(summary, section.children[1] || null);
    const observations = el("div", "report-observations");
    for (const [category, name] of Object.entries(CATEGORY)) {
        const group = add(el("section", "report-observation-group"), el("h3", "", name));
        for (const row of content.observations.filter((item) => item.category === category)) add(group,
            el("p", "report-prose", row.text), el("p", "analysis-footnote", LEVEL[row.evidence_level]), references(row.evidence_ids, targets, detail));
        observations.append(group);
    }
    section.append(observations);
    const hypotheses = add(el("section", "report-hypotheses"), el("h3", "", "待验证线索"));
    if (!content.hypotheses.length) hypotheses.append(el("p", "report-prose", "本次没有提出额外假设；现有数据不能确定主要影响因素。"));
    for (const row of content.hypotheses) {
        const item = add(el("article", "report-item"), el("h4", "", `${FACTOR[row.factor]} · 证据有限`), el("p", "report-prose", row.text));
        const alternatives = el("ul", "analysis-missing");
        for (const alternative of row.alternatives) alternatives.append(el("li", "", alternative));
        add(item, el("p", "analysis-footnote", "其他可能解释"), alternatives, references(row.evidence_ids, targets, detail));
        hypotheses.append(item);
    }
    section.append(hypotheses);
    const actions = add(el("section", "report-actions"), el("h3", "", "下一步观察"));
    if (!content.actions.length) actions.append(el("p", "report-prose", "本次没有额外观察建议。"));
    for (const row of content.actions) add(actions, add(el("article", "report-item"), el("h4", "", ACTION[row.kind]), el("p", "report-prose", row.text), el("p", "analysis-footnote", `观察什么：${row.observe}`), references(row.evidence_ids, targets, detail)));
    const limitations = add(el("details", "readiness-details report-limitations"), el("summary", "", "查看解读限制")), list = el("ul", "analysis-missing");
    for (const code of new Set(content.limitations)) list.append(el("li", "", label(LIMITATIONS, code, "另有输入数据限制，不能将未观测内容视为正常或不存在。")));
    add(limitations, list); add(section, actions, limitations, detail);
    add(section, el("p", "report-meta", `睡眠归属日 ${day} · 生成于 ${stamp(report.generated_at, time)} · ${report.input_quality_state === "with_gaps" ? "生成时数据有允许范围内的缺口" : "生成时必需数据完整"}`),
        el("p", "analysis-footnote", "证据对应报告生成时的观测，仅供个人睡眠观察。"));
    return section;
}

function reportSummary(value, time) {
    const row = object(value);
    if (row.state === "complete") return `报告已生成 · ${stamp(row.generated_at, time)}`;
    if (row.state === "not_generated") return "尚未生成报告";
    if (row.state === "unavailable") return "报告记录暂不可用";
    return "暂无报告状态";
}

export { validReport, reportSection, reportSummary };
