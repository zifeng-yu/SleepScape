/* Canvas timeline with injected view callbacks; no fetch or application state. */
import { el, add } from "./dom.js";
import {
    number,
    numericText,
    seriesLabel,
} from "./format.js";
import { metricInfo, metricItems, metricColor, metricLabel } from "./metrics.js";
import { environmentPointLines } from "./environment.js";

function roundedStep(value) {
    const magnitude = 10 ** Math.floor(Math.log10(Math.max(value, 0.000001)));
    const normalized = value / magnitude;
    return ([1, 2, 2.5, 5, 10].find((step) => step >= normalized) || 10) * magnitude;
}

function axisUnit(unit) {
    return { bpm: "次/分（bpm）", ms: "毫秒（ms）", points: "分" }[unit] || unit || "";
}

function valueAxis(values, definition) {
    const precision = definition?.precision || 0;
    const minimumStep = 10 ** -precision;
    const low = values.length ? Math.min(...values) : 0;
    const high = values.length ? Math.max(...values) : 1;
    const padding = Math.max(minimumStep, (high - low) * 0.12);
    let min = number(definition?.axis.min) ? definition.axis.min : low - padding;
    let max = number(definition?.axis.max) ? definition.axis.max : high + padding;
    const step = Math.max(minimumStep, roundedStep((max - min) / 3));
    if (!number(definition?.axis.min)) min = Math.floor(min / step) * step;
    if (!number(definition?.axis.max)) max = Math.ceil(max / step) * step;
    if (max <= min) max = min + step;
    const ticks = [];
    for (let value = Math.ceil(min / step) * step; value <= max + step / 100; value += step)
        ticks.push(Math.abs(value) < step / 100 ? 0 : value);
    return { min, max, ticks, precision };
}

function timeTicks(range, time, plotWidth) {
    const count = Math.max(3, Math.floor(plotWidth / 90));
    const target = (range[1] - range[0]) / count;
    const step = [1, 5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440]
        .map((minutes) => minutes * 60000).find((value) => value >= target) || 86400000;
    const wallStart = Date.parse(`${time.localInput(range[0])}:00Z`);
    const wallEnd = Date.parse(`${time.localInput(range[1])}:00Z`);
    const result = [range[0]];
    for (let wall = Math.ceil(wallStart / step) * step; wall < wallEnd; wall += step) {
        const instant = time.localToUTC(new Date(wall).toISOString().slice(0, 16));
        const x = (instant - range[0]) / (range[1] - range[0]) * plotWidth;
        if (Number.isFinite(instant) && x >= 44 && x <= plotWidth - 54) result.push(instant);
    }
    result.push(range[1]);
    return result;
}

export class TimelineChart {
    constructor(
        shell,
        data,
        sleep,
        { hidden, time, onRangeChange, onReset, onEventSelect },
    ) {
        this.hidden = hidden;
        this.time = time;
        this.onRangeChange = onRangeChange;
        this.onReset = onReset;
        this.onEventSelect = onEventSelect;
        this.shell = shell;
        this.data = data;
        this.sleep = sleep;
        this.interaction = "select";
        this.pointer = null;
        this.drag = null;
        this.pinned = false;
        this.canvas = el("canvas", "chart-canvas");
        this.canvas.tabIndex = 0;
        this.canvas.setAttribute("role", "img");
        this.canvas.setAttribute(
            "aria-label",
            "共享时间轴观测图。加减键缩放，左右键平移，Home恢复整夜。Garmin为真实观测点，环境为真实分钟窗口；无数据处留空。",
        );
        this.tooltip = el("div", "chart-tooltip");
        this.tooltip.hidden = true;
        this.tooltip.setAttribute("role", "region");
        this.tooltip.setAttribute("aria-label", "观测详情");
        this.tooltip.tabIndex = -1;
        this.tooltip.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                this.unpin();
            }
        });
        this.description = el(
            "p",
            "sr-only",
            "身体指标与环境指标按真实UTC时间对齐，环境不同配置与启动周期分段。用指针或触摸选点，查看原始观测时刻或分钟窗口、覆盖与质量。环境缺口不使用相邻窗口代替。",
        );
        this.description.id = "chart-axis-description";
        this.canvas.setAttribute("aria-describedby", this.description.id);
        const guide = el("div", "chart-reading-guide");
        guide.title = "相对声音以dBFS表示，负值越接近0越响；未校准声压，不能按dBA解读。";
        add(guide,
            el("span", "", "每行独立纵轴，单位见标题"),
            el("span", "", "淡紫：睡眠时段 · 竖虚线：配置或重启分界"));
        add(shell, guide, this.canvas, this.tooltip, this.description);
        this.range = [
            Date.parse(data.window.start),
            Date.parse(data.window.end),
        ];
        this.series = (data.series || []).map((s) => {
            const points = (s.points || [])
                .filter((p) => Number.isFinite(Date.parse(p.timestamp)))
                .map((p) => ({ ...p, t: Date.parse(p.timestamp),
                    end: Date.parse(p.window_end) }))
                .sort((a, b) => a.t - b.t);
            return { ...s, observations: points, valid: points.filter((p) => number(p.value)) };
        });
        this.eventHits = [];
        this.rows = [];
        this.canvas.addEventListener("pointerdown", (event) =>
            this.down(event),
        );
        this.canvas.addEventListener("pointermove", (event) =>
            this.move(event),
        );
        this.canvas.addEventListener("pointerup", (event) => this.up(event));
        this.canvas.addEventListener("pointercancel", () => {
            this.drag = null;
            this.pointer = null;
            this.draw();
        });
        this.canvas.addEventListener("pointerleave", () => {
            if (!this.drag && !this.pinned) {
                this.pointer = null;
                this.draw();
            }
        });
        this.canvas.addEventListener(
            "wheel",
            (event) => {
                if (!event.ctrlKey && !event.metaKey) return;
                event.preventDefault();
                const point = this.position(event);
                this.zoom(event.deltaY > 0 ? 1.25 : 0.8, this.toTime(point.x));
            },
            { passive: false },
        );
        this.canvas.addEventListener("keydown", (event) => this.key(event));
        this.observer = new ResizeObserver(() => this.draw());
        this.observer.observe(shell);
        this.draw();
    }
    destroy() {
        this.observer?.disconnect();
    }
    position(event) {
        const box = this.canvas.getBoundingClientRect();
        return { x: event.clientX - box.left, y: event.clientY - box.top };
    }
    toTime(x) {
        return (
            this.range[0] +
            Math.max(0, Math.min(1, (x - this.left) / this.plotWidth)) *
                (this.range[1] - this.range[0])
        );
    }
    toX(t) {
        return (
            this.left +
            ((t - this.range[0]) / (this.range[1] - this.range[0])) *
                this.plotWidth
        );
    }
    down(event) {
        if (event.button !== 0) return;
        const p = this.position(event);
        if (p.x < this.left || p.x > this.width - 13) return;
        this.canvas.focus({ preventScroll: true });
        this.canvas.setPointerCapture(event.pointerId);
        this.drag = {
            start: p,
            current: p,
            original: [...this.range],
            mode: event.shiftKey ? "pan" : this.interaction,
        };
        this.pointer = p;
    }
    move(event) {
        if (this.pinned && !this.drag) return;
        const p = this.position(event);
        this.pointer = p;
        if (this.drag) {
            this.drag.current = p;
            if (this.drag.mode === "pan") {
                const delta =
                    ((this.drag.start.x - p.x) / this.plotWidth) *
                    (this.drag.original[1] - this.drag.original[0]);
                this.range = this.drag.original.map((t) => t + delta);
                this.canvas.classList.add("is-dragging");
            }
        }
        this.draw();
    }
    up(event) {
        if (!this.drag) return;
        const p = this.position(event),
            drag = this.drag;
        this.drag = null;
        this.canvas.classList.remove("is-dragging");
        if (this.canvas.hasPointerCapture(event.pointerId))
            this.canvas.releasePointerCapture(event.pointerId);
        if (Math.abs(p.x - drag.start.x) < 6) {
            const hit = this.eventHits.find(
                (h) => Math.abs(h.x - p.x) < 11 && Math.abs(h.y - p.y) < 13,
            );
            if (hit) this.onEventSelect(hit.event);
            else this.pinned = true;
            this.pointer = p;
            this.draw();
            return;
        }
        if (drag.mode === "select")
            this.commit([
                Math.min(this.toTime(drag.start.x), this.toTime(p.x)),
                Math.max(this.toTime(drag.start.x), this.toTime(p.x)),
            ]);
        else this.commit(this.range);
    }
    commit(range) {
        this.pinned = false;
        if (range[1] - range[0] < 300000) {
            const center = (range[0] + range[1]) / 2;
            range = [center - 150000, center + 150000];
        }
        if (range[1] - range[0] > 48 * 3600000) {
            const center = (range[0] + range[1]) / 2;
            range = [center - 24 * 3600000, center + 24 * 3600000];
        }
        this.range = range;
        this.pointer = null;
        this.draw();
        this.onRangeChange(range);
    }
    zoom(factor, center = (this.range[0] + this.range[1]) / 2) {
        this.commit([
            center - (center - this.range[0]) * factor,
            center + (this.range[1] - center) * factor,
        ]);
    }
    key(event) {
        if (
            [
                "+",
                "=",
                "-",
                "_",
                "ArrowLeft",
                "ArrowRight",
                "Home",
                "Escape",
            ].includes(event.key)
        )
            event.preventDefault();
        else return;
        if (event.key === "+" || event.key === "=") this.zoom(0.75);
        else if (event.key === "-" || event.key === "_") this.zoom(1.35);
        else if (event.key === "Home") this.onReset();
        else if (event.key === "Escape") {
            this.pinned = false;
            this.pointer = null;
            this.drag = null;
            this.draw();
        } else {
            const shift =
                (this.range[1] - this.range[0]) *
                (event.key === "ArrowLeft" ? -0.2 : 0.2);
            this.commit(this.range.map((t) => t + shift));
        }
    }
    unpin() {
        this.pinned = false;
        this.pointer = null;
        this.draw();
        this.canvas.focus({ preventScroll: true });
    }
    draw() {
        this.tooltip.classList.toggle("is-pinned", this.pinned);
        this.rows = metricItems().filter((metric) => this.series.some((series) =>
            series.metric === metric.id && !this.hidden.has(series.id),
        )).map((metric) => metric.id);
        this.width = Math.max(250, this.shell.clientWidth);
        this.left = this.width < 500 ? 55 : 76;
        this.plotWidth = this.width - this.left - 14;
        const rowHeight = this.width < 500 ? 132 : 126,
            top = 76,
            bottom = 49;
        this.height = top + Math.max(1, this.rows.length) * rowHeight + bottom;
        const ratio = Math.min(window.devicePixelRatio || 1, 3);
        this.canvas.width = Math.round(this.width * ratio);
        this.canvas.height = Math.round(this.height * ratio);
        this.canvas.style.height = `${this.height}px`;
        const ctx = this.canvas.getContext("2d");
        ctx.scale(ratio, ratio);
        ctx.clearRect(0, 0, this.width, this.height);
        const startSleep = Date.parse(this.sleep.start_at),
            endSleep = Date.parse(this.sleep.end_at),
            ticks = timeTicks(this.range, this.time, this.plotWidth),
            descriptions = [];
        this.axes = [];
        this.rows.forEach((metric, index) => {
            const definition = metricInfo(metric),
                y = top + index * rowHeight,
                plotTop = y + 36,
                plotBottom = y + rowHeight - 16,
                group = this.series.filter((series) => series.metric === metric && !this.hidden.has(series.id)),
                points = group.flatMap((series) => series.valid.filter((point) => series.source === "environment"
                    ? point.t < this.range[1] && point.end > this.range[0]
                    : point.t >= this.range[0] && point.t <= this.range[1])),
                values = points.flatMap((point) => [point.value, point.min, point.max].filter(number)),
                axis = valueAxis(values, definition),
                toY = (value) => plotTop + ((axis.max - value) / (axis.max - axis.min)) * (plotBottom - plotTop);
            this.axes.push({ metric, ...axis, plotTop, plotBottom });
            ctx.fillStyle = index % 2 ? "#f9fbfc" : "#ffffff";
            ctx.fillRect(0, y, this.width, rowHeight);
            if (Number.isFinite(startSleep) && Number.isFinite(endSleep)) {
                const x1 = Math.max(this.left, this.toX(startSleep)),
                    x2 = Math.min(this.width - 14, this.toX(endSleep));
                if (x2 > x1) {
                    ctx.fillStyle = "#f0edf8";
                    ctx.fillRect(x1, plotTop, x2 - x1, plotBottom - plotTop);
                }
            }
            // A full-width header keeps long metric names and their units out of the value axis.
            const title = `${metricLabel(metric)} · ${axisUnit(definition?.unit)}`;
            ctx.fillStyle = metricColor(metric);
            ctx.fillRect(1, y + 10, 3, 15);
            ctx.textAlign = "left";
            ctx.font = '600 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.fillStyle = "#304858";
            ctx.fillText(title, 10, y + 22, this.width - 70);
            ctx.textAlign = "right";
            ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.fillStyle = "#647684";
            ctx.fillText(definition?.source === "environment" ? "环境" : "手表", this.width - 14, y + 22);
            for (const instant of ticks) {
                const x = this.toX(instant);
                ctx.strokeStyle = "#e2e8ed";
                ctx.setLineDash([2, 4]);
                ctx.beginPath();
                ctx.moveTo(x, plotTop);
                ctx.lineTo(x, plotBottom);
                ctx.stroke();
            }
            ctx.setLineDash([]);
            if (points.length) {
                for (const value of axis.ticks) {
                    const tickY = toY(value);
                    ctx.strokeStyle = "#dfe6eb";
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(this.left, tickY);
                    ctx.lineTo(this.width - 14, tickY);
                    ctx.stroke();
                    ctx.textAlign = "right";
                    ctx.fillStyle = "#526b7b";
                    ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
                    ctx.fillText(numericText(value, axis.precision), this.left - 8, tickY + 4);
                }
                descriptions.push(`${title}，纵轴 ${numericText(axis.min, axis.precision)} 至 ${numericText(axis.max, axis.precision)}`);
            } else {
                ctx.textAlign = "left";
                ctx.fillStyle = "#6a7f8b";
                ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
                ctx.fillText("此时间范围暂无有效观测", this.left + 9, (plotTop + plotBottom) / 2 + 4);
                descriptions.push(`${title}，此时间范围暂无有效观测`);
            }
            ctx.save();
            ctx.beginPath();
            ctx.rect(this.left, plotTop - 1, this.plotWidth, plotBottom - plotTop + 2);
            ctx.clip();
            for (const series of group) {
                for (const point of series.valid) {
                    if (series.source === "environment") {
                        if (!Number.isFinite(point.end) || point.t >= this.range[1] || point.end <= this.range[0]) continue;
                        const x1 = Math.max(this.left, this.toX(point.t)),
                            x2 = Math.min(this.width - 14, this.toX(point.end)),
                            middle = (x1 + x2) / 2;
                        ctx.strokeStyle = metricColor(metric);
                        ctx.globalAlpha = point.channel_state === "partial" ? 0.5 : 0.95;
                        if (number(point.min) && number(point.max) && point.min !== point.max) {
                            ctx.lineWidth = 1;
                            ctx.beginPath();
                            ctx.moveTo(middle, toY(point.min));
                            ctx.lineTo(middle, toY(point.max));
                            ctx.stroke();
                        }
                        // Each stroke remains a real minute window, never a bridge across a gap or boot.
                        ctx.lineWidth = series.series_type === "minute_peak" ? 1.2 : 2;
                        ctx.setLineDash(series.series_type === "minute_peak" ? [2, 2] : []);
                        ctx.beginPath();
                        ctx.moveTo(x1, toY(point.value));
                        ctx.lineTo(Math.max(x1 + 0.5, x2), toY(point.value));
                        ctx.stroke();
                        ctx.setLineDash([]);
                        ctx.lineWidth = 1;
                        ctx.globalAlpha = 1;
                    } else if (point.t >= this.range[0] && point.t <= this.range[1]) {
                        ctx.fillStyle = metricColor(metric);
                        ctx.globalAlpha = 0.9;
                        ctx.beginPath();
                        ctx.arc(this.toX(point.t), toY(point.value),
                            series.series_type === "hourly_average" || series.valid.length < 40 ? 3.5 : 1.8,
                            0, Math.PI * 2);
                        ctx.fill();
                        ctx.globalAlpha = 1;
                    }
                }
            }
            ctx.restore();
            ctx.strokeStyle = "#e5ebef";
            ctx.beginPath();
            ctx.moveTo(0, y + rowHeight);
            ctx.lineTo(this.width, y + rowHeight);
            ctx.stroke();
        });
        // Repeat the shared, local-time axis above and below the tracks; date labels make midnight explicit.
        for (const [index, instant] of ticks.entries()) {
            const x = this.toX(instant),
                local = this.time.localInput(instant),
                clock = local.slice(11, 16),
                date = local.slice(5, 10).replace("-", "/");
            ctx.textAlign = index === ticks.length - 1 ? "right" : "center";
            ctx.fillStyle = "#405c6d";
            ctx.font = '600 11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.fillText(clock, x, 18);
            ctx.fillText(clock, x, this.height - 28);
            ctx.fillStyle = "#718694";
            ctx.font = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.fillText(date, x, 32);
            ctx.fillText(date, x, this.height - 13);
        }
        ctx.textAlign = "left";
        ctx.fillStyle = "#657c8b";
        ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        ctx.fillText("时间", 1, 18);
        ctx.fillText("事件", 1, 62);
        ctx.strokeStyle = "#e5ebef";
        ctx.beginPath();
        ctx.moveTo(this.left, 58);
        ctx.lineTo(this.width - 14, 58);
        ctx.stroke();
        this.description.textContent = `横轴按本地时间 ${this.time.dateTime(new Date(this.range[0]).toISOString())} 至 ${this.time.dateTime(new Date(this.range[1]).toISOString())}。各指标独立纵轴：${descriptions.join("；")}。相对声音负值越接近0越响，未校准声压。点选查看观测详情；空白处为缺测，环境不同配置和启动周期不连线。`;
        if (!this.rows.length) {
            ctx.fillStyle = "#647684";
            ctx.textAlign = "left";
            ctx.fillText(this.series.length ? "所有序列已隐藏，可从图例重新选择" : "此范围无可展示观测", this.left + 12, top + 44);
        }
        // Boundaries label configuration/boot changes without joining or merging their data.
        for (const segment of this.data.environment?.segments || []) {
            const t = Date.parse(segment.utc_start);
            if (!Number.isFinite(t) || t <= this.range[0] || t >= this.range[1]) continue;
            const environmentAxes = this.axes.filter((axis) => metricInfo(axis.metric)?.source === "environment");
            if (!environmentAxes.length) break;
            ctx.strokeStyle = "#b6b4bf";
            ctx.setLineDash([2, 5]);
            ctx.beginPath();
            for (const axis of environmentAxes) {
                ctx.moveTo(this.toX(t), axis.plotTop);
                ctx.lineTo(this.toX(t), axis.plotBottom);
            }
            ctx.stroke();
            ctx.setLineDash([]);
        }
        this.eventHits = [];
        ctx.font = "11px sans-serif";
        ctx.textAlign = "left";
        for (const event of this.data.events || []) {
            const t = Date.parse(event.start_at);
            if (event.type === "environment_sound") {
                const end = Date.parse(event.end_at);
                if (!Number.isFinite(t) || !Number.isFinite(end) || t >= this.range[1] || end <= this.range[0]) continue;
                const x1 = Math.max(this.left, this.toX(t)), x2 = Math.min(this.width - 14, this.toX(end));
                ctx.strokeStyle = metricColor("sound_relative");
                ctx.setLineDash([2, 2]);
                ctx.beginPath();
                ctx.moveTo(x1, 54); ctx.lineTo(x1, 62);
                ctx.lineTo(Math.max(x1 + 3, x2), 62); ctx.lineTo(Math.max(x1 + 3, x2), 54);
                ctx.stroke(); ctx.setLineDash([]);
                this.eventHits.push({ x: (x1 + x2) / 2, y: 58, event });
                continue;
            }
            if (!Number.isFinite(t) || t < this.range[0] || t > this.range[1])
                continue;
            const x = this.toX(t);
            ctx.fillStyle = event.type === "nap" ? "#aa96c9" : "#6ba69b";
            ctx.beginPath();
            ctx.arc(x, 58, 4, 0, Math.PI * 2);
            ctx.fill();
            if (x < this.width - 65)
                ctx.fillText(
                    event.label || (event.type === "nap" ? "午睡" : "活动"),
                    x + 7,
                    51,
                );
            this.eventHits.push({ x, y: 58, event });
        }
        if (this.drag?.mode === "select") {
            const left = Math.max(
                    this.left,
                    Math.min(this.drag.start.x, this.drag.current.x),
                ),
                right = Math.min(
                    this.width - 14,
                    Math.max(this.drag.start.x, this.drag.current.x),
                );
            ctx.fillStyle = "#3b9b8f1a";
            ctx.fillRect(left, top, right - left, rowHeight * this.rows.length);
        }
        if (
            this.pointer &&
            !this.drag &&
            this.pointer.x >= this.left &&
            this.pointer.x <= this.width - 14
        ) {
            ctx.strokeStyle = "#9caeb6";
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(this.pointer.x, top);
            ctx.lineTo(this.pointer.x, this.height - bottom);
            ctx.stroke();
            ctx.setLineDash([]);
            this.showTooltip(this.toTime(this.pointer.x));
        } else this.tooltip.hidden = true;
    }
    showTooltip(instant) {
        const heading = el("div", "tooltip-heading"),
            caption = el("span", "", `光标 ${this.time.dateTime(new Date(instant).toISOString(), true)}`);
        heading.append(caption);
        if (this.pinned) {
            const close = el("button", "tooltip-close", "取消固定");
            close.type = "button";
            close.addEventListener("click", () => this.unpin());
            heading.append(close);
        }
        this.tooltip.classList.toggle("is-pinned", this.pinned);
        this.tooltip.replaceChildren(
            heading,
            el("div", "tooltip-hint", this.pinned ? "详情已固定，可上下滚动；Esc 或取消固定关闭" : "点击图表固定详情，可上下滚动查看全部指标"),
        );
        const visible = this.series.filter((s) => !this.hidden.has(s.id));
        const windowPoint = (series) => series.observations.find((point) =>
            point.t <= instant && instant < point.end,
        );
        const envGroups = new Map();
        for (const series of visible.filter((s) => s.source === "environment")) {
            const key = `${series.metric}:${series.series_type}`;
            if (!envGroups.has(key)) envGroups.set(key, []);
            envGroups.get(key).push(series);
        }
        const hoverSeries = visible.filter((s) => s.source !== "environment");
        for (const group of envGroups.values()) {
            const matching = group.filter(windowPoint);
            hoverSeries.push(...(matching.length ? matching : group.slice(0, 1)));
        }
        for (const series of hoverSeries) {
            const row = el("div", "tooltip-row"), dot = el("span", "legend-dot"), body = el("div");
            row.dataset.metric = series.metric;
            row.dataset.source = series.source || "garmin";
            row.dataset.seriesId = series.id;
            dot.style.background = metricColor(series.metric);
            const label = seriesLabel(series);
            if (series.source === "environment") {
                const point = windowPoint(series);
                if (point) {
                    add(body, el("strong", "", `${label}  ${number(point.value) ? numericText(point.value, metricInfo(series.metric)?.precision) : "无有效值"} ${metricInfo(series.metric)?.unit || ""}`));
                    const peer = this.series.find((item) => item.metric === series.metric && item.source_ref === series.source_ref && item.boot_ref === series.boot_ref && item.series_type !== series.series_type);
                    for (const line of environmentPointLines(point, series, this.time, peer && windowPoint(peer))) body.append(el("small", "", line));
                } else body.append(el("span", "", `${label} · 此处无观测（分钟缺口）`));
                add(row, dot, body);
                this.tooltip.append(row);
                continue;
            }
            const nearby = series.valid
                .filter((p) => p.t >= this.range[0] && p.t <= this.range[1])
                .reduce(
                    (best, p) =>
                        !best ||
                        Math.abs(p.t - instant) < Math.abs(best.t - instant)
                            ? p
                            : best,
                    null,
                );
            const tolerance = number(series.hover_tolerance_seconds)
                ? Math.max(0, series.hover_tolerance_seconds) * 1000
                : ((this.range[1] - this.range[0]) / this.plotWidth) * 10;
            if (nearby && Math.abs(nearby.t - instant) <= tolerance) {
                const delta = Math.round((nearby.t - instant) / 1000),
                    distance =
                        Math.abs(delta) >= 60
                            ? `${(Math.abs(delta) / 60).toFixed(1)}分`
                            : `${Math.abs(delta)}秒`;
                add(
                    body,
                    el(
                        "strong",
                        "",
                        `${label}  ${numericText(nearby.value, metricInfo(series.metric)?.precision)} ${metricInfo(series.metric)?.unit || ""}`,
                    ),
                    el(
                        "small",
                        "",
                        `${this.time.dateTime(nearby.timestamp, true)} · 距光标${delta < 0 ? "前" : "后"}${distance}${series.aggregation_window_seconds ? ` · ${series.aggregation_window_seconds / 60}分钟聚合` : ""}`,
                    ),
                );
            } else body.append(el("span", "", `${label} · 此处无观测`));
            add(row, dot, body);
            this.tooltip.append(row);
        }
        this.tooltip.hidden = false;
        const width = this.tooltip.offsetWidth;
        this.tooltip.style.left = `${Math.max(5, Math.min(this.width - width - 5, this.pointer.x > this.width * 0.55 ? this.pointer.x - width - 15 : this.pointer.x + 15))}px`;
        const height = this.tooltip.offsetHeight,
            shellTop = this.shell.getBoundingClientRect().top,
            canvasOffset = this.canvas.getBoundingClientRect().top - shellTop,
            viewportTop = 8 - shellTop,
            viewportBottom = window.innerHeight - shellTop - height - 8,
            lower = Math.max(7, viewportTop),
            upper = Math.min(canvasOffset + this.height - height - 7, viewportBottom);
        // A tall chart may begin above the viewport after page scrolling. Keep
        // the fixed heading/close action reachable, even if the popover then
        // needs to extend outside the visible portion of its chart container.
        const top = lower <= upper
            ? Math.max(lower, Math.min(upper, canvasOffset + this.pointer.y + 12))
            : Math.max(viewportTop, Math.min(viewportBottom, canvasOffset + this.pointer.y + 12));
        this.tooltip.style.top = `${top}px`;
    }
}
