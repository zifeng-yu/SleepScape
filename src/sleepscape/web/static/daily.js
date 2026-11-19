/* Read-only daily collection metadata. Unknown fields and raw errors never render. */
import { add, el } from "./dom.js";
import { createTimeFormatter, validDate } from "./format.js";

const STATES = {
    not_run: "尚未运行",
    running: "正在运行",
    completed: "本轮请求已完成",
    partial: "部分请求未完成",
    failed: "本轮运行失败",
    deferred: "等待服务允许重试",
    interrupted: "运行已中断",
};
const PHASES = {
    planned: "等待采集",
    preparing: "准备采集",
    collecting: "采集 Garmin 数据",
    collection: "采集 Garmin 数据",
    summarizing: "整理采集状态",
    observations: "检查已保存的观测",
    finished: "本轮结束",
    completed: "本轮结束",
    deferred: "等待重试窗口",
};
const ERRORS = {
    daily_state_unavailable: "日任务记录暂不可用，请检查运行状态",
    daily_already_running: "已有采集任务正在运行",
    collection_already_running: "已有采集任务正在运行",
    retry_after_deferred: "服务要求延后请求",
    retry_after_wait_still_active: "仍在服务要求的等待时间内",
    authorization_failed: "采集授权失败，需检查配置",
    authentication_rejected: "采集授权失败，需检查配置",
    authentication_retry_exhausted: "认证重试未成功",
    authentication_network_retry_exhausted: "认证网络重试未成功",
    network_retry_exhausted: "网络重试未成功",
    retry_exhausted: "请求重试未成功",
    collection_error: "采集未完成，请检查运行状态",
    local_collection_error: "本机采集处理失败",
    daily_interrupted: "上次运行意外中断",
};
const OBSERVATIONS = {
    available: "已有睡眠记录",
    missing: "缺少睡眠记录，等待下次窗口内补取",
    invalid: "睡眠记录无效，等待补取",
    stale: "已有睡眠记录，本轮未成功核实",
    request_failed: "请求失败，睡眠记录待核实",
    not_collected: "尚未采集",
    unknown: "睡眠记录状态未确认",
};
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const known = (table, value, fallback) => typeof value === "string" && Object.hasOwn(table, value) ? table[value] : fallback;
const count = (value, max = 1000000) => Number.isSafeInteger(value) && value >= 0 && value <= max;
const day = (value) => typeof value === "string" && /^20\d\d-/.test(value) && validDate(value);
const stamp = (value) => typeof value === "string" && value.length <= 40 &&
    /^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(value) &&
    Number.isFinite(Date.parse(value));

function facts(rows) {
    const list = el("dl", "analysis-facts daily-facts");
    for (const [label, value] of rows) add(list, el("dt", "", label), el("dd", "", value));
    return list;
}

function dailyStatusPanel(payload) {
    const data = object(payload), panel = el("section", "card analysis-card daily-card"),
        time = createTimeFormatter("Asia/Shanghai");
    panel.id = "daily-status-panel";
    panel.setAttribute("aria-label", "每日采集编排");
    panel.setAttribute("aria-live", "polite");
    add(panel, el("h2", "", "每日采集编排"));
    if (!["available", "not_run", "unavailable"].includes(data.state)) {
        panel.append(el("p", "analysis-note", "暂无每日任务信息，其他已保存数据仍可查看。"));
        return panel;
    }
    const schedule = object(data.schedule), rows = [];
    if (schedule.timezone === "Asia/Shanghai" && schedule.time === "09:00")
        rows.push(["计划时间", "每天 09:00 · 上海时间"]);
    else rows.push(["计划时间", "计划信息暂不可用"]);
    rows.push(["定时器安装", "未核验；计划时间不代表服务器已启用"]);
    if (count(data.lookback_days, 31) && data.lookback_days > 0)
        rows.push(["配置的睡眠回查范围", `含任务当天的最近 ${data.lookback_days} 天`]);
    if (stamp(schedule.next_planned_at)) rows.push(["下次计划时间", time.dateTime(schedule.next_planned_at, true)]);
    if (data.state === "unavailable") {
        rows.push(["任务状态", "日任务记录暂不可用"]);
        add(panel, facts(rows), el("p", "analysis-scope", "未读取到可靠的任务记录；不会据此启动采集，其他已保存数据仍可查看。"));
        return panel;
    }
    const latest = object(data.latest), latestStatus = known(STATES, latest.status, "任务状态未确认");
    rows.push(["最近运行", data.state === "not_run" ? STATES.not_run : latestStatus]);
    if (data.state !== "not_run") {
        if (day(latest.day)) rows.push(["任务日期", latest.day]);
        const window = object(latest.window);
        if (day(window.start_date) && day(window.end_date) && window.start_date <= window.end_date)
            rows.push(["本轮睡眠日期范围", `${window.start_date} — ${window.end_date}`]);
        if (latest.phase) rows.push(["当前步骤", known(PHASES, latest.phase, "步骤状态未确认")]);
        if (stamp(latest.started_at)) rows.push(["开始时间", time.dateTime(latest.started_at, true)]);
        if (stamp(latest.finished_at)) rows.push(["结束时间", time.dateTime(latest.finished_at, true)]);
        if (stamp(latest.retry_not_before)) rows.push(["允许重试时间", time.dateTime(latest.retry_not_before, true)]);
        const requests = object(latest.request_counts);
        for (const [key, label] of [["planned", "计划请求项"], ["completed", "已完成请求项"], ["failed", "失败请求项"], ["http", "HTTP 请求次数（含重试）"]])
            if (count(requests[key])) rows.push([label, String(requests[key])]);
        if (count(latest.attempt, 1000) && latest.attempt > 0)
            rows.push(["当日尝试次数", String(latest.attempt)]);
        if (latest.error_code) rows.push(["运行提示", known(ERRORS, latest.error_code, "本轮未完成，请检查运行状态")]);
    }
    const analysis = known({
        automatic_enabled: "自动分析已启用：检查通过后按预算生成",
        automatic_disabled: "自动分析未启用：采集与完整性检查继续运行",
    }, data.analysis, "自动分析配置暂不可确认");
    rows.push(["睡眠分析", analysis]);
    if (latest.analysis === "skipped_deferred") rows.push(["最近采集的历史记录", "当时报告生成尚未启用，不代表当前配置"]);
    panel.append(facts(rows));
    if (data.state !== "not_run")
        panel.append(el("p", "analysis-scope", "请求数为最近已保存的汇总，不代表实时进度。"));
    const observations = Array.isArray(data.observations) ? data.observations : [];
    const list = el("ul", "daily-observations");
    const seen = new Set();
    for (const candidate of observations.slice(0, 31)) {
        const item = object(candidate);
        if (!day(item.date) || seen.has(item.date)) continue;
        seen.add(item.date);
        list.append(add(el("li"), el("span", "daily-date", item.date), el("span", "", known(OBSERVATIONS, item.sleep, OBSERVATIONS.unknown))));
    }
    if (list.childElementCount)
        add(panel, el("h3", "", "当前回查日期的睡眠记录"), list);
    if (data.observations_error_code)
        panel.append(el("p", "analysis-note", "睡眠记录状态暂不可用，不能据此判断数据齐全或缺失。"));
    add(panel,
        el("p", "analysis-scope", "请求完成不代表手表已同步。窗口内缺少的数据在下一次日任务补取，成功空结果不立即重复请求。"),
        el("p", "analysis-scope", "每日流程先采集，再检查双源数据；按自动分析开关与调用预算处理合格夜晚。此处的采集完成不代表报告已生成，具体结果请查看夜晚详情。硬件上传独立运行。"),
    );
    return panel;
}

export { dailyStatusPanel };
