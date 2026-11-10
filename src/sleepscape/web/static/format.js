/* Pure presentation helpers. Time conversion always has an explicit timezone. */
const number = (value) => typeof value === "number" && Number.isFinite(value);
const text = (value, fallback = "—") =>
    value === null || value === undefined || value === ""
        ? fallback
        : String(value);
const STATE_NAMES = {
    available: "已记录",
    missing: "未记录",
    stale: "旧值保留",
    pending: "待确认",
    invalid: "需核对",
    unavailable: "暂不可用",
    failed: "请求失败",
    ok: "正常",
    empty: "无记录",
    partial: "部分完成",
    completed: "已完成",
    running: "采集中",
    interrupted: "已中断",
    unknown: "未知",
    not_configured: "未配置",
};
const REASONS = {
    insufficient_baseline: "近30日有效记录不足",
    incomplete_baseline: "近30日有效记录不足",
    baseline_incomplete: "近30日有效记录不足",
    zero_baseline: "基线为零，无法计算变化",
    baseline_zero: "基线为零，无法计算变化",
    target_missing: "目标日负荷缺失",
    target_load_missing: "目标日负荷缺失",
    missing_training_load: "部分活动未提供负荷",
    source_unavailable: "来源未提供此字段",
    latest_request_failed: "最近请求失败，保留旧记录",
    current_day_provisional: "今日记录尚待确认",
    end_at_derived: "结束时间由已记录经过时长计算",
    stored_observations: "已有本地观测记录",
    not_collected: "尚未采集",
    requires_source_intervals: "来源尚未提供分期起止区间",
    hardware_not_connected: "环境硬件尚未接入",
    stored_source_intervals: "已有来源提供的真实分期区间",
    sleep_stage_timeline_unavailable: "未提供分期时间线",
    sample_series_unavailable: "原生时间序列不可用",
    negative_or_sentinel: "无效读数已保留为缺失",
    source_not_plottable: "来源标记该序列不宜绘图",
    collection_not_recent: "最近采集需要更新",
    sleep_summary_minute_rounding_consistent:
        "阶段按分钟汇总，与秒级总量存在精度差异",
    activity_source_date_differs_from_configured_date:
        "活动源日期与当前时区日期不同",
    activity_source_date_unavailable: "活动源日期缺失，采用配置时区",
    no_sleep_record: "该日尚无睡眠记录",
    no_records: "尚无记录",
    request_failed: "最近请求失败",
    retry_after_deferred: "服务限流，等待下次尝试",
};
const reason = (code) => REASONS[code] || text(code, "来源尚未提供记录");
const seriesLabel = (series) =>
    `${series.label || METRIC_NAMES[series.metric]}${series.series_type === "hourly_average" && !series.label?.includes("小时") ? " · 小时均值" : ""}`;
const stateName = (value) => STATE_NAMES[value] || text(value, "未知");
const COLORS = {
    heart_rate: "#c47c8c",
    hrv: "#4b9a91",
    spo2: "#789fc6",
    body_battery: "#bcab78",
};
const METRIC_NAMES = {
    heart_rate: "心率",
    hrv: "HRV",
    spo2: "血氧",
    body_battery: "Body Battery",
};
const STAGE_COLORS = {
    deep: "#8674b4",
    light: "#b8a7d7",
    rem: "#d9d0ea",
    awake: "#e7cbb6",
    unmeasurable: "#dbe2e6",
};
function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
    const instant = new Date(`${value}T12:00:00Z`);
    return (
        Number.isFinite(+instant) &&
        instant.toISOString().slice(0, 10) === value
    );
}
function shiftDate(value, days) {
    const d = new Date(`${value}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
}
function duration(seconds) {
    if (!number(seconds)) return "未记录";
    const minutes = Math.floor(seconds / 60),
        hours = Math.floor(minutes / 60);
    return hours
        ? `${hours}小时${minutes % 60 ? ` ${minutes % 60}分` : ""}`
        : `${minutes}分`;
}
const numericText = (value, digits = 0) =>
    number(value)
        ? new Intl.NumberFormat("zh-CN", {
              maximumFractionDigits: digits,
          }).format(value)
        : "—";

export function createTimeFormatter(timezone) {
    function parts(instant) {
        return Object.fromEntries(
            new Intl.DateTimeFormat("en-CA", {
                timeZone: timezone,
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hourCycle: "h23",
            })
                .formatToParts(new Date(instant))
                .map((p) => [p.type, p.value]),
        );
    }
    function localInput(instant) {
        const p = parts(instant);
        return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
    }
    function localToUTC(value) {
        if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return NaN;
        const wall = Date.parse(`${value}:00Z`);
        if (!Number.isFinite(wall)) return NaN;
        let guess = wall;
        for (let i = 0; i < 3; i++) {
            const p = parts(guess);
            const observed = Date.parse(
                `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}Z`,
            );
            guess += wall - observed;
        }
        return localInput(guess) === value ? guess : NaN;
    }
    function dateTime(value, full = false) {
        if (!value || !Number.isFinite(Date.parse(value))) return "—";
        return new Intl.DateTimeFormat("zh-CN", {
            timeZone: timezone,
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            ...(full ? { year: "numeric", second: "2-digit" } : {}),
            hourCycle: "h23",
        }).format(new Date(value));
    }
    function clockTime(value) {
        if (!value || !Number.isFinite(Date.parse(value))) return "—";
        return new Intl.DateTimeFormat("zh-CN", {
            timeZone: timezone,
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        }).format(new Date(value));
    }
    return { localInput, localToUTC, dateTime, clockTime };
}

export {
    number,
    text,
    STATE_NAMES,
    reason,
    stateName,
    seriesLabel,
    COLORS,
    METRIC_NAMES,
    STAGE_COLORS,
    validDate,
    shiftDate,
    duration,
    numericText,
};
