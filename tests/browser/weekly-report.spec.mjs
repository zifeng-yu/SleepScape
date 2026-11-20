import { test, expect } from "@playwright/test";

const END = "2026-11-09";
const START = "2026-11-03";
const url = `/sleep?view=history&date=${END}`;
const window = { start_date: START, end_date: END, visible_n: 7 };

test.beforeEach(async ({ request }) => {
    expect((await (await request.get("/api/v1/status")).json()).demo).toBe(true);
});

test("blocked dates are listed without changing history statistics or starting generation", async ({ page }) => {
    const methods = [];
    await page.route("**/api/v1/sleep-reviews/*", async (route) => {
        methods.push(route.request().method());
        await route.fulfill({ json: {
            window, state: "incomplete_data", report: null,
            incomplete: [
                { date: "2026-11-08", missing: [{ metric: "sleep.night_average_hrv", code: "missing_garmin_core" }] },
                { date: END, missing: [{ metric: "environment.window", code: "missing_environment_minutes" }] },
            ],
        } });
    });
    await page.goto(url);
    const panel = page.locator(".weekly-report");
    await expect(panel.getByText("11 月 8 日数据完整度不够", { exact: true })).toBeVisible();
    await expect(panel.getByText("11 月 9 日数据完整度不够", { exact: true })).toBeVisible();
    await expect(panel.getByText(/环境分钟窗口存在缺口/)).toBeVisible();
    await expect(page.locator(".history-review").getByText("中位数 · 2/7 晚")).toHaveCount(5);
    await expect(page.locator(".history-table tbody tr")).toHaveCount(7);
    await page.getByLabel("按晨起睡感筛选").selectOption("unfilled");
    await expect(panel.getByText("11 月 8 日数据完整度不够", { exact: true })).toBeVisible();
    expect(methods).toEqual(["GET"]);
    await page.getByRole("button", { name: "近30日", exact: true }).click();
    await expect(panel).toHaveCount(0);
    expect(methods).toEqual(["GET"]);
});

test("changing the cutoff isolates a late result and a failed read", async ({ page }) => {
    const previous = "2026-11-08";
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/v1/sleep-reviews/*", async (route) => {
        if (route.request().url().endsWith(END)) {
            await held;
            await route.fulfill({ json: {
                window, state: "incomplete_data", report: null,
                incomplete: [{ date: END, missing: [] }],
            } });
        } else {
            await route.fulfill({ status: 503, json: { detail: "synthetic failure" } });
        }
    });
    const pending = page.waitForRequest((request) => request.url().endsWith(`/sleep-reviews/${END}`));
    await page.goto(url);
    await pending;
    try {
        await page.getByLabel("截止醒来日期").fill(previous);
        const panel = page.locator(".weekly-report");
        const control = panel.locator(":scope > summary");
        await expect(control).toContainText(previous);
        await control.focus();
        await page.keyboard.press("Enter");
        await expect(panel.getByText("总结暂不可读取", { exact: true })).toBeVisible();
        await expect(page.locator(".history-table tbody tr")).toHaveCount(7);
    } finally {
        release();
    }
    await expect(page.getByText("11 月 9 日数据完整度不够", { exact: true })).toHaveCount(0);
});

for (const width of [320, 390]) {
    test(`saved report and its evidence work at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 844 });
        await page.route("**/api/v1/sleep-reviews/*", (route) => route.fulfill({ json: {
            window, state: "complete", incomplete: [], source_changed: true,
            report: {
                generated_at: "2026-11-09T01:01:00Z",
                content: {
                    summary: { text: "这段时间的睡眠记录可以回看。", evidence_ids: ["A1"] },
                    observations: [{ text: "这晚睡感较差。", evidence_ids: ["N1"] }],
                    hypotheses: [], actions: [],
                },
                evidence: {
                    window: { ...window, eligible_n: 7 }, examples: ["N1"],
                    aggregates: [{ id: "A1", metric: "sleep_seconds", median: 21600, valid_n: 7 }],
                    comparisons: [],
                    nights: [{ id: "N1", night_date: END, quality: "with_gaps", feeling: "poor", metrics: { sleep_seconds: 21600 } }],
                    quality: { feeling_counts: { poor: 1, fair: 0, good: 0, unfilled: 6 }, limits: [] },
                },
            },
        } }));
        await page.goto(url);
        const panel = page.locator(".weekly-report");
        await panel.locator(":scope > summary").click();
        await expect(panel.getByText("数据已更新，以下为生成时的结果。")).toBeVisible();
        await expect(panel.getByText(/核验通过 7\/7 晚/)).toBeVisible();
        await panel.getByText("主睡眠时长 · 中位数", { exact: true }).click();
        await expect(panel.getByText("6小时 · 7 晚")).toBeVisible();
        await panel.getByText("11 月 9 日的夜晚证据", { exact: true }).click();
        const link = panel.getByRole("link", { name: `打开 ${END} 夜晚详情` });
        await expect(link).toHaveAttribute("href", `/sleep?view=night&date=${END}`);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({ path: testInfo.outputPath(`weekly-${width}-synthetic.png`), fullPage: true });
        await link.click();
        await expect(page).toHaveURL(/view=night&date=2026-11-09/);
    });
}
