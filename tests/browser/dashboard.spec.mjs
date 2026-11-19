import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";
const NIGHT_URL = `/sleep?view=night&date=${NIGHT}`;
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

function analysisPayload(night = NIGHT, overrides = {}) {
    return {
        night_date: night,
        implementation_stage: "reports",
        data_state: "not_evaluated",
        execution_state: "not_configured",
        model: {
            model: "qwen3.8-max",
            enabled: false,
            configured: false,
            configuration_state: "not_configured",
            connection_state: "not_checked",
            last_check_at: null,
            last_check_status: null,
            last_error_code: null,
            usage: {},
            budget: { used: 0, limit: 30, remaining: 30 },
        },
        readiness: null,
        automatic_enabled: false,
        missing: [],
        blockers: ["model_not_configured"],
        report: null,
        ...overrides,
    };
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

function visibleCanvasY(page, box, offset = 80) {
    // The expanded tracks may be taller than the viewport after scrolling.
    // Place gestures in the visible canvas instead of an offscreen row header.
    const top = Math.max(12, box.y + 1);
    const bottom = Math.min(page.viewportSize().height - 12, box.y + box.height - 1);
    return Math.max(top, Math.min(bottom, box.y + offset));
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
        y: visibleCanvasY(page, box, relativeY),
    };
}

test("night cards preserve values, expose sources, and use icon-only date controls", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1050 });
    await openNight(page);
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.locator("#sleep-analysis-panel")).toContainText("尚无当前配置的已完成请求");
    await expect(page.locator("#sleep-analysis-panel")).not.toContainText("请求状态不可用");
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

test("report panel shows missing configuration without inventing unchecked data gaps or calling models", async ({
    page,
}) => {
    const methods = [];
    page.on("request", (request) => {
        if (request.url().includes("/api/")) methods.push(request.method());
    });
    let reads = 0;
    await page.route("**/api/v1/nights/*/analysis", (route) => {
        reads += 1;
        return route.fulfill({ json: analysisPayload() });
    });
    await openNight(page);
    const panel = page.locator("#sleep-analysis-panel");
    await expect(panel).toHaveAttribute("aria-busy", "false");
    await expect(panel).toContainText("模型未配置");
    await expect(panel).toContainText("尚无当前配置的已完成请求");
    await expect(panel).toContainText("本晚尚无可用的完整性检查");
    await expect(panel.locator(".readiness-missing")).toHaveCount(0);
    await expect(panel.getByRole("button")).toHaveCount(0);
    await page.getByRole("button", { name: "重新读取本地数据" }).click();
    await expect.poll(() => reads).toBe(2);
    await expect(panel).toHaveAttribute("aria-busy", "false");
    expect(methods.every((method) => method === "GET")).toBe(true);
});

test("analysis endpoint failure stays inside its own card and can be refreshed", async ({
    page,
}) => {
    await page.route("**/api/v1/nights/*/analysis", (route) =>
        route.fulfill({ status: 503, json: { detail: "synthetic_error" } }),
    );
    await openNight(page);
    const panel = page.locator("#sleep-analysis-panel");
    await expect(panel).toContainText("暂时无法读取分析状态");
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(page.locator("#notice")).toBeHidden();
    await page.unroute("**/api/v1/nights/*/analysis");
    await page.route("**/api/v1/nights/*/analysis", (route) =>
        route.fulfill({ json: analysisPayload() }),
    );
    await page.getByRole("button", { name: "重新读取本地数据" }).click();
    await expect(panel).toContainText("模型未配置");
});

test("a delayed analysis response cannot replace the selected night's state", async ({
    page,
}) => {
    let releaseFirst;
    const pending = new Promise((resolve) => { releaseFirst = resolve; });
    let firstRequested = false;
    let firstSettled = false;
    await page.route("**/api/v1/nights/*/analysis", async (route) => {
        const night = route.request().url().split("/").at(-2);
        if (night === NIGHT) {
            firstRequested = true;
            await pending;
        }
        await route.fulfill({
            json: analysisPayload(night, {
                execution_state: night === NIGHT ? "disabled" : "queued",
            }),
        }).catch(() => {}); // The old navigation's request is deliberately aborted.
        if (night === NIGHT) firstSettled = true;
    });
    try {
        await openNight(page);
        await expect.poll(() => firstRequested).toBe(true);
        await page.getByRole("button", { name: "上一晚", exact: true }).click();
        await expect(page).toHaveURL(/date=2026-11-08/);
        const panel = page.locator("#sleep-analysis-panel");
        await expect(panel.locator(".report-badge")).toHaveText("等待生成");
        releaseFirst();
        await expect.poll(() => firstSettled).toBe(true);
        await expect(panel.locator(".report-badge")).toHaveText("等待生成");
        await expect(page.locator(".chart-canvas")).toBeVisible();
    } finally {
        releaseFirst();
    }
});

test("model status renders bounded public evidence while rejecting secrets and HTML", async ({
    page,
}) => {
    const marker = "SYNTHETIC_MODEL_SECRET_MUST_NOT_RENDER";
    const unsafe = `<img src=x onerror="window.modelInjection=true">${marker}`;
    const model = {
        ...analysisPayload().model,
        configured: true,
        enabled: true,
        configuration_state: "configured",
        connection_state: "failed",
        last_check_status: "failed",
        last_check_at: "2026-11-09T01:00:00Z",
        last_error_code: "output_schema_required",
        usage: { prompt_tokens: 11, completion_tokens: 7, total_tokens: 18 },
        budget: { used: 1, limit: 30, remaining: 29 },
        api_key: unsafe,
        base_url: `https://${marker}.invalid`,
        raw_error: unsafe,
    };
    await page.route("**/api/v1/status", async (route) => {
        const response = await route.fetch();
        await route.fulfill({ json: { ...await response.json(), model } });
    });
    await page.route("**/api/v1/nights/*/analysis", (route) =>
        route.fulfill({
            json: analysisPayload(NIGHT, {
                execution_state: "queued",
                model,
                notice: unsafe,
                missing: [{ source: "garmin", metric: unsafe, date: unsafe }],
            }),
        }),
    );
    await openNight(page);
    await expect(page.locator("#sleep-analysis-panel")).toContainText("响应缺少必需字段");
    await expect(page.locator("#sleep-analysis-panel .analysis-missing")).toHaveCount(0);
    await page.getByRole("link", { name: "数据状态", exact: true }).click();
    const panel = page.locator("#model-status-panel");
    await expect(panel).toContainText("最近请求未成功");
    await expect(panel).toContainText("响应缺少必需字段");
    await expect(panel.locator("dt").filter({ hasText: /^该次请求结果$/ }).locator("xpath=following-sibling::dd[1]")).toHaveText("响应缺少必需字段");
    await expect(panel).toContainText("报告是否已生成请查看对应夜晚详情");
    await expect(panel).not.toContainText("连接核验失败");
    await expect(panel).toContainText("输入 Token");
    await expect(panel).toContainText("18");
    await expect(panel).toContainText("1 / 30");
    await expect(panel).toContainText("今日模型请求次数");
    await expect(panel).toContainText("最近请求时间（共享）");
    await expect(panel).not.toContainText("最近核验");
    await expect(panel).toContainText("费用");
    await expect(panel).toContainText("未知");
    await expect(page.locator("body")).not.toContainText(marker);
    await expect(panel.locator("img, script, iframe")).toHaveCount(0);
    expect(await page.evaluate(() => window.modelInjection)).toBeUndefined();
});

for (const scenario of ["mismatched night", "unexpected report"]) {
    test(`report panel rejects ${scenario}`, async ({ page }) => {
        await page.route("**/api/v1/nights/*/analysis", (route) =>
            route.fulfill({
                json: analysisPayload(scenario === "mismatched night" ? "2026-11-08" : NIGHT, {
                    execution_state: "queued",
                    report: scenario === "unexpected report"
                        ? { conclusion: "SYNTHETIC_REPORT_MUST_NOT_RENDER" }
                        : null,
                }),
            }),
        );
        await openNight(page);
        await expect(page.locator("#sleep-analysis-panel")).toContainText("暂时无法读取分析状态");
        await expect(page.locator("body")).not.toContainText("SYNTHETIC_REPORT_MUST_NOT_RENDER");
        await expect(page.locator(".chart-canvas")).toBeVisible();
    });
}

test("history exposes analysis state and handles absent or unknown summaries", async ({ page }) => {
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.items = body.items.map((item, index) => ({
            ...item,
            analysis_summary: index === 0 ? undefined : {
                state: "not_evaluated",
                label: "SYNTHETIC_UNTRUSTED_LABEL_MUST_NOT_RENDER",
            },
        }));
        await route.fulfill({ json: body });
    });
    await page.goto(`/sleep?view=history&date=${NIGHT}`);
    await expect(page.getByRole("columnheader", { name: "完整性检查", exact: true })).toBeVisible();
    await expect(page.locator(".analysis-history-state")).toHaveCount(7);
    await expect(
        page.locator(".analysis-history-state").filter({ hasText: "暂无分析状态" }),
    ).toHaveCount(1);
    await expect(
        page.locator(".analysis-history-state").filter({ hasText: "尚未检查" }),
    ).toHaveCount(6);
    await expect(page.locator("body")).not.toContainText(
        "SYNTHETIC_UNTRUSTED_LABEL_MUST_NOT_RENDER",
    );
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
    for (const metric of ["heart_rate", "hrv", "spo2", "body_battery"])
        await expect(tooltip.locator(`.tooltip-row[data-metric="${metric}"]`)).toHaveCount(1);
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
        await page.mouse.move(box.x + box.width * 0.35, visibleCanvasY(page, box, 75));
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.7, visibleCanvasY(page, box, 150), {
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
        await page.mouse.move(box.x + box.width * 0.6, visibleCanvasY(page, box, 75));
        await page.mouse.down();
        await page.mouse.move(box.x + box.width * 0.45, visibleCanvasY(page, box, 75), {
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
    await page.mouse.move(box.x + box.width / 2, visibleCanvasY(page, box));
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
    await expect(page.locator("#environment-status-panel")).toBeVisible();
    await expect(page.getByText("环境看板尚未接入", { exact: true })).toHaveCount(0);
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

test("sleep entry keeps its path while switching views", async ({ page }) => {
    await page.goto(`/sleep?view=night&date=${NIGHT}`);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await page.locator('nav a[data-view="history"]').click();
    await expect(page).toHaveURL(/\/sleep\?.*view=history/);
    await page.reload();
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await page.locator('nav a[data-view="night"]').click();
    await expect(page).toHaveURL(/\/sleep\?.*view=night/);
});
