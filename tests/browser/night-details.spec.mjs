import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";
const URL = `/sleep?view=night&date=${NIGHT}`;

test.beforeEach(async ({ request, page }) => {
    expect((await (await request.get("/api/v1/status")).json()).demo).toBe(true);
    page.on("pageerror", (error) => { throw error; });
});

async function open(page, url = URL) {
    await page.goto(url);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
}

test("each source disclosure expands its own card and leaves neighbouring card heights unchanged", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await open(page);
    const cards = page.locator(".metric-card");
    const heights = await cards.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().height));
    await cards.nth(0).locator('summary[aria-label="主睡眠时长的来源与口径"]').click();
    await expect(cards.nth(0).locator("details")).toHaveAttribute("open", "");
    expect((await cards.nth(0).boundingBox()).height).toBeGreaterThan(heights[0]);
    for (let index = 1; index < 4; index++) {
        await expect(cards.nth(index).locator("details")).not.toHaveAttribute("open", "");
        expect((await cards.nth(index).boundingBox()).height).toBeCloseTo(heights[index], 1);
    }
    await cards.nth(1).locator('summary[aria-label="睡眠平均心率的来源与口径"]').click();
    await expect(cards.nth(0).locator("details")).toHaveAttribute("open", "");
    await expect(cards.nth(1).locator("details")).toHaveAttribute("open", "");
});

test("one metric selector controls all boot segments and preserves old per-series URL choices", async ({ page, request }) => {
    const payload = await (await request.get(`/api/v1/nights/${NIGHT}/timeline?max_points=2000`)).json();
    const ids = payload.series.filter((series) => series.metric === "temperature_c").map((series) => series.id);
    expect(ids).toHaveLength(2);
    await open(page, `${URL}&hidden=${encodeURIComponent(ids[0])}`);
    const checkbox = page.locator('#timeline-legend label[data-metric="temperature_c"] input');
    await expect(checkbox).toHaveCount(1);
    await expect(checkbox).toHaveAttribute("data-series-count", "2");
    expect(await checkbox.evaluate((node) => node.indeterminate)).toBe(true);
    await checkbox.check();
    expect(new globalThis.URL(page.url()).searchParams.get("hidden") || "").not.toContain(ids[0]);
    await checkbox.uncheck();
    for (const id of ids) expect(new globalThis.URL(page.url()).searchParams.get("hidden")).toContain(id);
    await page.reload();
    await expect(checkbox).not.toBeChecked();
    await checkbox.check();
    await expect(page.locator("#chart-axis-description")).toContainText("温度 · °C");
});

for (const [metric, seriesType, preference, selected] of [
    ["temperature_c", "minute_mean", "hidden", false],
    ["sound_relative", "minute_peak", "visible", true],
]) {
    test(`64 boot-segment ${preference} choices survive a URL round trip`, async ({ page, request }) => {
        const payload = await (await request.get(`/api/v1/nights/${NIGHT}/timeline?max_points=2000`)).json();
        const sample = payload.series.find((series) => series.metric === metric && series.series_type === seriesType);
        expect(sample).toBeTruthy();
        // Real registry metadata and series shape, with distinct synthetic boot references.
        payload.series = Array.from({ length: 64 }, (_, index) => {
            const boot = `boot-${index.toString(16).padStart(24, "0")}`;
            return { ...sample, boot_ref: boot,
                id: `${metric}:${seriesType}:${sample.source_ref}:${boot}`,
                points: sample.points.slice(0, 1) };
        });
        payload.events = [];
        const expectedIDs = payload.series.map((series) => series.id).sort();
        expect(expectedIDs.join(",").length).toBeGreaterThan(5000);
        await page.route(`**/api/v1/nights/${NIGHT}/timeline?**`, (route) => route.fulfill({ json: payload }));
        await open(page);
        const checkbox = page.locator(`#timeline-legend label[data-metric="${metric}"][data-series-type="${seriesType}"] input`);
        await expect(checkbox).toHaveAttribute("data-series-count", "64");
        await expect(checkbox).toBeChecked({ checked: !selected });
        await checkbox.setChecked(selected);
        const saved = new globalThis.URL(page.url()).searchParams.get(preference);
        expect(saved.split(",")).toEqual(expectedIDs);
        await page.reload();
        await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
        await expect(checkbox).toHaveAttribute("data-series-count", "64");
        await expect(checkbox).toBeChecked({ checked: selected });
        expect(await checkbox.evaluate((node) => node.indeterminate)).toBe(false);
        expect(new globalThis.URL(page.url()).searchParams.get(preference)).toBe(saved);
    });
}

test("nine sound summaries are explained as summaries and category filters retain zero-count states", async ({ page }) => {
    await page.route(`**/api/v1/nights/${NIGHT}`, async (route) => {
        const payload = await (await route.fetch()).json();
        payload.activities = [];
        await route.fulfill({ json: payload });
    });
    await page.route(`**/api/v1/nights/${NIGHT}/timeline?**`, async (route) => {
        const payload = await (await route.fetch()).json();
        const sample = payload.events.find((event) => event.type === "environment_sound");
        payload.events = Array.from({ length: 9 }, (_, index) => ({ ...sample, key: `synthetic-sound-${index}` }));
        await route.fulfill({ json: payload });
    });
    await open(page);
    const panel = page.locator("#events-panel");
    await expect(panel.getByRole("heading", { name: "这晚的关联记录" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "声音摘要 9", exact: true })).toBeVisible();
    await expect(panel.getByRole("button", { name: "运动 0", exact: true })).toBeVisible();
    await expect(panel.getByRole("button", { name: "午睡 0", exact: true })).toBeVisible();
    await expect(panel.locator("details.event:visible")).toHaveCount(9);
    await panel.locator(".event-help summary").click();
    await expect(panel.locator(".event-help")).toContainText("更不是觉醒次数");
    await expect(panel).toContainText("完整的每分钟环境读数在上方曲线");
    await panel.getByRole("button", { name: "运动 0", exact: true }).click();
    await expect(panel.locator("details.event:visible")).toHaveCount(0);
    await expect(panel.locator(".event-empty")).toBeVisible();
    await panel.getByRole("button", { name: "声音摘要 9", exact: true }).click();
    await expect(panel.locator("details.event:visible")).toHaveCount(9);
    await expect(panel.locator("details.event").first().locator("summary")).toContainText("较响累计");
});

test("event cap is explicit and narrow layouts keep filters and source disclosures readable", async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route(`**/api/v1/nights/${NIGHT}/timeline?**`, async (route) => {
        const payload = await (await route.fetch()).json();
        payload.environment.events_truncated = true;
        await route.fulfill({ json: payload });
    });
    await open(page);
    await page.locator(".metric-card").first().locator("summary").click();
    await expect(page.locator(".metric-card").first().locator("details dl")).toBeVisible();
    const panel = page.locator("#events-panel");
    await expect(panel).toContainText("当前条数不是完整总数");
    await panel.getByRole("button", { name: /^声音摘要/ }).click();
    await panel.locator(".event-help summary").click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: info.outputPath("synthetic-events-mobile.png") });
});

test("observed data cards do not show date-based confirmation badges", async ({ page }) => {
    await open(page);
    await expect(page.locator(".cards")).not.toContainText("待确认");
    await expect(page.locator(".date-caption")).not.toContainText("待确认");
    await page.locator(".metric-card").first().locator("summary").click();
    await expect(page.locator(".metric-card").first()).toContainText("已记录");
});

test("unavailable sound data does not masquerade as zero summaries", async ({ page }) => {
    await page.route(`**/api/v1/nights/${NIGHT}/timeline?**`, async (route) => {
        const payload = await (await route.fetch()).json();
        payload.events = payload.events.filter((event) => event.type !== "environment_sound");
        payload.series = payload.series.filter((series) => series.source !== "environment");
        payload.environment = { state: "unavailable" };
        await route.fulfill({ json: payload });
    });
    await open(page);
    const panel = page.locator("#events-panel");
    await panel.getByRole("button", { name: "声音摘要 —", exact: true }).click();
    await expect(panel.locator(".event-selection")).toHaveText("声音摘要暂不可用");
    await expect(panel.locator(".event-empty")).toContainText("不能判断摘要条数");
    await expect(panel.getByRole("button", { name: "声音摘要 0", exact: true })).toHaveCount(0);
});

test("sound playback requests only a published clip and only after a click", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 760 });
    await page.addInitScript(() => {
        const connect = AudioNode.prototype.connect;
        AudioNode.prototype.connect = function (...args) {
            if (this instanceof MediaElementAudioSourceNode) {
                window.clipOutput = args[0];
                window.clipContext = this.context;
            }
            return connect.apply(this, args);
        };
    });
    const key = "record-" + "a".repeat(24);
    await page.route(`**/api/v1/nights/${NIGHT}/timeline?**`, async (route) => {
        const payload = await (await route.fetch()).json();
        const event = payload.events.find((item) => item.type === "environment_sound");
        payload.events = [{ ...event, key, audio_available: true }, { ...event, audio_available: false }];
        await route.fulfill({ json: payload });
    });
    let requests = 0;
    await page.route(`**/api/v1/event-audio/${key}`, async (route) => {
        requests++;
        // An 8 kHz mono WAV with low rumble and a higher reference tone.
        const wav = Buffer.alloc(44 + 8 * 8000 * 2);
        wav.write("RIFF"); wav.writeUInt32LE(wav.length - 8, 4);
        wav.write("WAVEfmt ", 8); wav.writeUInt32LE(16, 16);
        wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
        wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28);
        wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
        wav.write("data", 36); wav.writeUInt32LE(wav.length - 44, 40);
        for (let i = 0; i < (wav.length - 44) / 2; i++) {
            const phase = 2 * Math.PI * i / 8000;
            wav.writeInt16LE(Math.round(4000 * (Math.sin(20 * phase) + Math.sin(1000 * phase))), 44 + i * 2);
        }
        await route.fulfill({ contentType: "audio/wav", body: wav });
    });
    await open(page);
    const player = page.locator("#events-panel button", { hasText: /^播放$/ });
    await expect(player).toHaveCount(1);
    const mode = page.getByRole("combobox", { name: "回听模式" });
    const audio = page.locator("#events-panel audio");
    await expect(mode).toHaveCount(1);
    await expect(mode).toHaveValue("original");
    await mode.selectOption("filtered");
    await mode.selectOption("original");
    expect(requests).toBe(0);
    await player.click();
    const pause = page.getByRole("button", { name: "暂停", exact: true });
    await expect(pause).toBeVisible();
    await expect.poll(() => audio.evaluate(a => a.currentTime)).toBeGreaterThan(0.3);
    const position = await audio.evaluate(a => a.currentTime);
    const initialRequests = requests;
    expect(initialRequests).toBeGreaterThan(0);
    await mode.selectOption("filtered");
    await expect.poll(() => page.evaluate(() => window.clipOutput?.type)).toBe("highpass");
    const gains = await page.evaluate(() => {
        const magnitude = new Float32Array(3);
        window.clipOutput.getFrequencyResponse(new Float32Array([20, 80, 1000]), magnitude, new Float32Array(3));
        return Array.from(magnitude);
    });
    expect(gains[0]).toBeLessThan(0.07);
    expect(gains[1]).toBeCloseTo(Math.SQRT1_2, 2);
    expect(gains[2]).toBeCloseTo(1, 2);
    await mode.selectOption("original");
    await expect.poll(() => page.evaluate(() => window.clipOutput === window.clipContext.destination)).toBe(true);
    await expect(pause).toBeVisible();
    expect(await audio.evaluate(a => a.currentTime)).toBeGreaterThan(position);
    await pause.click();
    await expect(player).toBeVisible();
    const paused = await audio.evaluate(a => a.currentTime);
    await mode.selectOption("filtered");
    expect(await audio.evaluate(a => a.paused)).toBe(true);
    expect(await audio.evaluate(a => a.currentTime)).toBe(paused);
    await player.click();
    await expect(pause).toBeVisible();
    await expect.poll(() => page.evaluate(() => window.clipOutput?.type)).toBe("highpass");
    expect(requests).toBe(initialRequests);
    expect(await page.locator("#events-panel").evaluate(p => p.scrollWidth <= p.clientWidth)).toBe(true);
    await page.locator('[data-event-filter="activity"]').click();
    await expect.poll(() => audio.evaluate(a => a.paused)).toBe(true);
});
