import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";
const NIGHT_URL = `/sleep?view=night&date=${NIGHT}`;
const TIMELINE = `/api/v1/nights/${NIGHT}/timeline?max_points=2000`;
const ENVIRONMENT_METRICS = [
    "temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative",
];

test.beforeEach(async ({ request, page }) => {
    // This fixture is built from synthetic protocol batches in a disposable DB.
    // Refuse to open a dashboard backed by personal observations or credentials.
    const status = await request.get("/api/v1/status");
    expect(status.ok()).toBeTruthy();
    expect((await status.json()).demo).toBe(true);
    page.on("pageerror", (error) => {
        throw new Error(`Browser runtime error: ${error.message}`);
    });
});

async function openNight(page, url = NIGHT_URL) {
    await page.goto(url);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(".chart-canvas")).toBeVisible();
}

async function moveTo(page, payload, instant) {
    const canvas = page.locator(".chart-canvas");
    await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    const left = box.width < 500 ? 55 : 76;
    const fraction = (Date.parse(instant) - Date.parse(payload.window.start)) /
        (Date.parse(payload.window.end) - Date.parse(payload.window.start));
    // Multiple readable tracks can make the canvas taller than the viewport.
    // Keep the hover inside the visible canvas after page scrolling.
    const visibleY = visibleCanvasY(page, box);
    await page.mouse.move(box.x + left + fraction * (box.width - left - 14), visibleY);
    await expect(page.locator(".chart-tooltip")).toBeVisible();
}

function visibleCanvasY(page, box, offset = 80) {
    const top = Math.max(12, box.y + 1);
    const bottom = Math.min(page.viewportSize().height - 12, box.y + box.height - 1);
    return Math.max(top, Math.min(bottom, box.y + offset));
}

async function checkMetric(page, metric) {
    const inputs = page.locator(`#timeline-legend label[data-metric="${metric}"] input`);
    for (let i = 0; i < await inputs.count(); i++) await inputs.nth(i).check();
}

function environmentSeries(payload, metric, type = "minute_mean") {
    return payload.series.filter((row) => row.source === "environment" &&
        row.metric === metric && row.series_type === type);
}

function pointAt(payload, metric, stamp, type = "minute_mean") {
    return environmentSeries(payload, metric, type).flatMap((row) => row.points)
        .find((point) => Date.parse(point.window_start) === Date.parse(stamp));
}

test("registered environment observations retain original windows, quality and private references", async ({ request }) => {
    const registry = await (await request.get("/api/v1/metrics")).json();
    expect(registry.items.map((item) => item.id)).toEqual([
        "heart_rate", "hrv", "spo2", "body_battery", ...ENVIRONMENT_METRICS,
    ]);
    const payload = await (await request.get(TIMELINE)).json();
    expect(payload.environment.record_count).toBe(460);
    expect(payload.environment.sources).toHaveLength(2);
    expect(payload.environment.global_time_state.unknown_record_count).toBe(1);
    expect(payload.environment.global_time_state.conflict_record_count).toBe(1);
    for (const metric of ENVIRONMENT_METRICS) {
        const series = environmentSeries(payload, metric);
        expect(series).toHaveLength(2);
        expect(series.flatMap((row) => row.points)).toHaveLength(458);
        for (const row of series) {
            expect(row.source_ref).toMatch(/^source-[a-f0-9]{24}$/);
            expect(row.boot_ref).toMatch(/^boot-[a-f0-9]{24}$/);
            for (const point of row.points) {
                expect(Date.parse(point.window_end) - Date.parse(point.window_start)).toBe(60000);
                expect(point.timestamp).toBe(point.window_start);
                expect(point.time_quality).toBe("estimated");
                expect(point.time_uncertainty_ms).toBeNull();
            }
        }
    }
    expect(environmentSeries(payload, "sound_relative", "minute_peak")).toHaveLength(2);
    expect(pointAt(payload, "temperature_c", "2026-11-08T15:40:00Z")).toBeUndefined();
    const beforeGap = pointAt(payload, "temperature_c", "2026-11-08T15:39:00Z");
    const afterGap = pointAt(payload, "temperature_c", "2026-11-08T15:41:00Z");
    expect(beforeGap.segment_ref).not.toBe(afterGap.segment_ref);
    expect(pointAt(payload, "relative_humidity_pct", "2026-11-08T15:50:00Z"))
        .toMatchObject({ value: null, channel_state: "missing", valid_count: 0 });
    expect(pointAt(payload, "illuminance_lux", "2026-11-08T16:00:00Z"))
        .toMatchObject({ value: null, channel_state: "invalid", valid_count: 0 });
    expect(pointAt(payload, "sound_relative", "2026-11-08T16:10:00Z"))
        .toMatchObject({ channel_state: "partial", valid_count: 55, coverage: { dma_overflow_count: 1, valid_duration_ms: 55000 } });
    expect(pointAt(payload, "sound_relative", "2026-11-08T16:20:00Z").value).toBeNull();
    expect(pointAt(payload, "sound_relative", "2026-11-08T15:10:00Z").event_detection)
        .toMatchObject({ detector_state: "warming", detected_count: null });
    const events = payload.events.filter((event) => event.type === "environment_sound");
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ merged: true, envelope_is_continuous_sound: false,
        metrics: { active_duration_ms: 2000, max_1s_rms_dbfs: -28, baseline_dbfs: null } });
    const serialized = JSON.stringify(payload);
    expect(serialized).not.toContain("browser-private");
    for (const key of ["token_hash", "device_id", "measurement_profile_id", "placement_id", "boot_id", "raw_pcm"])
        expect(serialized).not.toContain(`"${key}"`);
});

test("all four environment tracks and peak selection survive URL reload without accepting unknown metrics", async ({ page }) => {
    await openNight(page, `${NIGHT_URL}&hidden=heart_rate%3Anative,unknown_metric%3Anative&visible=unknown_metric%3Anative`);
    await expect(page.locator('#timeline-legend label[data-metric="heart_rate"] input')).not.toBeChecked();
    for (const metric of ENVIRONMENT_METRICS) {
        await expect(page.locator(`#timeline-legend label[data-metric="${metric}"]`).first()).toBeVisible();
        await checkMetric(page, metric);
    }
    const peak = page.locator('#timeline-legend input[data-series-id^="sound_relative:minute_peak:"]');
    await expect(peak).toHaveCount(1);
    await expect(peak).toHaveAttribute("data-series-count", "2");
    await expect(peak.first()).toBeChecked();
    expect(new URL(page.url()).searchParams.get("visible")).toContain("illuminance_lux:minute_mean:");
    expect(page.url()).not.toContain("unknown_metric");
    await page.reload();
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator('#timeline-legend label[data-metric="heart_rate"] input')).not.toBeChecked();
    await expect(page.locator('#timeline-legend input[data-series-id^="sound_relative:minute_peak:"]').first()).toBeChecked();
    await expect(page.locator('#timeline-legend label[data-metric="illuminance_lux"] input').first()).toBeChecked();
    await expect(page.locator("#environment-panel")).toContainText("不跨段连线");
    await expect(page.locator("#environment-panel")).toContainText("不代表四通道齐全");
});

test("shared hover uses actual minute windows and preserves gaps, partial audio and detector warmup", async ({ page, request }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openNight(page);
    const payload = await (await request.get(TIMELINE)).json();
    const first = environmentSeries(payload, "sound_relative")[0];
    const sound = page.locator(`.tooltip-row[data-series-id="${first.id}"]`);
    await moveTo(page, payload, "2026-11-08T15:10:30Z");
    await expect(sound).toContainText("23:10");
    await expect(sound).toContainText("23:11");
    await expect(sound).toContainText("不能当作0次事件");
    await expect(sound).toContainText("绝对误差未知");
    await expect(page.locator('.tooltip-row[data-metric="temperature_c"]').first()).toContainText("min / max");
    await expect(page.locator('.tooltip-row[data-metric="heart_rate"]')).toHaveCount(1);
    await moveTo(page, payload, "2026-11-08T15:40:30Z");
    await expect(sound).toContainText("此处无观测");
    await expect(page.locator('.tooltip-row[data-metric="temperature_c"]').first()).toContainText("此处无观测");
    await moveTo(page, payload, "2026-11-08T16:10:30Z");
    await expect(sound).toContainText("部分有效");
    await expect(sound).toContainText("55");
    await expect(sound).toContainText("DMA丢样 1");
    await moveTo(page, payload, "2026-11-08T16:20:30Z");
    await expect(sound).toContainText("低于数值下限");
    await expect(sound).not.toContainText("0 dBFS");
});

test("sound summaries keep envelope and active time separate and locate a shared-axis window", async ({ page }) => {
    await openNight(page);
    const event = page.locator('#events-panel details[data-event-type="environment_sound"]').first();
    await event.locator("summary").click();
    await expect(event).toContainText("包络不是持续声音");
    await expect(event).toContainText("2秒");
    await expect(event).toContainText("包含合并事件");
    await expect(event).toContainText("-28");
    const response = page.waitForResponse((item) => item.url().includes(`/nights/${NIGHT}/timeline?`) && item.status() === 200);
    await event.getByRole("button", { name: "定位到时间轴" }).click();
    const payload = await (await response).json();
    expect(Date.parse(payload.window.end) - Date.parse(payload.window.start)).toBeLessThan(3 * 3600000);
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.locator("#environment-panel")).toHaveAttribute("data-state", "available");
});

test("environment status distinguishes upload evidence from capture and realtime availability", async ({ page }) => {
    await page.goto("/sleep?view=status");
    const panel = page.locator("#environment-status-panel");
    await expect(panel).toHaveAttribute("data-state", "available");
    await expect(panel).toContainText("最近接收");
    await expect(panel).toContainText("最近可映射采集");
    await expect(panel).toContainText(/2026[\/-]11[\/-]10/);
    await expect(panel).toContainText("1 / 1");
    await expect(panel).toContainText("12 / 0");
    await expect(panel).toContainText("0.4.0");
    await expect(panel).toContainText("不代表实时在线");
    await expect(panel).not.toContainText("browser-private");
    const ready = page.waitForResponse((response) =>
        response.url().includes("/nights/2026-11-10/timeline?") && response.status() === 200,
    );
    await panel.getByRole("button", { name: "查看最近采集窗口" }).click();
    const payload = await (await ready).json();
    expect(Date.parse(payload.window.start)).toBe(Date.parse("2026-11-10T06:00:00Z"));
    expect(Date.parse(payload.window.end)).toBe(Date.parse("2026-11-10T06:11:00Z"));
    expect(payload.environment.record_count).toBe(10);
    expect(payload.quality).toContain("sleep_window_unavailable");
    await expect(page.getByText("这一天还没有睡眠记录", { exact: true })).toBeVisible();
    await expect(page.locator(".metric-card")).toHaveCount(0);
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(page.locator("#environment-panel")).toHaveAttribute("data-state", "available");
    await expect(page.getByRole("button", { name: "自定义", exact: true })).toHaveAttribute("aria-pressed", "true");
});

for (const state of ["index_not_initialized", "unavailable"]) {
    test(`environment ${state} leaves Garmin usable and recovers through read-only refresh`, async ({ page }) => {
        const mutations = [];
        page.on("request", (request) => {
            if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url());
        });
        await page.route(`**/nights/${NIGHT}/timeline?**`, async (route) => {
            const original = await route.fetch();
            const payload = await original.json();
            payload.series = payload.series.filter((series) => series.source !== "environment");
            payload.events = payload.events.filter((event) => event.type !== "environment_sound");
            payload.environment = { state, record_count: 0, sources: [], segments: [], channel_summaries: [], coverage: null, global_time_state: null };
            await route.fulfill({ response: original, json: payload });
        });
        await openNight(page);
        await expect(page.locator("#environment-panel")).toHaveAttribute("data-state", state);
        await expect(page.locator(".metric-card")).toHaveCount(4);
        await expect(page.locator('#timeline-legend label[data-source="garmin"]')).toHaveCount(4);
        await expect(page.locator("#environment-panel")).toContainText(
            state === "index_not_initialized" ? "刷新不会修改数据库" : "Garmin观测继续显示",
        );
        await page.unroute(`**/nights/${NIGHT}/timeline?**`);
        await page.getByRole("button", { name: "重新读取本地数据" }).click();
        await expect(page.locator("#environment-panel")).toHaveAttribute("data-state", "available");
        expect(mutations).toEqual([]);
    });
}

test("metric registry failure preserves cards and successful refresh restores tracks", async ({ page }) => {
    await page.route("**/api/v1/metrics", (route) => route.fulfill({ status: 503, json: { error: "unavailable" } }));
    await page.goto(NIGHT_URL);
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.locator("#chart-shell")).toContainText("指标配置暂不可用");
    await expect(page.locator("#sleep-analysis-panel")).toContainText("尚无当前配置的已完成请求");
    await page.unroute("**/api/v1/metrics");
    await page.getByRole("button", { name: "重新读取本地数据" }).click();
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(page.locator('#timeline-legend label[data-source="environment"]')).toHaveCount(5);
    await expect(page.locator('#timeline-legend label[data-source="environment"] input[data-series-count="2"]')).toHaveCount(5);
});

test("a date without Garmin can query an afternoon window on mobile and keep negative or zero observations", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 320, height: 900 });
    await page.goto("/sleep?view=night&date=2026-11-10");
    await expect(page.getByText("这一天还没有睡眠记录", { exact: true })).toBeVisible();
    await expect(page.locator(".metric-card")).toHaveCount(0);
    await page.getByRole("button", { name: "自定义", exact: true }).click();
    await page.locator('#custom-range input[name="from"]').fill("2026-11-10T14:00");
    await page.locator('#custom-range input[name="to"]').fill("2026-11-10T14:10");
    const ready = page.waitForResponse((response) => response.url().includes("/nights/2026-11-10/timeline?") && response.status() === 200);
    await page.getByRole("button", { name: "应用范围" }).click();
    const payload = await (await ready).json();
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    expect(payload.environment.record_count).toBe(10);
    expect(pointAt(payload, "temperature_c", "2026-11-10T06:02:00Z"))
        .toMatchObject({ value: -5, min: -5.2, max: -4.8 });
    expect(pointAt(payload, "illuminance_lux", "2026-11-10T06:02:00Z").value).toBe(0);
    expect(pointAt(payload, "sound_relative", "2026-11-10T06:02:00Z").value).toBe(-120);
    await checkMetric(page, "illuminance_lux");
    await moveTo(page, payload, "2026-11-10T06:02:30Z");
    await expect(page.locator('.tooltip-row[data-metric="temperature_c"]')).toContainText("-5");
    await expect(page.locator('.tooltip-row[data-metric="illuminance_lux"]')).toContainText("0 lx");
    await expect(page.locator('.tooltip-row[data-metric="sound_relative"]')).toContainText("-120");
    await page.mouse.move(0, 0);
    await page.locator("#environment-panel details > summary").click();
    await expect(page.locator("#environment-panel")).toContainText("-5.2 — -4.8");
    await expect(page.locator("#environment-panel")).toContainText("-120 — -100");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("synthetic-environment-mobile.png"), fullPage: true });
});

test("environment presentation drops untrusted metadata and never inserts HTML", async ({ page }) => {
    await page.route(`**/nights/${NIGHT}/timeline?**`, async (route) => {
        const original = await route.fetch();
        const payload = await original.json();
        const poison = '<img src=x onerror="window.environmentInjected=true">private-env-marker';
        payload.environment.error = poison;
        payload.environment.sources[0].firmware_version = poison;
        payload.environment.sources[0].placement_id = poison;
        payload.environment.channel_summaries[0].quality.push(poison);
        await route.fulfill({ response: original, json: payload });
    });
    await openNight(page);
    await page.locator("#environment-panel details > summary").click();
    await expect(page.locator("#environment-panel img")).toHaveCount(0);
    await expect(page.locator("#environment-panel")).not.toContainText("private-env-marker");
    expect(await page.evaluate(() => window.environmentInjected)).toBeUndefined();
});

test("mobile observation details stay within the viewport and can be pinned, scrolled and dismissed", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 320, height: 800 });
    // Use a one-hour window so a mobile pointer can select the intended minute.
    await openNight(page);
    await page.getByRole("button", { name: "自定义", exact: true }).click();
    await page.locator('#custom-range input[name="from"]').fill("2026-11-09T00:40");
    await page.locator('#custom-range input[name="to"]').fill("2026-11-09T01:40");
    const ready = page.waitForResponse((response) => response.url().includes(`/nights/${NIGHT}/timeline?`) && response.status() === 200);
    await page.getByRole("button", { name: "应用范围" }).click();
    const payload = await (await ready).json();
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    for (const metric of ENVIRONMENT_METRICS) await checkMetric(page, metric);
    await moveTo(page, payload, "2026-11-08T17:10:30Z");
    // A short click fixes the same minute; the hover layer must not swallow it.
    await page.mouse.down();
    await page.mouse.up();
    const tooltip = page.getByRole("region", { name: "观测详情", exact: true, includeHidden: true });
    await expect(tooltip).toHaveClass(/is-pinned/);
    await expect(tooltip.locator(".tooltip-row")).toHaveCount(9);
    const bounds = await tooltip.boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(320);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(800);
    expect(await tooltip.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.wheel(0, 500);
    await expect.poll(() => tooltip.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
    const audio = tooltip.locator('.tooltip-row[data-series-id^="sound_relative:minute_peak:"]');
    const counts = audio.getByText(/^事件检测 3次/);
    await counts.scrollIntoViewIfNeeded();
    await expect(audio).toContainText("PCM有效／已读／预期");
    await expect(audio).toContainText("保存 1 · 合并 2 · 丢弃 0");
    const countsBounds = await counts.boundingBox();
    const scrolledBounds = await tooltip.boundingBox();
    expect(countsBounds.y).toBeGreaterThanOrEqual(scrolledBounds.y);
    expect(countsBounds.y + countsBounds.height).toBeLessThanOrEqual(scrolledBounds.y + scrolledBounds.height);
    await page.screenshot({ path: testInfo.outputPath("synthetic-environment-pinned-details.png") });
    await page.keyboard.press("Escape");
    await expect(tooltip).toBeHidden();
    await expect(tooltip).not.toHaveClass(/is-pinned/);

    // A lower track remains clickable after the top of the long chart scrolls away.
    const canvas = page.locator(".chart-canvas");
    await canvas.evaluate((element) => {
        element.scrollIntoView({ block: "end", behavior: "instant" });
        window.scrollBy({ top: 240, behavior: "instant" });
    });
    let box = await canvas.boundingBox();
    expect(box.y).toBeLessThan(0);
    const minuteFraction = (Date.parse("2026-11-08T17:10:30Z") - Date.parse(payload.window.start)) /
        (Date.parse(payload.window.end) - Date.parse(payload.window.start));
    const lowerPoint = { x: box.x + 55 + minuteFraction * (box.width - 55 - 14), y: box.y + box.height - 80 };
    expect(await canvas.evaluate((element, point) =>
        document.elementFromPoint(point.x, point.y) === element, lowerPoint)).toBe(true);
    await page.mouse.click(lowerPoint.x, lowerPoint.y);
    await expect(tooltip).toHaveClass(/is-pinned/);
    const lowerBounds = await tooltip.boundingBox();
    expect(lowerBounds.x).toBeGreaterThanOrEqual(0);
    expect(lowerBounds.y).toBeGreaterThanOrEqual(0);
    expect(lowerBounds.x + lowerBounds.width).toBeLessThanOrEqual(320);
    expect(lowerBounds.y + lowerBounds.height).toBeLessThanOrEqual(800);
    await expect(tooltip.getByRole("button", { name: "取消固定", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(tooltip).toBeHidden();

    // Unmodified scrolling remains page scrolling once details have been released.
    await canvas.scrollIntoViewIfNeeded();
    box = await canvas.boundingBox();
    await page.mouse.move(box.x + box.width * 0.7, visibleCanvasY(page, box));
    const previousScroll = await page.evaluate(() => window.scrollY);
    await page.mouse.wheel(0, 120);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(previousScroll);

    // The visible button is the alternate release path, and does not disable brushing.
    await moveTo(page, payload, "2026-11-08T17:10:30Z");
    await page.mouse.down();
    await page.mouse.up();
    await expect(tooltip).toHaveClass(/is-pinned/);
    await tooltip.getByRole("button", { name: "取消固定", exact: true }).click();
    await expect(tooltip).toBeHidden();
    await canvas.scrollIntoViewIfNeeded();
    box = await canvas.boundingBox();
    const response = page.waitForResponse((item) => item.url().includes(`/nights/${NIGHT}/timeline?`) && item.status() === 200);
    await page.mouse.move(box.x + box.width * 0.4, visibleCanvasY(page, box));
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.8, visibleCanvasY(page, box, 120), { steps: 8 });
    await page.mouse.up();
    const zoomed = await (await response).json();
    expect(Date.parse(zoomed.window.end) - Date.parse(zoomed.window.start))
        .toBeLessThan(Date.parse(payload.window.end) - Date.parse(payload.window.start));
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(canvas).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
