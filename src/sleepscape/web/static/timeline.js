/* Canvas timeline with injected view callbacks; no fetch or application state. */
import { el, add } from "./dom.js";
import {
    number,
    numericText,
    COLORS,
    METRIC_NAMES,
    seriesLabel,
} from "./format.js";

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
        this.canvas = el("canvas", "chart-canvas");
        this.canvas.tabIndex = 0;
        this.canvas.setAttribute("role", "img");
        this.canvas.setAttribute(
            "aria-label",
            "共享时间轴散点图。加减键缩放，左右键平移，Home恢复整夜。每个点代表真实观测，无数据处留空。",
        );
        this.tooltip = el("div", "chart-tooltip");
        this.tooltip.hidden = true;
        const description = el(
            "p",
            "sr-only",
            "心率、HRV、血氧与Body Battery按真实UTC时间对齐。用指针或触摸选点，显示观测时刻和距离光标的时间差；没有邻近观测时不显示估算值。",
        );
        add(shell, this.canvas, this.tooltip, description);
        this.range = [
            Date.parse(data.window.start),
            Date.parse(data.window.end),
        ];
        this.series = (data.series || []).map((s) => ({
            ...s,
            valid: (s.points || [])
                .filter(
                    (p) =>
                        number(p.value) &&
                        Number.isFinite(Date.parse(p.timestamp)),
                )
                .map((p) => ({ ...p, t: Date.parse(p.timestamp) }))
                .sort((a, b) => a.t - b.t),
        }));
        this.eventHits = [];
        this.rows = ["heart_rate", "hrv", "spo2", "body_battery"];
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
            if (!this.drag) {
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
    draw() {
        this.width = Math.max(250, this.shell.clientWidth);
        this.left = this.width < 500 ? 55 : 76;
        this.plotWidth = this.width - this.left - 14;
        const rowHeight = this.width < 500 ? 84 : 90,
            top = 36,
            bottom = 35;
        this.height = top + this.rows.length * rowHeight + bottom;
        const ratio = Math.min(window.devicePixelRatio || 1, 3);
        this.canvas.width = Math.round(this.width * ratio);
        this.canvas.height = Math.round(this.height * ratio);
        this.canvas.style.height = `${this.height}px`;
        const ctx = this.canvas.getContext("2d");
        ctx.scale(ratio, ratio);
        ctx.clearRect(0, 0, this.width, this.height);
        ctx.font =
            '10px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        const startSleep = Date.parse(this.sleep.start_at),
            endSleep = Date.parse(this.sleep.end_at);
        if (Number.isFinite(startSleep) && Number.isFinite(endSleep)) {
            const x1 = Math.max(this.left, this.toX(startSleep)),
                x2 = Math.min(this.width - 14, this.toX(endSleep));
            if (x2 > x1) {
                ctx.fillStyle = "#f8f6fc";
                ctx.fillRect(x1, top, x2 - x1, rowHeight * this.rows.length);
            }
        }
        const ticks = this.width < 500 ? 4 : 6;
        for (let i = 0; i <= ticks; i++) {
            const x = this.left + (this.plotWidth * i) / ticks,
                instant =
                    this.range[0] +
                    ((this.range[1] - this.range[0]) * i) / ticks;
            ctx.strokeStyle = "#edf1f4";
            ctx.setLineDash([2, 4]);
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, this.height - bottom);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "#647684";
            ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
            ctx.fillText(
                this.time.clockTime(new Date(instant).toISOString()),
                x,
                this.height - 17,
            );
            if (
                i === 0 ||
                (i === ticks &&
                    this.time.localInput(instant).slice(0, 10) !==
                        this.time.localInput(this.range[0]).slice(0, 10))
            ) {
                ctx.font = "11px sans-serif";
                ctx.fillText(
                    this.time.localInput(instant).slice(5, 10).replace("-", "/"),
                    x,
                    this.height - 5,
                );
                ctx.font = "11px sans-serif";
            }
        }
        this.rows.forEach((metric, index) => {
            const y = top + index * rowHeight,
                group = this.series.filter(
                    (s) => s.metric === metric && !this.hidden.has(s.id),
                ),
                points = group.flatMap((s) =>
                    s.valid.filter(
                        (p) => p.t >= this.range[0] && p.t <= this.range[1],
                    ),
                );
            const values = points.map((p) => p.value);
            let min = values.length ? Math.min(...values) : 0,
                max = values.length ? Math.max(...values) : 100;
            if (metric === "spo2") {
                min = Math.min(90, min);
                max = 100;
            } else if (metric === "body_battery") {
                min = 0;
                max = 100;
            } else {
                const padding = Math.max(4, (max - min) * 0.2);
                min = Math.max(0, min - padding);
                max += padding;
            }
            ctx.textAlign = "left";
            ctx.fillStyle = "#6c8190";
            ctx.font = `${this.width < 500 ? 11 : 12}px sans-serif`;
            ctx.fillText(
                metric === "body_battery" ? "能量" : METRIC_NAMES[metric],
                1,
                y + 15,
            );
            ctx.fillStyle = "#647684";
            ctx.font = "11px sans-serif";
            ctx.fillText(
                group[0]?.unit ||
                    {
                        heart_rate: "bpm",
                        hrv: "ms",
                        spo2: "%",
                        body_battery: "/100",
                    }[metric],
                1,
                y + 29,
            );
            ctx.strokeStyle = "#edf1f3";
            ctx.beginPath();
            ctx.moveTo(this.left, y + rowHeight);
            ctx.lineTo(this.width - 14, y + rowHeight);
            ctx.stroke();
            if (!points.length) {
                ctx.fillStyle = "#647684";
                ctx.font = "11px sans-serif";
                ctx.fillText(
                    group.length ? "此范围无有效观测" : "序列已隐藏",
                    this.left + 12,
                    y + rowHeight / 2 + 3,
                );
            } else {
                ctx.textAlign = "right";
                ctx.fillStyle = "#647684";
                ctx.font = "11px sans-serif";
                ctx.fillText(numericText(max), this.left - 7, y + 15);
                ctx.fillText(
                    numericText(min),
                    this.left - 7,
                    y + rowHeight - 10,
                );
            }
            for (const series of group)
                for (const p of series.valid) {
                    if (p.t < this.range[0] || p.t > this.range[1]) continue;
                    const x = this.toX(p.t),
                        py =
                            y +
                            13 +
                            ((max - p.value) / Math.max(1, max - min)) *
                                (rowHeight - 27);
                    ctx.fillStyle = COLORS[metric];
                    ctx.globalAlpha = 0.82;
                    ctx.beginPath();
                    ctx.arc(
                        x,
                        py,
                        series.series_type === "hourly_average" ||
                            metric === "body_battery"
                            ? 3.5
                            : 1.65,
                        0,
                        Math.PI * 2,
                    );
                    ctx.fill();
                    ctx.globalAlpha = 1;
                }
        });
        this.eventHits = [];
        ctx.font = "11px sans-serif";
        ctx.textAlign = "left";
        for (const event of this.data.events || []) {
            const t = Date.parse(event.start_at);
            if (!Number.isFinite(t) || t < this.range[0] || t > this.range[1])
                continue;
            const x = this.toX(t);
            ctx.fillStyle = event.type === "nap" ? "#aa96c9" : "#6ba69b";
            ctx.beginPath();
            ctx.arc(x, 20, 3, 0, Math.PI * 2);
            ctx.fill();
            if (x < this.width - 65)
                ctx.fillText(
                    event.label || (event.type === "nap" ? "午睡" : "活动"),
                    x + 7,
                    23,
                );
            this.eventHits.push({ x, y: 20, event });
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
        this.tooltip.replaceChildren(
            el(
                "div",
                "tooltip-heading",
                `光标 ${this.time.dateTime(new Date(instant).toISOString(), true)}`,
            ),
        );
        for (const series of this.series.filter(
            (s) => !this.hidden.has(s.id),
        )) {
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
            const row = el("div", "tooltip-row"),
                dot = el("span", "legend-dot");
            dot.style.background = COLORS[series.metric];
            const body = el("div");
            const label = seriesLabel(series);
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
                        `${label}  ${numericText(nearby.value, 1)} ${series.unit || ""}`,
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
        this.tooltip.style.top = `${Math.max(7, Math.min(this.height - this.tooltip.offsetHeight - 40, this.pointer.y + 12))}px`;
    }
}
