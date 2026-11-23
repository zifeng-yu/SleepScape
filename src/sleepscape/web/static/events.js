/* Read-only event presentation. Counts describe summaries, never awakenings. */
import { el, add, button, icon } from "./dom.js";
import { text, duration, numericText, reason } from "./format.js";
import { environmentEventDetail } from "./environment.js";

const TYPES = [
    ["all", "全部"], ["activity", "运动"], ["nap", "午睡"],
    ["environment_sound", "声音摘要"],
];

function eventDetails(event, { time, timeline, onLocate }) {
    const sound = event.type === "environment_sound";
    const detail = el("details", "event");
    detail.dataset.eventKey = event.key;
    detail.dataset.eventType = event.type;
    const metrics = event.metrics || {};
    const summary = add(el("summary"),
        add(el("span", `event-icon ${sound ? "sound" : event.type === "nap" ? "nap" : ""}`),
            icon(sound ? "sound" : event.type === "nap" ? "moon" : "load")),
        add(el("span", "event-main"),
            el("span", "", sound ? "声音摘要" : event.label || (event.type === "nap" ? "午睡" : "运动")),
            el("small", "", `${time.dateTime(event.start_at)} — ${time.clockTime(event.end_at)}`)),
        el("span", "event-meta", sound
            ? `较响累计 ${numericText(metrics.active_duration_ms / 1000, 1)}秒`
            : duration(metrics.duration_seconds)));
    if (sound && event.audio_available === true && /^record-[0-9a-f]{24}$/.test(event.key)) {
        const audio = el("audio");
        audio.preload = "none";
        audio.src = `/api/v1/event-audio/${event.key}`;
        const play = button("播放", async (click) => {
            click.preventDefault();
            click.stopPropagation();
            if (!audio.paused) { audio.pause(); return; }
            for (const other of detail.closest(".events-list")?.querySelectorAll("audio") || []) {
                if (other !== audio) other.pause();
            }
            try { await audio.play(); } catch (error) {
                if (error.name !== "AbortError") play.textContent = "无法播放";
            }
        });
        audio.addEventListener("playing", () => { play.textContent = "暂停"; });
        audio.addEventListener("pause", () => { play.textContent = "播放"; });
        audio.addEventListener("ended", () => { play.textContent = "播放"; });
        summary.append(play, audio);
    }
    const body = el("div", "event-detail");
    if (event.type === "activity") add(body,
        el("span", "", `训练负荷 ${numericText(metrics.training_load, 1)}`),
        el("span", "", `平均心率 ${numericText(metrics.average_heart_rate)} 次/分`));
    if (sound) body.append(environmentEventDetail(event, time,
        timeline?.environment?.sources?.find((source) => source.source_ref === event.source_ref),
        timeline?.series?.find((series) => series.source_ref === event.source_ref &&
            series.boot_ref === event.boot_ref && series.series_type === "minute_mean" &&
            series.points?.some((point) => point.event_detection))
            ?.points?.find((point) => Date.parse(point.window_start) <= Date.parse(event.start_at) &&
                Date.parse(point.window_end) > Date.parse(event.start_at))));
    else body.append(el("span", "", `源日期 ${text(event.source_date)}`));
    if (!sound && event.quality?.length)
        body.append(el("span", "", event.quality.map(reason).join("；")));
    if (Number.isFinite(Date.parse(event.start_at)))
        body.append(button("定位到时间轴", () => onLocate(event)));
    return add(detail, summary, body);
}

function eventsPanel(events, options) {
    const { time, timeline, previousDate } = options;
    const soundAvailable = timeline?.environment?.state === "available";
    const root = el("section", "card events-card");
    root.id = "events-panel";
    const header = add(el("div", "section-heading"),
        add(el("div"), el("h2", "", "这晚的关联记录"),
            el("p", "", "按类型查看额外标记；完整的每分钟环境读数在上方曲线。")),
        el("span", "section-aside", `已读取 ${events.length} 条`));
    const scope = el("p", "event-scope", `运动包含${previousDate}全天及图表范围内的记录；午睡、声音摘要按当前图表时间范围显示。`);
    const range = timeline?.window;
    if (range?.start && range?.end)
        scope.append(document.createTextNode(` 当前范围：${time.dateTime(range.start)} — ${time.dateTime(range.end)}。`));
    const help = add(el("details", "event-help"),
        el("summary", "", "声音摘要是什么意思？"),
        el("p", "", "声音比当时背景明显升高时，设备记录声音变化摘要。持续声音可能按分钟拆开，同一分钟内多个短片段也可能合并；摘要条数不是独立声音次数，更不是觉醒次数。"),
        el("p", "", "“较响累计”是摘要内达到检测条件的时长，不代表整个起止时段一直很响。有可回听的睡眠时段片段时，右侧显示播放按钮；预热和采样缺口也会影响摘要数量。"));
    const filters = el("div", "event-filters");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "关联记录类型");
    const list = el("div", "events-list");
    const ordered = events.slice().sort((a, b) => Date.parse(a.start_at) - Date.parse(b.start_at));
    const rows = ordered.map((event) => eventDetails(event, options));
    rows.forEach((row) => list.append(row));
    const empty = el("p", "event-empty");
    empty.setAttribute("role", "status");
    const selected = el("p", "event-selection");
    selected.setAttribute("aria-live", "polite");
    function show(type) {
        const count = ordered.filter((event) => type === "all" || event.type === type).length;
        for (const row of rows) {
            row.hidden = type !== "all" && row.dataset.eventType !== type;
            if (row.hidden) row.querySelector("audio")?.pause();
        }
        for (const b of filters.children) b.setAttribute("aria-pressed", String(b.dataset.eventFilter === type));
        const label = type === "all" ? "关联记录" : TYPES.find(([key]) => key === type)?.[1] || "记录";
        selected.textContent = type === "environment_sound" && !soundAvailable
            ? "声音摘要暂不可用" : `显示 ${count} 条${label}`;
        empty.hidden = count > 0;
        empty.textContent = type === "environment_sound"
            ? soundAvailable
                ? "当前范围没有声音摘要；没有摘要不代表整晚绝对安静，仍可查看声音曲线和采样覆盖。"
                : "声音摘要尚未读取或暂不可用，请刷新重试；当前不能判断摘要条数。"
            : "当前范围未记录此类内容；未记录不代表没有发生。";
    }
    for (const [type, label] of TYPES) {
        const count = events.filter((event) => type === "all" || event.type === type).length;
        const b = button(`${label} ${type === "environment_sound" && !soundAvailable ? "—" : count}`, () => show(type));
        b.dataset.eventFilter = type;
        filters.append(b);
    }
    add(root, header, scope, filters, selected, help);
    if (!soundAvailable)
        root.append(el("p", "environment-note", "声音摘要尚未读取或暂不可用，不按0条解释。"));
    if (timeline?.environment?.events_truncated || timeline?.environment?.truncated)
        root.append(el("p", "environment-warning", "声音摘要达到查询或展示上限，当前条数不是完整总数。请缩小图表时间范围查看。"));
    add(root, list, empty);
    show("all");
    return root;
}

function revealEvent(panel, key) {
    if (!panel) return;
    panel.querySelector('[data-event-filter="all"]')?.click();
    for (const detail of panel.querySelectorAll("[data-event-key]")) {
        if (detail.dataset.eventKey !== key) continue;
        detail.open = true;
        detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
    }
}

export { eventsPanel, revealEvent };
