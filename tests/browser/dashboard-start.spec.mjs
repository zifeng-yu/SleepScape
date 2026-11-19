import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";

test.beforeEach(async ({ request, page }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => {
        throw new Error(`Browser runtime error: ${error.message}`);
    });
});

// Keep the common disposable fixture unrestricted for existing tests. Only this
// browser receives the public display policy; real service bounds have separate
// backend regressions. All returned observations still come from synthetic data.
async function displayFrom(page, start, { latest = NIGHT } = {}) {
    await page.route("**/api/v1/status", async (route) => {
        const response = await route.fetch();
        const payload = await response.json();
        expect(payload.demo).toBe(true);
        payload.dashboard_start_date = start;
        payload.latest_night = latest;
        payload.earliest_night = latest;
        await route.fulfill({ response, json: payload });
    });
}

test("display start clamps old URLs, typed dates and back navigation", async ({ page }) => {
    await displayFrom(page, NIGHT);
    const nightRequests = [];
    page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (path.startsWith("/api/v1/nights/")) nightRequests.push(path);
    });
    await page.goto("/sleep?view=night&date=2026-11-08");
    const picker = page.getByLabel("选择醒来日期");
    await expect(picker).toHaveValue(NIGHT);
    await expect(picker).toHaveAttribute("min", NIGHT);
    await expect(page).toHaveURL(/date=2026-11-09/);
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeDisabled();

    await picker.fill("2026-11-08");
    await expect(picker).toHaveValue(NIGHT);
    await page.getByRole("button", { name: "下一晚", exact: true }).click();
    await expect(picker).toHaveValue("2026-11-10");
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeEnabled();
    await page.goBack();
    await expect(picker).toHaveValue(NIGHT);
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeDisabled();
    await page.reload();
    await expect(picker).toHaveValue(NIGHT);
    expect(nightRequests.some((path) => path.includes("2026-11-08"))).toBe(false);
});

test("seven and thirty day histories do not request or show dates before display start", async ({ page, request }) => {
    await displayFrom(page, NIGHT);
    const ranges = [];
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.pathname === "/api/v1/nights") ranges.push(url.searchParams.get("start"));
    });
    await page.goto("/sleep?view=history&date=2026-11-08");
    await expect(page.locator(".history-date")).toHaveCount(1);
    await expect(page.locator(".history-date")).toHaveText(NIGHT);
    await expect(page.locator(".history-period")).toContainText(`${NIGHT} — ${NIGHT}`);
    await page.getByRole("button", { name: "近30日", exact: true }).click();
    await expect(page.locator(".history-date")).toHaveCount(1);
    await expect(page.locator(".history-period")).toContainText(`${NIGHT} — ${NIGHT}`);
    await expect.poll(() => ranges.length).toBe(2);
    expect(ranges).toEqual([NIGHT, NIGHT]);
    // The presentation policy does not remove the underlying source observations.
    expect((await request.get("/api/v1/nights/2026-11-08")).ok()).toBe(true);
});

test("a future display start waits for the first night instead of falling back to old sleep", async ({ page }) => {
    const start = "2026-11-18";
    await page.clock.setFixedTime(new Date("2026-11-17T14:00:00Z"));
    await displayFrom(page, start, { latest: null });
    await page.goto("/sleep");
    await expect(page.getByLabel("选择醒来日期")).toHaveValue(start);
    await expect(page.locator(".date-caption")).toContainText(`从${start}起展示`);
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeDisabled();
    await expect(page.getByText("等待采集首晚睡眠数据", { exact: true })).toBeVisible();
    await expect(page.locator(".metric-card")).toHaveCount(0);
    await expect(page.locator("#chart-shell")).toHaveAttribute("aria-busy", "false");
    await expect(page.locator("#notice")).not.toHaveClass(/error/);
});

test("the first visible night retains its prior-evening environment and custom window", async ({ page }) => {
    await displayFrom(page, NIGHT);
    const initial = page.waitForResponse((response) => response.url().includes(`/nights/${NIGHT}/timeline?`) && response.status() === 200);
    await page.goto(`/sleep?view=night&date=${NIGHT}`);
    const payload = await (await initial).json();
    const boundary = Date.parse(`${NIGHT}T00:00:00+08:00`);
    const points = payload.series.filter((series) => series.source === "environment").flatMap((series) => series.points);
    expect(points.some((point) => Date.parse(point.window_start) < boundary)).toBe(true);
    await expect(page.locator(".chart-canvas")).toBeVisible();

    await page.getByRole("button", { name: "自定义", exact: true }).click();
    await page.locator('#custom-range input[name="from"]').fill("2026-11-08T23:10");
    await page.locator('#custom-range input[name="to"]').fill("2026-11-09T00:10");
    const selected = page.waitForResponse((response) => response.url().includes(`/nights/${NIGHT}/timeline?`) && response.status() === 200);
    await page.getByRole("button", { name: "应用范围" }).click();
    const custom = await (await selected).json();
    expect(custom.environment.record_count).toBeGreaterThan(0);
    expect(Date.parse(custom.window.start)).toBeLessThan(boundary);
    await expect(page.getByLabel("选择醒来日期")).toHaveValue(NIGHT);
});

test("the first night can show environment while waiting for Garmin sleep", async ({ page }) => {
    const start = "2026-11-07";
    await page.clock.setFixedTime(new Date("2026-11-06T14:00:00Z"));
    await displayFrom(page, start, { latest: null });
    await page.goto("/sleep");
    await expect(page.getByLabel("选择醒来日期")).toHaveValue(start);
    await expect(page.locator(".first-night-waiting")).toHaveText("环境数据已开始记录，等待采集首晚睡眠数据。");
    await expect(page.locator("#environment-panel")).toHaveAttribute("data-state", "available");
    await expect(page.locator(".chart-canvas")).toBeVisible();
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeDisabled();
});

test("recent environment shortcut keeps its time window while clamping the night date", async ({ page }) => {
    const start = "2026-11-11";
    await displayFrom(page, start, { latest: null });
    await page.goto("/sleep?view=status");
    await expect(page.getByRole("button", { name: "查看最近采集窗口" })).toBeVisible();
    const response = page.waitForResponse((response) => response.url().includes(`/nights/${start}/timeline?`) && response.status() === 200);
    await page.getByRole("button", { name: "查看最近采集窗口" }).click();
    const payload = await (await response).json();
    await expect(page.getByLabel("选择醒来日期")).toHaveValue(start);
    await expect(page.getByRole("button", { name: "自定义", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect(payload.environment.record_count).toBeGreaterThan(0);
    expect(Date.parse(payload.window.start)).toBeLessThan(Date.parse(`${start}T00:00:00+08:00`));
});

test("an unset display start keeps previous navigation behavior", async ({ page }) => {
    await displayFrom(page, null);
    await page.goto(`/sleep?view=night&date=${NIGHT}`);
    await expect(page.getByLabel("选择醒来日期")).not.toHaveAttribute("min", /.+/);
    await expect(page.getByRole("button", { name: "上一晚", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "上一晚", exact: true }).click();
    await expect(page.getByLabel("选择醒来日期")).toHaveValue("2026-11-08");
    await expect(page.locator(".metric-card")).toHaveCount(4);
});
