/* Page state and view coordination. Health data remains in memory only. */
import { api, errorText } from "./api.js";
import { $, el, add, button, icon, chip } from "./dom.js";
import {
    number,
    text,
    reason,
    stateName,
    seriesLabel,
    COLORS,
    STAGE_COLORS,
    validDate,
    shiftDate,
    duration,
    numericText,
    createTimeFormatter,
} from "./format.js";
import { TimelineChart } from "./timeline.js";

const content = $("#page-content");
const state = {
    view: "night",
    date: null,
    hidden: new Set(),
    timezone: "Asia/Shanghai",
    status: null,
    night: null,
    timeline: null,
    chart: null,
    historyDays: 7,
    sort: "date",
    sortDirection: -1,
    mode: "night",
    window: null,
    generation: 0,
    timelineGeneration: 0,
    abort: null,
};

let time = createTimeFormatter(state.timezone);

function readURL() {
    const params = new URLSearchParams(location.search);
    state.view = ["night", "history", "status"].includes(params.get("view"))
        ? params.get("view")
        : "night";
    state.date = validDate(params.get("date")) ? params.get("date") : null;
    state.hidden = new Set(
        (params.get("hidden") || "")
            .split(",")
            .filter((v) =>
                /^(heart_rate|hrv|spo2|body_battery):[a-z_]+$/.test(v),
            ),
    );
    state.historyDays = params.get("days") === "30" ? 30 : 7;
}
function writeURL(replace = false) {
    const params = new URLSearchParams();
    params.set("view", state.view);
    if (state.date) params.set("date", state.date);
    if (state.hidden.size)
        params.set("hidden", [...state.hidden].sort().join(","));
    if (state.view === "history") params.set("days", String(state.historyDays));
    const url = `/?${params}`;
    if (location.pathname + location.search !== url)
        history[replace ? "replaceState" : "pushState"]({}, "", url);
    updateNav();
}
function updateNav() {
    document.querySelectorAll("[data-view]").forEach((a) => {
        if (a.dataset.view === state.view)
            a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
        const params = new URLSearchParams({ view: a.dataset.view });
        if (state.date) params.set("date", state.date);
        a.href = `/?${params}`;
    });
}
function notice(message = "", error = false) {
    const node = $("#notice");
    node.hidden = !message;
    node.textContent = message;
    node.classList.toggle("error", error);
}
function loading() {
    state.chart?.destroy();
    state.chart = null;
    const grid = el("div", "skeleton-grid");
    for (let i = 0; i < 4; i++) grid.append(el("div", "skeleton"));
    content.replaceChildren(
        el("p", "loading-label", "正在读取观测记录…"),
        grid,
        el("div", "skeleton skeleton-large"),
    );
}
function empty(title, description) {
    return add(
        el("div", "empty-state"),
        icon("moon"),
        el("h3", "", title),
        el("p", "", description),
    );
}
function heading(title, subtitle, aside) {
    const node = el("div", "section-heading");
    add(
        node,
        add(
            el("div"),
            el("h2", "", title),
            subtitle ? el("p", "", subtitle) : null,
        ),
    );
    if (aside) node.append(el("span", "section-aside", aside));
    return node;
}
async function loadView(refreshStatus = false) {
    const generation = ++state.generation;
    ++state.timelineGeneration;
    state.abort?.abort();
    state.abort = new AbortController();
    notice();
    loading();
    const titles = {
        night: ["NIGHT OVERVIEW", "夜晚详情", "以醒来的日期，记录每一晚。"],
        history: [
            "SLEEP JOURNAL",
            "历史记录",
            "在日常记录中，回看睡眠的变化。",
        ],
        status: [
            "DATA HEALTH",
            "数据状态",
            "查看采集进度、可用字段与数据新鲜度。",
        ],
    };
    ["#page-eyebrow", "#page-title", "#page-subtitle"].forEach(
        (selector, i) => {
            $(selector).textContent = titles[state.view][i];
        },
    );
    updateNav();
    try {
        if (!state.status || refreshStatus)
            state.status = await api("status", state.abort.signal);
        if (generation !== state.generation) return;
        try {
            new Intl.DateTimeFormat("zh-CN", {
                timeZone: state.status.timezone,
            }).format();
            state.timezone = state.status.timezone || "Asia/Shanghai";
        } catch {
            state.timezone = "Asia/Shanghai";
        }
        time = createTimeFormatter(state.timezone);
        if (!state.date)
            state.date = validDate(state.status.latest_night)
                ? state.status.latest_night
                : time.localInput(Date.now()).slice(0, 10);
        writeURL(true);
        $("#sync-label").textContent = state.status.latest_success_at
            ? `采集于 ${time.dateTime(state.status.latest_success_at)}`
            : "尚无成功采集";
        $("#demo-label")?.remove();
        if (state.status.demo === true) {
            const demo = chip("pending", "演示数据");
            demo.id = "demo-label";
            $(".header-meta").prepend(demo);
        }
        $("#footer-timezone").textContent = state.timezone;
        $("#nav-status-dot").classList.toggle(
            "attention",
            !!state.status.stale,
        );
        if (state.status.stale && state.status.latest_success_at)
            notice(
                "最近成功采集已超过36小时，当前展示保留的记录。可在数据状态中查看采集情况。",
            );
        if (state.view === "status") {
            renderStatus(state.status);
            return;
        }
        if (state.view === "history") {
            await loadHistory(generation);
            return;
        }
        state.mode = "night";
        state.window = null;
        let night;
        try {
            night = await api(`nights/${state.date}`, state.abort.signal);
        } catch (error) {
            if (error.status !== 404) throw error;
            night = null;
        }
        if (generation !== state.generation) return;
        state.night = night;
        state.timeline = null;
        renderNight(night);
        if (night) await loadTimeline();
    } catch (error) {
        if (error.name === "AbortError" || generation !== state.generation)
            return;
        content.replaceChildren(empty("数据暂时无法读取", errorText(error)));
        notice(errorText(error), true);
    }
}
function navigate(view, date = state.date) {
    state.view = view;
    state.date = date;
    writeURL();
    loadView();
}
function dateToolbar(provisional = false) {
    const root = el("div", "toolbar"),
        picker = el("div", "date-picker"),
        input = el("input");
    input.type = "date";
    input.value = state.date;
    input.setAttribute("aria-label", "选择醒来日期");
    input.addEventListener("change", () => {
        if (validDate(input.value)) navigate("night", input.value);
    });
    const nav = el("div", "date-nav");
    for (const [name, amount, label] of [
        ["left", -1, "上一晚"],
        ["right", 1, "下一晚"],
    ]) {
        const b = button(
            "",
            () => navigate("night", shiftDate(state.date, amount)),
            "icon-button",
        );
        b.append(icon(name));
        b.setAttribute("aria-label", label);
        b.title = label;
        nav.append(b);
    }
    const weekday = new Intl.DateTimeFormat("zh-CN", {
        weekday: "long",
        timeZone: "UTC",
    }).format(new Date(`${state.date}T12:00Z`));
    add(picker, input, el("span", "weekday", weekday), nav);
    add(
        root,
        picker,
        el(
            "span",
            "date-caption",
            `醒来日期 · ${state.timezone}${provisional ? " · 今日记录待确认" : ""}`,
        ),
    );
    return root;
}
function sourceDetails(metric, extra = []) {
    const data = metric || {};
    const root = add(
        el("details", "source-details"),
        el("summary", "", "来源与口径"),
    );
    const dl = el("dl");
    const entries = [
        ["数据状态", stateName(data.state || "missing")],
        ["取值日期", data.source_date],
        ["来源", data.source_resource],
        ["字段", data.source_field],
        ["采集时间", time.dateTime(data.fetched_at, true)],
        ...extra,
    ];
    if (data.reason) entries.push(["说明", reason(data.reason)]);
    if (data.quality?.length)
        entries.push(["质量标记", data.quality.map(reason).join("；")]);
    for (const [key, value] of entries)
        add(dl, el("dt", "", key), el("dd", "", text(value)));
    root.append(dl);
    return root;
}
function metricCard(title, key, symbol, night) {
    const metric = night.cards?.[key] || {},
        root = el("article", "card metric-card");
    add(
        root,
        add(
            el("div", "metric-label"),
            el("span", "", title),
            add(el("span", `metric-icon ${symbol}`), icon(symbol)),
        ),
    );
    const value = el(
        "div",
        `metric-value${number(metric.value) ? "" : " is-missing"}`,
    );
    if (!number(metric.value))
        value.textContent = stateName(metric.state || "missing");
    else if (key === "sleep_seconds") {
        const min = Math.floor(metric.value / 60);
        add(
            value,
            el("span", "", Math.floor(min / 60)),
            el("span", "unit", "小时"),
            el("span", "", min % 60),
            el("span", "unit", "分"),
        );
    } else
        add(
            value,
            el(
                "span",
                "",
                numericText(metric.value, key === "training_load" ? 1 : 0),
            ),
            el(
                "span",
                "unit",
                key === "average_heart_rate"
                    ? "次/分"
                    : key === "night_average_hrv"
                      ? "ms"
                      : "",
            ),
        );
    const sub = el("div", "metric-subtitle"),
        extra = [];
    if (key === "training_load") {
        const comparison = night.load_comparison || {};
        if (number(comparison.change_percent)) {
            sub.append(
                el(
                    "span",
                    "delta",
                    `${comparison.change_percent > 0 ? "+" : ""}${numericText(comparison.change_percent, 1)}%`,
                ),
            );
            sub.append(el("span", "", "较此前30日日均"));
        } else
            sub.textContent = comparison.reason
                ? reason(comparison.reason)
                : "30日对比暂不可算";
        extra.push(
            ["负荷取值日", comparison.target_date],
            [
                "基线范围",
                comparison.baseline_start && comparison.baseline_end
                    ? `${comparison.baseline_start} 至 ${comparison.baseline_end}`
                    : null,
            ],
            [
                "有效天数",
                `${comparison.baseline_valid_days ?? 0} / ${comparison.baseline_required_days ?? 30}`,
            ],
            ["基线日均", numericText(comparison.baseline_mean, 1)],
            ["负荷口径", "全部已记录运动的单次负荷合计"],
        );
        if (
            ["stale", "pending", "invalid"].includes(comparison.state) &&
            comparison.state !== metric.state
        )
            sub.append(chip(comparison.state));
        if (comparison.reason)
            extra.push(["比较状态", reason(comparison.reason)]);
        if (number(comparison.baseline_fresh_days))
            extra.push([
                "近期有效",
                `${comparison.baseline_fresh_days} / ${comparison.baseline_required_days ?? 30}日`,
            ]);
    } else if (key === "night_average_hrv") {
        sub.textContent = number(night.cards?.weekly_average_hrv?.value)
            ? `近7日均值 ${numericText(night.cards.weekly_average_hrv.value)} ms`
            : "夜间平均 · 非逐心搏数据";
    } else
        sub.textContent =
            key === "sleep_seconds"
                ? "主睡眠 · 不含午睡"
                : "睡眠窗口内的设备平均值";
    if (["stale", "pending", "invalid"].includes(metric.state))
        sub.append(chip(metric.state));
    add(root, value, sub, sourceDetails(metric, extra));
    return root;
}
function renderNight(night) {
    content.replaceChildren(dateToolbar(night?.is_provisional));
    if (!night) {
        content.append(
            add(
                el("section", "card"),
                empty(
                    "这一天还没有睡眠记录",
                    "可选择其他日期。没有记录的日期不会被自动跳过，也不会按零值显示。",
                ),
            ),
        );
        return;
    }
    const cards = el("section", "cards");
    cards.setAttribute("aria-label", "夜间核心指标");
    [
        ["主睡眠时长", "sleep_seconds", "moon"],
        ["睡眠平均心率", "average_heart_rate", "heart"],
        ["夜间 HRV", "night_average_hrv", "hrv"],
        ["昨日运动负荷", "training_load", "load"],
    ].forEach(([label, key, symbol]) =>
        cards.append(metricCard(label, key, symbol, night)),
    );
    const secondary = el("section", "secondary-metrics");
    for (const [label, key, unit] of [
        ["昨日平均压力", "previous_stress", ""],
        ["昨日步数", "previous_steps", "步"],
        ["睡眠平均呼吸", "average_respiration", "次/分"],
        ["睡眠评分", "sleep_score", "分"],
    ]) {
        const metric = night.cards?.[key] || {};
        const value = el(
            "strong",
            "",
            number(metric.value)
                ? numericText(
                      metric.value,
                      key === "average_respiration" ? 1 : 0,
                  )
                : "未记录",
        );
        if (number(metric.value) && unit)
            value.append(el("span", "unit", unit));
        const item = add(
            el("div", "secondary-metric"),
            el("span", "", label),
            value,
        );
        item.title = `取值日 ${text(metric.source_date)} · ${stateName(metric.state || "missing")}`;
        secondary.append(item);
    }
    add(
        content,
        cards,
        secondary,
        sleepPanel(night),
        timelinePanel(),
        eventsPanel(night.activities || []),
    );
}
function sleepPanel(night) {
    const root = el("section", "card sleep-card"),
        sleep = night.sleep || {};
    root.append(
        heading(
            "这一晚的睡眠",
            "睡眠窗口与阶段累计时长",
            sleep.stage_timeline_available ? "设备分期" : "无分期时间线",
        ),
    );
    const layout = el("div", "sleep-layout"),
        window = el("div", "sleep-window"),
        times = el("div", "window-times");
    add(
        times,
        add(
            el("div", "window-time"),
            el("strong", "", time.clockTime(sleep.start_at)),
            el("small", "", "入睡时间"),
        ),
        add(
            el("div", "window-time"),
            el("strong", "", time.clockTime(sleep.end_at)),
            el("small", "", "醒来时间"),
        ),
    );
    add(
        window,
        times,
        el("div", "window-track"),
        el(
            "p",
            "window-caption",
            sleep.start_at && sleep.end_at
                ? `${time.dateTime(sleep.start_at)} — ${time.dateTime(sleep.end_at)}`
                : "睡眠起止时间未记录",
        ),
    );
    const stageArea = el("div"),
        bar = el("div", "stage-bar"),
        legend = el("div", "stage-legend");
    bar.setAttribute("aria-label", "阶段累计时长比例，并非睡眠发生顺序");
    const stages = night.stages || [],
        total = stages.reduce(
            (sum, s) => sum + (number(s.seconds) ? s.seconds : 0),
            0,
        );
    for (const stage of stages) {
        const color = STAGE_COLORS[stage.stage] || "#dbe2e6";
        if (number(stage.seconds) && stage.seconds > 0 && total > 0) {
            const segment = el("span");
            segment.style.background = color;
            segment.style.flex = String(stage.seconds);
            segment.title = `${stage.label} ${duration(stage.seconds)}`;
            bar.append(segment);
        }
        const item = el("div", "stage-item"),
            dot = el("span");
        dot.style.background = color;
        add(
            item,
            dot,
            document.createTextNode(text(stage.label)),
            el("strong", "", duration(stage.seconds)),
        );
        legend.append(item);
    }
    add(
        stageArea,
        total ? bar : el("p", "stage-note", "阶段累计时长未记录"),
        legend,
        el(
            "p",
            "stage-note",
            "各色仅表示累计时长，不代表整夜发生顺序。阶段汇总可能按分钟取整。",
        ),
    );
    add(layout, window, stageArea);
    root.append(layout);
    return root;
}
function timelinePanel() {
    const root = el("section", "card timeline-card");
    root.id = "timeline-panel";
    root.append(
        heading(
            "同一晚，同一条时间轴",
            "按真实观测时刻对齐，不补值、不跨缺口连线。",
            "UTC 对齐 · 本地时间显示",
        ),
    );
    const controls = el("div", "timeline-controls"),
        segmented = el("div", "segmented");
    segmented.setAttribute("aria-label", "时间范围");
    for (const [value, label] of [
        ["night", "整夜"],
        ["previous", "前一日活动"],
        ["custom", "自定义"],
    ]) {
        const b = button(label, () => chooseWindow(value));
        b.dataset.range = value;
        b.setAttribute("aria-pressed", String(state.mode === value));
        segmented.append(b);
    }
    const actions = el("div", "timeline-actions"),
        select = button("框选", () => setInteraction("select")),
        pan = button("平移", () => setInteraction("pan")),
        reset = button("恢复整夜", () => chooseWindow("night"));
    select.dataset.interaction = "select";
    pan.dataset.interaction = "pan";
    select.setAttribute("aria-pressed", "true");
    pan.setAttribute("aria-pressed", "false");
    add(actions, select, pan, reset);
    add(controls, segmented, actions);
    root.append(controls);
    const form = el("form", "custom-range");
    form.id = "custom-range";
    form.hidden = true;
    for (const [name, label] of [
        ["from", "从"],
        ["to", "到"],
    ]) {
        const input = el("input");
        input.type = "datetime-local";
        input.name = name;
        input.required = true;
        input.setAttribute(
            "aria-label",
            `自定义${label}时间（${state.timezone}）`,
        );
        add(form, add(el("label"), document.createTextNode(label), input));
    }
    const apply = el("button", "", "应用范围");
    apply.type = "submit";
    add(form, apply, el("span", "", `${state.timezone} · 最多48小时`));
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const start = time.localToUTC(form.elements.from.value),
            end = time.localToUTC(form.elements.to.value);
        if (
            !Number.isFinite(start) ||
            !Number.isFinite(end) ||
            end <= start ||
            end - start > 48 * 3600000
        ) {
            notice("请选择有效的起止时间，范围须大于0且不超过48小时。", true);
            return;
        }
        notice();
        state.mode = "custom";
        state.window = [start, end];
        loadTimeline();
    });
    const legend = el("div", "legend");
    legend.id = "timeline-legend";
    const shell = el("div", "chart-shell");
    shell.id = "chart-shell";
    shell.append(el("div", "loading-inline", "正在读取时间序列…"));
    const bottom = el("div", "chart-bottom");
    add(
        bottom,
        el("span", "", "拖动框选 · 平移模式拖动 · Ctrl/⌘ + 滚轮缩放"),
        el("span", "", "键盘 +/− 缩放 · ←/→ 平移 · Home 恢复"),
    );
    add(root, form, legend, shell, bottom);
    return root;
}
function setInteraction(mode) {
    if (state.chart) {
        state.chart.interaction = mode;
        state.chart.canvas.classList.toggle("is-panning", mode === "pan");
    }
    document
        .querySelectorAll("[data-interaction]")
        .forEach((b) =>
            b.setAttribute(
                "aria-pressed",
                String(b.dataset.interaction === mode),
            ),
        );
}
function chooseWindow(mode) {
    state.mode = mode;
    document
        .querySelectorAll("[data-range]")
        .forEach((b) =>
            b.setAttribute("aria-pressed", String(b.dataset.range === mode)),
        );
    const form = $("#custom-range");
    if (!form) return;
    form.hidden = mode !== "custom";
    if (mode === "custom") {
        const w = state.chart?.range || state.window;
        if (w) {
            form.elements.from.value = time.localInput(w[0]);
            form.elements.to.value = time.localInput(w[1]);
        }
        form.elements.from.focus();
        return;
    }
    if (mode === "night") state.window = null;
    else
        state.window = [
            time.localToUTC(`${shiftDate(state.date, -1)}T00:00`),
            time.localToUTC(`${state.date}T00:00`),
        ];
    loadTimeline();
}
async function loadTimeline() {
    const generation = ++state.timelineGeneration,
        pageGeneration = state.generation;
    let range = state.window;
    if (!range && state.night?.sleep?.start_at && state.night?.sleep?.end_at)
        range = [
            Date.parse(state.night.sleep.start_at) - 3600000,
            Date.parse(state.night.sleep.end_at) + 3600000,
        ];
    const params = new URLSearchParams({ max_points: "2000" });
    if (
        range &&
        range.every(Number.isFinite) &&
        range[1] > range[0] &&
        range[1] - range[0] <= 48 * 3600000
    ) {
        params.set("from", new Date(range[0]).toISOString());
        params.set("to", new Date(range[1]).toISOString());
    }
    const shell = $("#chart-shell");
    if (!shell) return;
    shell.setAttribute("aria-busy", "true");
    try {
        const timeline = await api(
            `nights/${state.date}/timeline?${params}`,
            state.abort.signal,
        );
        if (
            generation !== state.timelineGeneration ||
            pageGeneration !== state.generation
        )
            return;
        const focused = document.activeElement === state.chart?.canvas;
        const interaction = state.chart?.interaction || "select";
        state.timeline = timeline;
        state.chart?.destroy();
        shell.replaceChildren();
        shell.setAttribute("aria-busy", "false");
        const legend = $("#timeline-legend");
        legend.replaceChildren();
        for (const series of timeline.series || []) {
            const label = el("label"),
                input = el("input"),
                dot = el("span", "legend-dot");
            input.type = "checkbox";
            input.checked = !state.hidden.has(series.id);
            dot.style.background = COLORS[series.metric] || "#8b9ca5";
            input.addEventListener("change", () => {
                if (input.checked) state.hidden.delete(series.id);
                else state.hidden.add(series.id);
                writeURL();
                state.chart?.draw();
            });
            add(
                label,
                input,
                dot,
                document.createTextNode(seriesLabel(series)),
            );
            legend.append(label);
        }
        state.chart = new TimelineChart(
            shell,
            timeline,
            state.night?.sleep || {},
            {
                hidden: state.hidden,
                time,
                onRangeChange: (range) => {
                    state.mode = "custom";
                    state.window = range;
                    document
                        .querySelectorAll("[data-range]")
                        .forEach((button) =>
                            button.setAttribute(
                                "aria-pressed",
                                String(button.dataset.range === "custom"),
                            ),
                        );
                    loadTimeline();
                },
                onReset: () => chooseWindow("night"),
                onEventSelect: (event) => {
                    for (const detail of document.querySelectorAll(
                        "[data-event-key]",
                    )) {
                        if (detail.dataset.eventKey !== event.key) continue;
                        detail.open = true;
                        detail.scrollIntoView({
                            behavior: "smooth",
                            block: "nearest",
                        });
                    }
                },
            },
        );
        setInteraction(interaction);
        if (focused) state.chart.canvas.focus({ preventScroll: true });
        $("#series-resolution")?.remove();
        const resolution = add(
            el("details", "source-details"),
            el("summary", "", "实际观测与显示粒度"),
        );
        resolution.id = "series-resolution";
        const resolutionList = el("dl");
        for (const series of timeline.series || []) {
            const display = series.display_resolution || {};
            const sampling =
                series.series_type === "hourly_average"
                    ? "小时聚合"
                    : "原生观测点";
            const reduction =
                display.method === "minmax" ||
                display.method === "minmax_bucket"
                    ? ` · 按${numericText((display.bucket_seconds || 0) / 60, 1)}分钟显示桶保留极值`
                    : "";
            add(
                resolutionList,
                el("dt", "", seriesLabel(series)),
                el(
                    "dd",
                    "",
                    `${sampling} · ${numericText(display.valid_count)}个有效点 / ${numericText(display.original_count)}个源点 · 展示${numericText(display.returned_count)}点${reduction} · ${stateName(series.state)}`,
                ),
            );
        }
        resolution.append(resolutionList);
        $("#timeline-panel").append(resolution);
        const events = [
            ...(state.night?.activities || []),
            ...(timeline.events || []),
        ];
        const unique = [
            ...new Map(events.map((event) => [event.key, event])).values(),
        ];
        $("#events-panel")?.replaceWith(eventsPanel(unique));
    } catch (error) {
        if (
            error.name === "AbortError" ||
            generation !== state.timelineGeneration ||
            pageGeneration !== state.generation
        )
            return;
        state.chart?.destroy();
        state.chart = null;
        shell.replaceChildren(empty("时间序列暂不可用", errorText(error)));
        shell.setAttribute("aria-busy", "false");
    }
}
function eventsPanel(events) {
    const root = el("section", "card events-card");
    root.id = "events-panel";
    root.append(
        heading(
            "运动与午睡",
            "关联记录单独保留，点击查看活动摘要。",
            `${events.length} 条记录`,
        ),
    );
    const list = el("div", "events-list");
    if (!events.length)
        list.append(
            empty("当前范围没有事件记录", "未记录不等于没有活动或午睡。"),
        );
    for (const event of events
        .slice()
        .sort((a, b) => Date.parse(a.start_at) - Date.parse(b.start_at))) {
        const detail = el("details", "event");
        detail.dataset.eventKey = event.key;
        const summary = el("summary"),
            badge = add(
                el("span", `event-icon ${event.type === "nap" ? "nap" : ""}`),
                icon(event.type === "nap" ? "moon" : "load"),
            );
        const label = add(
            el("span", "event-main"),
            el(
                "span",
                "",
                event.label || (event.type === "nap" ? "午睡" : "运动"),
            ),
            el(
                "small",
                "",
                `${time.dateTime(event.start_at)} — ${time.clockTime(event.end_at)}`,
            ),
        );
        const metrics = event.metrics || {};
        add(
            summary,
            badge,
            label,
            el("span", "event-meta", duration(metrics.duration_seconds)),
        );
        const body = el("div", "event-detail");
        if (event.type === "activity")
            add(
                body,
                el(
                    "span",
                    "",
                    `训练负荷 ${numericText(metrics.training_load, 1)}`,
                ),
                el(
                    "span",
                    "",
                    `平均心率 ${numericText(metrics.average_heart_rate)} 次/分`,
                ),
            );
        body.append(el("span", "", `源日期 ${text(event.source_date)}`));
        if (event.quality?.length)
            body.append(el("span", "", event.quality.map(reason).join("；")));
        if (event.start_at && Number.isFinite(Date.parse(event.start_at)))
            body.append(button("定位到时间轴", () => focusEvent(event)));
        add(detail, summary, body);
        list.append(detail);
    }
    root.append(list);
    return root;
}
function focusEvent(event) {
    const start = Date.parse(event.start_at),
        end = Date.parse(event.end_at);
    state.mode = "custom";
    state.window = [
        start - 1800000,
        (Number.isFinite(end) && end > start ? end : start) + 1800000,
    ];
    if (state.window[1] - state.window[0] > 48 * 3600000)
        state.window[1] = state.window[0] + 48 * 3600000;
    loadTimeline();
    $("#timeline-panel")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
    });
}

async function loadHistory(generation) {
    const start = shiftDate(state.date, -(state.historyDays - 1));
    const params = new URLSearchParams({ start, end: state.date, limit: "31" });
    const result = await api(`nights?${params}`, state.abort.signal);
    if (generation !== state.generation) return;
    renderHistory(result.items || [], start);
}
function renderHistory(items, start) {
    const controls = el("div", "history-controls"),
        segmented = el("div", "segmented");
    for (const days of [7, 30]) {
        const b = button(`近${days}日`, () => {
            state.historyDays = days;
            writeURL();
            loadView();
        });
        b.setAttribute("aria-pressed", String(state.historyDays === days));
        segmented.append(b);
    }
    add(
        controls,
        segmented,
        el("span", "history-period", `${start} — ${state.date} · 按醒来日期`),
    );
    const card = el("section", "card"),
        head = add(
            el("div", "table-heading"),
            el("h2", "", "每日观测"),
            el("small", "", `${items.length}个日历日 · 点击日期查看详情`),
        ),
        scroll = el("div", "table-scroll"),
        table = el("table", "history-table");
    const columns = [
        ["date", "醒来日期"],
        ["sleep_seconds", "主睡眠"],
        ["average_heart_rate", "平均心率"],
        ["night_average_hrv", "HRV"],
        ["training_load", "昨日负荷"],
        ["change_percent", "较30日日均"],
        ["quality", "状态"],
    ];
    const thead = el("thead"),
        tr = el("tr");
    for (const [key, label] of columns) {
        const th = el("th");
        th.scope = "col";
        if (key !== "quality") {
            th.setAttribute(
                "aria-sort",
                state.sort === key
                    ? state.sortDirection === 1
                        ? "ascending"
                        : "descending"
                    : "none",
            );
            const sortButton = button(label, () => {
                if (state.sort === key) state.sortDirection *= -1;
                else {
                    state.sort = key;
                    state.sortDirection = -1;
                }
                renderHistory(items, start);
            });
            sortButton.setAttribute("aria-label", label);
            th.append(sortButton);
        } else th.textContent = label;
        tr.append(th);
    }
    thead.append(tr);
    const tbody = el("tbody");
    const sorted = [...items].sort((a, b) => {
        const va = state.sort === "date" ? a.date : a[state.sort]?.value,
            vb = state.sort === "date" ? b.date : b[state.sort]?.value;
        if (va == null) return vb == null ? 0 : 1;
        if (vb == null) return -1;
        return (va < vb ? -1 : va > vb ? 1 : 0) * state.sortDirection;
    });
    for (const item of sorted) {
        const row = el("tr"),
            dateCell = el("td"),
            link = el("a", "history-date", item.date);
        link.href = `/?view=night&date=${encodeURIComponent(item.date)}`;
        link.addEventListener("click", (event) => {
            if (event.metaKey || event.ctrlKey) return;
            event.preventDefault();
            navigate("night", item.date);
        });
        dateCell.append(link);
        row.append(dateCell);
        for (const [key] of columns.slice(1, -1)) {
            const metric = item[key] || {};
            let value = "—";
            if (number(metric.value))
                value =
                    key === "sleep_seconds"
                        ? duration(metric.value)
                        : key === "change_percent"
                          ? `${metric.value > 0 ? "+" : ""}${numericText(metric.value, 1)}%`
                          : `${numericText(metric.value, key === "training_load" ? 1 : 0)}${key === "night_average_hrv" ? " ms" : key === "average_heart_rate" ? " 次/分" : ""}`;
            const cell = el("td", "", value);
            cell.title = `${stateName(metric.state || "missing")}${metric.reason ? ` · ${reason(metric.reason)}` : ""}`;
            row.append(cell);
        }
        const metricStates = columns
            .slice(1, -1)
            .map(([key]) => item[key]?.state || "missing");
        const rowState =
            ["invalid", "stale", "pending"].find((value) =>
                metricStates.includes(value),
            ) ||
            item.sleep_seconds?.state ||
            "missing";
        const partiallyMissing =
            rowState === "available" &&
            metricStates.some((value) =>
                ["missing", "unavailable"].includes(value),
            );
        row.append(
            add(
                el("td"),
                chip(
                    partiallyMissing ? "missing" : rowState,
                    partiallyMissing ? "部分缺失" : undefined,
                ),
            ),
        );
        tbody.append(row);
    }
    add(table, thead, tbody);
    scroll.append(table);
    add(
        card,
        head,
        items.length
            ? scroll
            : empty("暂时没有历史记录", "成功采集后，记录会按日期显示在这里。"),
    );
    content.replaceChildren(controls, card);
}
function statusItem(label, value, detail, stateCode) {
    const node = el("div", "status-item"),
        left = add(el("div"), el("span", "", label));
    if (detail) left.append(el("small", "", detail));
    return add(
        node,
        left,
        stateCode ? chip(stateCode, value) : el("span", "status-value", value),
    );
}
function renderStatus(data) {
    const collection = data.latest_collection || {},
        database = data.database || {},
        cards = el("section", "status-grid");
    const size = number(database.size_bytes)
        ? `${(database.size_bytes / 1048576).toFixed(1)} MB`
        : "—";
    for (const [label, value, detail] of [
        [
            "最近采集",
            stateName(collection.status || "missing"),
            collection.finished_at
                ? time.dateTime(collection.finished_at)
                : "尚未执行采集",
        ],
        [
            "数据新鲜度",
            data.stale
                ? "需要更新"
                : data.latest_success_at
                  ? "已更新"
                  : "暂无记录",
            data.latest_success_at
                ? `最近成功 ${time.dateTime(data.latest_success_at)}`
                : "等待首次成功采集",
        ],
        [
            "数据库大小",
            size,
            `${numericText(database.record_count)} 条标准记录`,
        ],
    ])
        cards.append(
            add(
                el("article", "card status-metric"),
                el("h3", "", label),
                el("strong", "", value),
                el("p", "", detail),
            ),
        );
    const resources = add(
        el("section", "card status-section"),
        heading("采集资源", "失败请求保留旧数据，记录状态与取值状态分别展示。"),
    );
    const list = el("div", "status-list");
    for (const resource of data.resources || []) {
        const detail = [
            `最近尝试 ${time.dateTime(resource.last_attempt_at)}`,
            `最近成功 ${time.dateTime(resource.last_success_at)}`,
        ];
        if (resource.error_code) detail.push(reason(resource.error_code));
        if (resource.issues?.length)
            detail.push(resource.issues.map(reason).join("；"));
        list.append(
            statusItem(
                resource.label || resource.resource,
                stateName(resource.last_status),
                detail.join(" · "),
                resource.last_status === "ok"
                    ? "available"
                    : resource.last_status || "missing",
            ),
        );
    }
    if (!list.childElementCount)
        list.append(empty("尚无采集请求", "首次采集后可在此查看各资源状态。"));
    resources.append(list);
    const columns = el("div", "status-columns"),
        capabilities = add(
            el("section", "card status-section"),
            heading("当前数据能力"),
        ),
        capList = el("div", "status-list");
    for (const cap of data.capabilities || [])
        capList.append(
            statusItem(
                cap.label || cap.key,
                cap.available ? "可用" : "暂无",
                cap.note ? reason(cap.note) : null,
                cap.available ? "available" : "unavailable",
            ),
        );
    capabilities.append(capList);
    const storage = add(
            el("section", "card status-section"),
            heading("存储与运行"),
        ),
        storageList = el("div", "status-list");
    add(
        storageList,
        statusItem("数据库", database.present ? "已建立" : "尚未建立"),
        statusItem("原始观测快照", numericText(database.raw_snapshot_count)),
        statusItem("请求审计", numericText(database.request_count)),
        statusItem(
            "当前数据日期",
            data.earliest_night && data.latest_night
                ? `${data.earliest_night} — ${data.latest_night}`
                : "暂无",
        ),
        statusItem(
            "备份",
            stateName(data.backup?.state || "not_configured"),
            data.backup?.last_success_at
                ? `最近成功 ${time.dateTime(data.backup.last_success_at)}`
                : "备份与恢复需单独配置",
        ),
    );
    if (collection.retry_not_before)
        storageList.append(
            statusItem(
                "重试等待至",
                time.dateTime(collection.retry_not_before, true),
            ),
        );
    if (collection.stop_reason)
        storageList.append(
            statusItem("停止原因", reason(collection.stop_reason)),
        );
    storage.append(storageList);
    add(columns, capabilities, storage);
    content.replaceChildren(cards, resources, columns);
}
document.querySelectorAll("[data-view]").forEach((a) =>
    a.addEventListener("click", (event) => {
        if (event.metaKey || event.ctrlKey) return;
        event.preventDefault();
        navigate(a.dataset.view);
    }),
);
$("#refresh-button").addEventListener("click", () => loadView(true));
window.addEventListener("popstate", () => {
    readURL();
    loadView();
});
readURL();
loadView();
