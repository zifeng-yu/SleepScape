import { test, expect } from "@playwright/test";

const HISTORY = "/sleep?view=history&date=2026-11-09";

test.beforeEach(async ({ page, request }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBe(true);
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => {
        throw new Error(`Browser runtime error: ${error.message}`);
    });
});

test("review uses one bounded request and reflects a rating-only date and its edit", async ({
    page,
    request,
}) => {
    const day = "2026-07-20";
    const origin = "http://127.0.0.1:8766";
    const path = `/api/v1/nights/${day}`;
    expect((await request.get(path)).status()).toBe(404);
    const saved = await request.put(`${path}/morning-feeling`, {
        headers: { origin },
        data: { feeling: "fair" },
    });
    expect(saved.ok()).toBe(true);

    const queries = [];
    page.on("request", (entry) => {
        const url = new URL(entry.url());
        if (url.pathname.startsWith("/api/v1/nights")) queries.push(url);
    });
    const listing = page.waitForResponse((response) =>
        new URL(response.url()).pathname === "/api/v1/nights",
    );
    await page.goto(HISTORY);
    const summary = page.locator(".history-review");
    const rows = page.locator(".history-table tbody tr");
    await expect(summary.getByRole("heading", { name: "7 日睡眠复盘" })).toBeVisible();
    await expect(summary.getByText("前日 23:10")).toBeVisible();
    await expect(summary.getByText("56.5 次/分")).toBeVisible();
    await expect(summary.getByText("中位数 · 2/7 晚")).toHaveCount(5);
    await expect(rows).toHaveCount(7);
    expect((await listing).headers()["cache-control"]).toContain("no-store");
    expect(queries.map((url) => url.pathname)).toEqual(["/api/v1/nights"]);
    expect(queries[0].searchParams.get("include_review")).toBe("true");

    const cutoff = page.getByLabel("截止醒来日期");
    await cutoff.fill(day);
    await expect(page).toHaveURL(new RegExp(`view=history&date=${day}`));
    await expect(summary.getByText("一般 1晚")).toBeVisible();
    await expect(summary.getByText("0/7 晚")).toHaveCount(5);
    const filter = page.getByLabel("按晨起睡感筛选");
    await filter.focus();
    await filter.selectOption("fair");
    await expect(filter).toBeFocused();
    await expect(rows).toHaveCount(1);
    await expect(rows.first().locator(".history-feeling")).toHaveText("一般");
    await expect(summary.getByText("未填写 6晚")).toBeVisible();
    await rows.first().getByRole("link", { name: day }).click();
    await expect(page).toHaveURL(new RegExp(`view=night&date=${day}`));
    const choice = page.getByRole("group", { name: "晨起睡感" })
        .getByRole("button", { name: "较差" });
    await choice.click();
    await expect(choice).toHaveAttribute("aria-pressed", "true");
    await page.goBack();
    await expect(filter).toHaveValue("fair");
    await expect(page.getByRole("heading", { name: "没有匹配的夜晚" })).toBeVisible();
    await expect(summary.getByText("较差 1晚")).toBeVisible();
    await filter.selectOption("poor");
    await expect(rows.first().locator(".history-feeling")).toHaveText("较差");

    await page.getByRole("button", { name: "近30日", exact: true }).click();
    await expect(summary).toHaveCount(0);
    await expect(filter).toHaveValue("poor");
    await expect(page.getByText("显示 1 / 30 个日历日")).toBeVisible();
    await page.reload();
    await expect(filter).toHaveValue("all");
    await expect(rows).toHaveCount(30);
});

test("missing rating access keeps the historical table and metric medians", async ({ page }) => {
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.review.feeling_state = "unavailable";
        body.review.feeling_counts = null;
        body.items.forEach((item) => { item.morning_feeling = null; });
        await route.fulfill({ response, json: body });
    });
    await page.goto(HISTORY);
    await expect(page.getByLabel("按晨起睡感筛选")).toBeDisabled();
    await expect(page.locator(".history-review-note")).toHaveText("晨起睡感暂不可读取");
    await expect(page.getByText("56.5 次/分", { exact: true })).toBeVisible();
    await expect(page.locator(".history-table tbody tr")).toHaveCount(7);
    await expect(page.locator(".history-feeling").first()).toHaveText("—");
});

test("returning to the previous date ignores a late history response", async ({ page }) => {
    let started, release;
    const pending = new Promise((resolve) => { started = resolve; });
    const delayed = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/v1/nights?*", async (route) => {
        if (new URL(route.request().url()).searchParams.get("end") !== "2026-11-10") {
            await route.continue();
            return;
        }
        const response = await route.fetch();
        started();
        await delayed;
        await route.fulfill({ response }).catch(() => {});
    });
    await page.goto(HISTORY);
    const cutoff = page.getByLabel("截止醒来日期");
    try {
        await cutoff.fill("2026-11-10");
        await pending;
        await page.goBack();
        await expect(cutoff).toHaveValue("2026-11-09");
    } finally {
        release();
    }
    await expect(page.locator(".history-period")).toContainText("— 2026-11-09");
    await expect(page.locator(".history-date").first()).toHaveText("2026-11-09");
});

for (const width of [320, 390]) {
    test(`history review controls and overview fit ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 844 });
        await page.goto(HISTORY);
        const summary = page.locator(".history-review");
        const cutoff = page.getByLabel("截止醒来日期");
        const filter = page.getByLabel("按晨起睡感筛选");
        await expect(cutoff).toBeVisible();
        await expect(filter).toBeVisible();
        await expect(summary.locator(".history-review-metric")).toHaveCount(5);
        await filter.selectOption("unfilled");
        await expect(summary.getByText("中位数 · 2/7 晚")).toHaveCount(5);
        await filter.selectOption("all");
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        const table = page.locator(".table-scroll");
        expect(await table.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
        await table.evaluate((node) => {
            window.scrollTo(0, window.scrollY + node.getBoundingClientRect().top - 80);
        });
        await expect(page.locator(".history-feeling").first()).toBeInViewport({ ratio: 1 });
        expect(await table.evaluate((node) => node.scrollLeft)).toBe(0);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({
            path: testInfo.outputPath(`history-${width}-synthetic.png`),
            fullPage: true,
        });
    });
}
