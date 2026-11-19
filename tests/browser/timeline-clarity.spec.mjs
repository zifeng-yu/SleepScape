import { test, expect } from "@playwright/test";

const NIGHT = "/sleep?view=night&date=2026-11-09";

test.beforeEach(async ({ request, page }) => {
    expect((await (await request.get("/api/v1/status")).json()).demo).toBe(true);
    page.on("pageerror", (error) => { throw error; });
    // Observe the actual canvas drawing commands, using only the synthetic server.
    await page.addInitScript(() => {
        const context = CanvasRenderingContext2D.prototype;
        const clear = context.clearRect;
        const text = context.fillText;
        const begin = context.beginPath;
        const move = context.moveTo;
        const line = context.lineTo;
        const stroke = context.stroke;
        context.clearRect = function (...args) {
            this.canvas.drawnLabels = [];
            this.canvas.drawnSegments = [];
            return clear.apply(this, args);
        };
        context.fillText = function (value, x, y, maxWidth) {
            this.canvas.drawnLabels?.push({ text: String(value), x, y, align: this.textAlign,
                width: Math.min(this.measureText(value).width, maxWidth ?? Infinity) });
            return text.apply(this, arguments);
        };
        context.beginPath = function (...args) { this.recordedPath = []; return begin.apply(this, args); };
        context.moveTo = function (x, y) { this.recordedPath?.push({ kind: "move", x, y }); return move.apply(this, arguments); };
        context.lineTo = function (x, y) { this.recordedPath?.push({ kind: "line", x, y }); return line.apply(this, arguments); };
        context.stroke = function (...args) {
            this.canvas.drawnSegments?.push(...(this.recordedPath || []));
            return stroke.apply(this, args);
        };
    });
});

async function open(page, url = NIGHT) {
    await page.goto(url);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(".chart-canvas")).toBeVisible();
}

async function drawn(page) {
    return page.locator(".chart-canvas").evaluate((canvas) => ({
        labels: canvas.drawnLabels, segments: canvas.drawnSegments,
        width: canvas.getBoundingClientRect().width,
    }));
}

test("metric headers carry units and y labels align with their horizontal grid lines", async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await open(page);
    await expect(page.locator("#chart-axis-description")).toContainText("各指标独立纵轴");
    const { labels, segments } = await drawn(page);
    for (const name of ["心率 · 次/分（bpm）", "HRV · 毫秒（ms）", "温度 · °C", "相对声音 · dBFS"])
        expect(labels.filter((label) => label.text === name)).toHaveLength(1);
    const yLabels = labels.filter((label) => label.align === "right" && label.x < 76 && /^-?[\d,.]+$/.test(label.text));
    expect(yLabels.length).toBeGreaterThanOrEqual(12);
    for (const label of yLabels)
        expect(segments.some((line) => line.kind === "move" && line.x === 76 && Math.abs(line.y - (label.y - 4)) < 0.01)).toBe(true);
    const clockLabels = labels.filter((label) => /^\d{2}:\d{2}$/.test(label.text));
    expect(clockLabels.length).toBeGreaterThanOrEqual(6);
    for (const clock of new Set(clockLabels.map((label) => label.text)))
        expect(clockLabels.filter((label) => label.text === clock)).toHaveLength(2);
    expect(labels.some((label) => label.text === "11/08")).toBe(true);
    expect(labels.some((label) => label.text === "11/09")).toBe(true);
    // Interior time ticks sit on round hours, while first/last labels retain the true query bounds.
    const firstX = Math.min(...clockLabels.map((label) => label.x));
    const lastX = Math.max(...clockLabels.map((label) => label.x));
    expect(clockLabels.filter((label) => label.x > firstX && label.x < lastX).every((label) => label.text.endsWith(":00"))).toBe(true);
    await page.locator("#chart-shell").screenshot({ path: info.outputPath("synthetic-chart-desktop.png") });
});

test("hidden metrics stop occupying a chart row and return when selected again", async ({ page }) => {
    await open(page);
    const input = page.locator('#timeline-legend label[data-metric="heart_rate"] input');
    const before = await page.locator(".chart-canvas").boundingBox();
    await input.uncheck();
    await expect(page.locator("#chart-axis-description")).not.toContainText("心率 · 次/分（bpm）");
    expect((await page.locator(".chart-canvas").boundingBox()).height).toBeLessThan(before.height);
    await input.check();
    await expect(page.locator("#chart-axis-description")).toContainText("心率 · 次/分（bpm）");
    expect((await page.locator(".chart-canvas").boundingBox()).height).toBe(before.height);
});

test("mobile axes retain long metric titles, midnight dates and independent units without overflow", async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page);
    for (const metric of ["relative_humidity_pct", "illuminance_lux"])
        for (const input of await page.locator(`#timeline-legend label[data-metric="${metric}"] input`).all()) await input.check();
    const { labels, segments, width } = await drawn(page);
    for (const name of ["相对湿度 · %", "光照 · lx", "相对声音 · dBFS"]) {
        const title = labels.find((label) => label.text === name);
        expect(title).toBeTruthy();
        expect(title.x + title.width).toBeLessThan(width - 45);
    }
    const yLabels = labels.filter((label) => label.align === "right" && label.x < 55 && /^-?[\d,.]+$/.test(label.text));
    expect(yLabels.length).toBeGreaterThan(0);
    for (const label of yLabels) {
        expect(label.x - label.width).toBeGreaterThanOrEqual(0);
        expect(segments.some((line) => line.kind === "move" && line.x === 55 && Math.abs(line.y - (label.y - 4)) < 0.01)).toBe(true);
    }
    expect(labels.some((label) => label.text === "11/08")).toBe(true);
    expect(labels.some((label) => label.text === "11/09")).toBe(true);
    const clocks = labels.filter((label) => /^\d{2}:\d{2}$/.test(label.text) && label.y === 18);
    expect(clocks.length).toBeGreaterThanOrEqual(3);
    for (let i = 1; i < clocks.length; i++) {
        const previous = clocks[i - 1], current = clocks[i];
        const previousRight = previous.x + (previous.align === "center" ? previous.width / 2 : 0);
        const currentLeft = current.x - (current.align === "center" ? current.width / 2 : current.width);
        expect(currentLeft - previousRight).toBeGreaterThanOrEqual(6);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.locator("#chart-shell").screenshot({ path: info.outputPath("synthetic-chart-mobile.png") });
});
