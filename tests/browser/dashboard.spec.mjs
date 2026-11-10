import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";
const NIGHT_URL = `/?view=night&date=${NIGHT}`;
const timelineResponse = (response) =>
    response.url().includes(`/nights/${NIGHT}/timeline?`) &&
    response.status() === 200;

test.beforeEach(async ({ request, page }) => {
    // This guard must pass before opening or capturing any dashboard page.
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => {
        throw new Error(`Browser runtime error: ${error.message}`);
    });
});

async function openNight(page) {
    await page.goto(NIGHT_URL);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(
        page.getByRole("heading", { name: "夜晚详情", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(page.locator("#chart-shell")).toHaveAttribute(
        "aria-busy",
        "false",
    );
}

async function settleTimeline(page, action) {
    const responsePromise = page.waitForResponse(timelineResponse);
    await action();
    const payload = await (await responsePromise).json();
    await expect(page.locator("#chart-shell")).toHaveAttribute(
        "aria-busy",
        "false",
    );
    await expect(page.locator(".chart-canvas")).toBeVisible();
    return payload;
}

async function plotPoint(page, payload, timestamp, relativeY = 80) {
    const canvas = page.locator(".chart-canvas");
    await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    const left = box.width < 500 ? 55 : 76;
    const fraction =
        (Date.parse(timestamp) - Date.parse(payload.window.start)) /
        (Date.parse(payload.window.end) - Date.parse(payload.window.start));
    return {
        x: box.x + left + fraction * (box.width - left - 14),
        y: box.y + relativeY,
    };
}

test("night cards preserve values, expose sources, and use icon-only date controls", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1050 });
    await openNight(page);
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(
        page.getByRole("button", { name: "上一晚", exact: true }),
    ).toHaveText("");
    await expect(
        page.getByRole("button", { name: "下一晚", exact: true }),
    ).toHaveText("");
    const load = page
        .locator(".metric-card")
        .filter({ hasText: "昨日运动负荷" });
    await expect(load.locator(".metric-value")).toHaveText("122");
    await expect(load.locator(".metric-value .unit")).toHaveText("");
    await load.getByText("来源与口径", { exact: true }).click();
    await expect(load.locator("details")).toHaveAttribute("open", "");
    await expect(load).toContainText("activityTrainingLoad");
    await expect(load).toContainText("30 / 30");
    await expect(page.locator(".sleep-card")).toContainText("无分期时间线");
    await expect(page.locator(".sleep-card")).toContainText(
        "不代表整夜发生顺序",
    );
    await expect(page.locator("body")).not.toContainText(
        "PRIVATE_MARKER_MUST_NOT_REACH_BROWSER",
    );
    await load.getByText("来源与口径", { exact: true }).click();
    await page.screenshot({
        path: testInfo.outputPath("desktop-synthetic.png"),
        fullPage: true,
    });
});

test("date navigation, empty dates, refresh and browser history preserve the selected date", async ({
    page,
}) => {
    await openNight(page);
    await page.getByRole("button", { name: "上一晚", exact: true }).click();
    await expect(page).toHaveURL(/date=2026-11-08/);
    await expect(page.getByLabel("选择醒来日期")).toHaveValue("2026-11-08");
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await page.getByRole("button", { name: "下一晚", exact: true }).click();
    await expect(page).toHaveURL(/date=2026-11-09/);
    await expect(page.getByLabel("选择醒来日期")).toHaveValue(NIGHT);
    await page.getByLabel("选择醒来日期").fill("2000-01-01");
    await expect(page).toHaveURL(/date=2000-01-01/);
    await expect(
        page.getByText("这一天还没有睡眠记录", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".metric-card")).toHaveCount(0);
    await page.reload();
    await expect(page.getByLabel("选择醒来日期")).toHaveValue("2000-01-01");
    await expect(
        page.getByText("这一天还没有睡眠记录", { exact: true }),
    ).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/date=2026-11-09/);
    await expect(page.locator(".chart-canvas")).toBeVisible();
});

test("legend selection survives URL refresh and restores on browser back", async ({
    page,
}) => {
    await openNight(page);
    const heart = page
        .locator("#timeline-legend label")
        .filter({ hasText: "心率" })
        .getByRole("checkbox");
    await heart.uncheck();
    await expect(page).toHaveURL(/hidden=heart_rate%3Anative/);
    await page.reload();
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(heart).not.toBeChecked();
    await page.goBack();
    await expect(page).not.toHaveURL(/hidden=/);
    await expect(heart).toBeChecked();
});

test("linked tooltip reports actual observations and leaves a deliberate heart-rate gap empty", async ({
    page,
    request,
}) => {
    await openNight(page);
    const payload = await (
        await request.get(`/api/v1/nights/${NIGHT}/timeline`)
    ).json();
    const series = payload.series.find((item) => item.metric === "heart_rate");
    const observed = series.points.find((point) => point.value !== null);
    const point = await plotPoint(page, payload, observed.timestamp);
    await page.mouse.move(point.x, point.y);
    const tooltip = page.locator(".chart-tooltip");
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText("距光标");
    await expect(tooltip).toContainText("23:10:00");
    await expect(tooltip.locator(".tooltip-row")).toHaveCount(4);
    await expect(tooltip).toContainText("小时均值");
    const gap = await plotPoint(
        page,
        payload,
        new Date(Date.parse(observed.timestamp) + 115 * 60000).toISOString(),
    );
    await page.mouse.move(gap.x, gap.y);
    await expect(
        tooltip
            .locator(".tooltip-row")
            .filter({ hasText: /^心率 · 此处无观测$/ }),
    ).toBeVisible();
    await expect(
        tooltip.locator(".tooltip-row").filter({ hasText: "HRV" }),
    ).toContainText("距光标");
});

test("box zoom, keyboard restore and drag panning update all series in a bounded shared window", async ({
    page,
    request,
}) => {
    await openNight(page);
    const original = await (
        await request.get(`/api/v1/nights/${NIGHT}/timeline`)
    ).json();
    const originalDuration =
        Date.parse(original.window.end) - Date.parse(original.window.start);
    const canvas = page.locator(".chart-canvas");
    await canvas.scrollIntoViewIfNeeded();
    let box = await canvas.boundingBox();
    const zoomed = await settleTimeline(page, async () => {
        await page.mouse.move(box.x + box.width * 0.35, box.y + 75);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.7, box.y + 150, {
            steps: 8,
        });
        await page.mouse.up();
    });
    const zoomDuration =
        Date.parse(zoomed.window.end) - Date.parse(zoomed.window.start);
    expect(zoomDuration).toBeLessThan(originalDuration);
    expect(zoomDuration).toBeGreaterThanOrEqual(5 * 60000);
    await expect(canvas).toBeFocused();
    const restored = await settleTimeline(page, () => canvas.press("Home"));
    expect(restored.window.start).toBe(original.window.start);
    expect(restored.window.end).toBe(original.window.end);
    await page.getByRole("button", { name: "平移", exact: true }).click();
    await canvas.scrollIntoViewIfNeeded();
    box = await canvas.boundingBox();
    const moved = await settleTimeline(page, async () => {
        await page.mouse.move(box.x + box.width * 0.6, box.y + 75);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.45, box.y + 75, {
            steps: 8,
        });
        await page.mouse.up();
    });
    expect(Date.parse(moved.window.end) - Date.parse(moved.window.start)).toBe(
        originalDuration,
    );
    expect(moved.window.start).not.toBe(original.window.start);
    await expect(
        page.getByRole("button", { name: "平移", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await settleTimeline(page, () =>
        page.getByRole("button", { name: "恢复整夜", exact: true }).click(),
    );
    await expect(
        page.getByRole("button", { name: "整夜", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
});

test("ordinary wheel scroll is retained, modified wheel zooms, and custom ranges are checked", async ({
    page,
    request,
}) => {
    await openNight(page);
    const canvas = page.locator(".chart-canvas");
    await canvas.scrollIntoViewIfNeeded();
    const original = await (
        await request.get(`/api/v1/nights/${NIGHT}/timeline`)
    ).json();
    const point = await plotPoint(page, original, original.window.start);
    await page.mouse.move(point.x + 100, point.y);
    let requests = 0;
    page.on("request", (req) => {
        if (req.url().includes("/timeline?")) requests += 1;
    });
    await page.mouse.wheel(0, 90);
    await page.evaluate(() => new Promise(requestAnimationFrame));
    expect(requests).toBe(0);
    await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 80);
    const zoomed = await settleTimeline(page, async () => {
        await page.keyboard.down("Control");
        await page.mouse.wheel(0, -100);
        await page.keyboard.up("Control");
    });
    expect(
        Date.parse(zoomed.window.end) - Date.parse(zoomed.window.start),
    ).toBeLessThan(
        Date.parse(original.window.end) - Date.parse(original.window.start),
    );
    await page.getByRole("button", { name: "自定义", exact: true }).click();
    await page
        .locator("#custom-range input[name=from]")
        .fill("2026-11-01T00:00");
    await page.locator("#custom-range input[name=to]").fill("2026-11-05T00:00");
    await page.getByRole("button", { name: "应用范围", exact: true }).click();
    await expect(page.locator("#notice")).toContainText("不超过48小时");
    await page
        .locator("#custom-range input[name=from]")
        .fill("2026-11-08T12:00");
    await page.locator("#custom-range input[name=to]").fill("2026-11-09T14:00");
    const custom = await settleTimeline(page, () =>
        page.getByRole("button", { name: "应用范围", exact: true }).click(),
    );
    expect(custom.window.start).toBe("2026-11-08T04:00:00Z");
    expect(custom.window.end).toBe("2026-11-09T06:00:00Z");
    await expect(page.locator("#notice")).toBeHidden();
    // This 26-hour window legitimately includes one nap on each source day.
    const naps = page.locator(".event").filter({ hasText: "午睡" });
    await expect(naps).toHaveCount(2);
    const nap = naps.filter({ hasText: "11/09" });
    await nap.locator("summary").click();
    await expect(nap).toHaveAttribute("open", "");
    await expect(nap).toContainText("20分");
    await settleTimeline(page, () =>
        nap.getByRole("button", { name: "定位到时间轴" }).click(),
    );
});

test("history selects 7 or 30 days, sorts real values, and opens the selected night", async ({
    page,
}) => {
    await openNight(page);
    await page.getByRole("link", { name: "历史记录", exact: true }).click();
    await expect(page.locator(".history-table tbody tr")).toHaveCount(7);
    await expect(
        page.getByRole("button", { name: "近7日", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "近30日", exact: true }).click();
    await expect(page.locator(".history-table tbody tr")).toHaveCount(30);
    await expect(page).toHaveURL(/days=30/);
    await page.getByRole("button", { name: "平均心率", exact: true }).click();
    await expect(
        page.locator("th").filter({ hasText: "平均心率" }),
    ).toHaveAttribute("aria-sort", "descending");
    await expect(page.locator(".history-table tbody tr").first()).toContainText(
        "2026-11-08",
    );
    await page.getByRole("button", { name: "平均心率", exact: true }).click();
    await expect(page.locator(".history-table tbody tr").first()).toContainText(
        NIGHT,
    );
    await page
        .locator(".history-table tbody tr")
        .first()
        .getByRole("link")
        .click();
    await expect(page).toHaveURL(/view=night&date=2026-11-09/);
    await expect(page.locator(".metric-card")).toHaveCount(4);
});

test("status shows capabilities, storage, and backup boundaries", async ({
    page,
}) => {
    await openNight(page);
    await page.getByRole("link", { name: "数据状态", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "数据状态", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "采集资源", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("睡眠分期区间", { exact: true })).toBeVisible();
    await expect(
        page.getByText("来源尚未提供分期起止区间", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("环境硬件尚未接入", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".status-item").filter({ hasText: "备份" }),
    ).toContainText("未配置");
    await expect(
        page.locator(".status-metric").filter({ hasText: "数据库大小" }),
    ).toContainText("MB");
});

for (const status of [401, 503]) {
    test(`HTTP ${status} displays a recoverable error and never invents metric values`, async ({
        page,
    }) => {
        await page.route("**/api/v1/**", (route) =>
            route.fulfill({
                status,
                contentType: "application/json",
                body: JSON.stringify({ error: "synthetic_error" }),
            }),
        );
        await page.goto(NIGHT_URL);
        await expect(page.locator("#notice")).toContainText(
            status === 401 ? "重新登录" : "暂时无法读取本地数据",
        );
        await expect(page.locator(".metric-card")).toHaveCount(0);
        await page.unroute("**/api/v1/**");
        await page.getByRole("button", { name: "重新读取本地数据" }).click();
        await expect(page.locator(".chart-canvas")).toBeVisible();
        await expect(page.locator("#notice")).toBeHidden();
    });
}

test("an interrupted API connection keeps a clear retry path", async ({
    page,
}) => {
    await page.route("**/api/v1/**", (route) =>
        route.abort("internetdisconnected"),
    );
    await page.goto(NIGHT_URL);
    await expect(page.locator("#notice")).toContainText("暂时无法读取本地数据");
    await expect(page.locator(".metric-card")).toHaveCount(0);
    await page.unroute("**/api/v1/**");
    await page.getByRole("button", { name: "重新读取本地数据" }).click();
    await expect(page.locator(".chart-canvas")).toBeVisible();
});

for (const width of [390, 320]) {
    test(`mobile ${width}px remains usable without page overflow`, async ({
        page,
    }, testInfo) => {
        await page.setViewportSize({ width, height: 844 });
        await openNight(page);
        const overflow = () =>
            page.evaluate(() => ({
                document: document.documentElement.scrollWidth,
                viewport: innerWidth,
            }));
        let sizes = await overflow();
        expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
        await page.getByRole("button", { name: "自定义", exact: true }).click();
        sizes = await overflow();
        expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
        await page.getByRole("button", { name: "整夜", exact: true }).click();
        await expect(page.locator("#custom-range")).toBeHidden();
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({
            path: testInfo.outputPath(`mobile-${width}-synthetic.png`),
            fullPage: true,
        });
        await page.getByRole("link", { name: "历史记录", exact: true }).click();
        await expect(page.locator(".history-table tbody tr")).toHaveCount(7);
        sizes = await overflow();
        expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
        expect(
            await page
                .locator(".table-scroll")
                .evaluate((node) => node.scrollWidth > node.clientWidth),
        ).toBe(true);
        await page.getByRole("link", { name: "数据状态", exact: true }).click();
        await expect(
            page.getByRole("heading", { name: "采集资源", exact: true }),
        ).toBeVisible();
        sizes = await overflow();
        expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
    });
}
