import { readinessGapLabel } from "./analysis.js";
import { api } from "./api.js";
import { add, el } from "./dom.js";
import { duration, number, numericText, validDate } from "./format.js";
import { FEELING_LABELS } from "./morning-feeling.js";

const STATUS = new Map(Object.entries({
    disabled: "尚未启用自动总结",
    not_initialized: "总结存储尚未初始化",
    not_run: "尚无该日的定时总结",
    running: "正在生成总结",
    readiness_unavailable: "完整性核验暂不可读取",
    blocked: "本轮总结暂不可生成",
    failed: "本轮总结生成失败",
    interrupted: "本轮总结生成已中断",
    source_changed: "生成期间来源变化，本轮结果未保存",
    unavailable: "总结暂不可读取",
}));
const BLOCKED = new Map(Object.entries({
    budget_exhausted: "当日共享模型额度已用完",
    rate_limited: "模型限流冷却中",
    model_busy: "模型正在处理其他任务",
    review_deadline: "本轮日任务剩余时间不足",
    review_input_too_large: "本段证据超过输入大小限制",
    storage_capacity_restricted: "存储容量受限",
    model_key_missing: "模型凭据尚未配置",
}));
const METRICS = new Map(Object.entries({
    sleep_seconds: ["主睡眠时长", "秒"],
    sleep_start_offset_seconds: ["入睡", ""],
    sleep_end_offset_seconds: ["醒来", ""],
    average_heart_rate: ["夜间平均心率", "次/分"],
    night_average_hrv: ["昨夜 HRV", "ms"],
    training_load: ["昨日负荷", ""],
    temperature_c: ["温度", "℃"],
    relative_humidity_pct: ["湿度", "%"],
    illuminance_lux: ["照度", "lx"],
    sound_relative: ["相对声音", "dBFS"],
}));
const LIMITS = new Map(Object.entries({
    low_light_limit: "部分照度处于传感器低光限制内。",
    environment_groups_differ: "按相同设备、配置及摆位分别比较环境指标。",
    clock_error_unknown: "环境时钟的绝对误差未知。",
    minute_statistics_include_boundary_outside_sleep: "睡眠边缘使用完整分钟统计。",
}));

function dateLabel(day) {
    return `${Number(day.slice(5, 7))} 月 ${Number(day.slice(8, 10))} 日`;
}

function metricValue(metric, value) {
    if (!number(value)) return "暂无记录";
    if (metric.endsWith("_offset_seconds")) {
        const day = Math.floor(value / 86400);
        const minute = Math.floor((value - day * 86400) / 60);
        const hour = String(Math.floor(minute / 60)).padStart(2, "0");
        const prefix = day === -1 ? "前日 " : day < -1 ? `前${-day}日 ` : "";
        return `${prefix}${hour}:${String(minute % 60).padStart(2, "0")}`;
    }
    if (metric === "sleep_seconds") return duration(value);
    return [numericText(value, 2), METRICS.get(metric)?.[1]].filter(Boolean).join(" ");
}

function evidenceDetails(row, byID) {
    const detail = el("details", "weekly-evidence");
    if (row.id.startsWith("P")) {
        detail.append(el("summary", "", `${METRICS.get(row.metric)?.[0] || "指标"}与睡感 · ${row.pair_n} 对，涉及 ${row.night_n} 晚`));
        if (row.max_duration_gap_seconds !== null) detail.append(el("p", "", `所比夜晚的时长差最多为 ${numericText(row.max_duration_gap_seconds, 1)} 秒。`));
        for (const [label, ids] of [["例证", row.example], ["反例", row.counterexample]]) {
            if (!ids.length) continue;
            detail.append(el("p", "", label));
            for (const id of ids) {
                const night = byID.get(id);
                if (night?.night_date) detail.append(evidenceDetails(night, byID));
            }
        }
        return detail;
    }
    if (row.night_date && validDate(row.night_date)) {
        detail.append(el("summary", "", `${dateLabel(row.night_date)}的夜晚证据`));
        const link = el("a", "history-date", `打开 ${row.night_date} 夜晚详情`);
        link.href = `${location.pathname}?view=night&date=${encodeURIComponent(row.night_date)}`;
        detail.append(link);
        const values = el("ul");
        if (row.feeling) values.append(el("li", "", `晨起睡感 ${FEELING_LABELS.get(row.feeling) || "未填写"}`));
        for (const [name, value] of Object.entries(row.metrics || {})) {
            if (METRICS.has(name)) values.append(el("li", "", `${METRICS.get(name)[0]} ${metricValue(name, value)}`));
        }
        detail.append(values);
        return detail;
    }
    const label = METRICS.get(row.metric)?.[0] || "汇总指标";
    const group = /^G[1-7]$/.test(row.environment_group || "") ? ` · 环境组 ${row.environment_group.slice(1)}` : "";
    detail.append(el("summary", "", `${label}${group} · ${row.id.startsWith("C") ? "睡感分组对照" : "中位数"}`));
    if (row.id.startsWith("C")) {
        for (const [key, feeling] of [["poor", "较差"], ["good", "较好"]]) {
            detail.append(el("p", "", `${feeling} ${metricValue(row.metric, row[`${key}_median`])} · ${row[`${key}_n`]} 晚`));
        }
    } else {
        detail.append(el("p", "", `${metricValue(row.metric, row.median)} · ${row.valid_n} 晚`));
    }
    return detail;
}

function appendReport(body, data, time) {
    const { content, evidence, generated_at } = data.report;
    const byID = new Map(
        [...evidence.aggregates, ...evidence.comparisons, ...evidence.nights, ...(evidence.pair_checks || [])].map((row) => [row.id, row]),
    );
    const counts = evidence.quality.feeling_counts;
    const ratings = counts ? counts.poor + counts.fair + counts.good : null;
    body.append(el("p", "weekly-report-note", `生成于 ${time.dateTime(generated_at)} · 核验通过 ${evidence.window.eligible_n}/${evidence.window.visible_n} 晚 · ${ratings === null ? "睡感暂不可读" : `已填睡感 ${ratings} 晚`}`));
    if (data.source_changed === true) body.append(el("p", "weekly-report-note", "复盘依据已更新，以下为生成时的结果。"));
    if (data.source_changed === null) body.append(el("p", "weekly-report-note", "暂无法核对当前来源，以下为生成时的结果。"));
    if (evidence.nights.some((night) => night.quality === "with_gaps")) {
        body.append(el("p", "weekly-report-note", "部分夜晚有允许范围内的缺采。"));
    }
    for (const code of evidence.quality.limits) {
        if (LIMITS.has(code)) body.append(el("p", "weekly-report-note", LIMITS.get(code)));
    }
    for (const [title, statements] of [
        ["概况", [content.summary]], ["观察", content.observations],
        ["待验证线索", content.hypotheses], ["接下来观察", content.actions],
    ]) {
        if (!statements.length) continue;
        const section = add(el("section", "weekly-statement"), el("h3", "", title));
        for (const statement of statements) {
            section.append(el("p", "", statement.text));
            if (statement.alternative) section.append(el("p", "", `另一个可能解释是${statement.alternative}`));
            if (statement.observe) section.append(el("p", "", `观察项 ${statement.observe}`));
            for (const id of statement.evidence_ids) {
                const row = byID.get(id);
                if (row) section.append(evidenceDetails(row, byID));
            }
        }
        body.append(section);
    }
}

export function weeklyReportPanel(window, signal, time) {
    const range = `${window.start_date} — ${window.end_date}`;
    const body = el("div", "weekly-report-body");
    const panel = add(
        el("details", "card weekly-report"),
        el("summary", "", `7 日总结 · ${range}`),
        body,
    );
    const status = el("p", "weekly-report-note", "正在读取总结");
    status.setAttribute("role", "status");
    body.append(status);
    api(`sleep-reviews/${window.end_date}`, signal).then((data) => {
        if (signal.aborted || !panel.isConnected) return;
        if (data.window?.start_date !== window.start_date || data.window?.end_date !== window.end_date) {
            status.textContent = "总结范围暂不可核对";
            return;
        }
        if (data.report) {
            if (data.report.evidence?.window?.start_date !== window.start_date
                || data.report.evidence?.window?.end_date !== window.end_date) {
                status.textContent = "总结范围暂不可核对";
                return;
            }
            body.replaceChildren();
            appendReport(body, data, time);
        } else if (data.state === "incomplete_data") {
            const entries = (data.incomplete || []).filter((entry) =>
                validDate(entry.date) && entry.date >= window.start_date && entry.date <= window.end_date,
            );
            if (!entries.length) {
                status.textContent = "完整性核验暂不可读取";
                return;
            }
            panel.querySelector("summary").textContent += ` · ${entries.length} 日数据完整度不够`;
            body.replaceChildren(...entries.map((entry) => add(
                el("section", "weekly-statement"),
                el("p", "", `${dateLabel(entry.date)}数据完整度不够`),
                ...[...new Set((entry.missing || []).map(readinessGapLabel))].map(
                    (label) => el("p", "weekly-report-note", label),
                ),
            )));
            panel.open = true;
        } else if (data.state === "ready") {
            panel.querySelector("summary").textContent += " · 完整度已通过";
            status.textContent = "窗口内各晚均已通过完整性检查，总结尚未生成。";
        } else {
            status.textContent = (data.state === "blocked" && BLOCKED.get(data.error_code))
                || STATUS.get(data.state) || "总结暂不可读取";
        }
    }).catch(() => {
        if (!signal.aborted && panel.isConnected) {
            body.replaceChildren(el("p", "weekly-report-note", "总结暂不可读取"));
        }
    });
    return panel;
}
