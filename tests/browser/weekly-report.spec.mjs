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

test("a rechecked window displays readiness while waiting for its first report", async ({ page }) => {
    await page.route("**/api/v1/sleep-reviews/*", (route) => route.fulfill({ json: {
        window, state: "ready", incomplete: [], report: null,
    } }));
    await page.goto(url);
    const panel = page.locator(".weekly-report");
    const summary = panel.locator(":scope > summary");
    await expect(summary).toContainText("完整度已通过");
    await summary.click();
    await expect(panel.getByText("窗口内各晚均已通过完整性检查，总结尚未生成。")).toBeVisible();
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
        await expect(panel.getByText("复盘依据已更新，以下为生成时的结果。")).toBeVisible();
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

test("paired evidence opens both the example and the counterexample with distinct night count", async ({ page }) => {
    const text = "对照不同夜晚，醒来时间与晨起睡感的关系并不一致。";
    const nights = [7, 8, 9].map((day, index) => ({
        night_date: `2026-11-${String(day).padStart(2, "0")}`, id: `N${index + 1}`,
        feeling: index === 1 ? "poor" : "fair", metrics: { sleep_seconds: 21600 },
    }));
    await page.route("**/api/v1/sleep-reviews/*", (route) => route.fulfill({ json: {
        window, source_changed: false, report: {
            generated_at: "2026-11-09T01:00:00Z",
            evidence: { window: { ...window, eligible_n: 7 }, quality: { feeling_counts: null, limits: [] },
                nights, aggregates: [], comparisons: [], pair_checks: [{
                    id: "P3", metric: "sleep_end_offset_seconds", pair_n: 2, night_n: 3,
                    example: ["N1","N2"], counterexample: ["N3","N2"], max_duration_gap_seconds: 0, text,
                }] },
            content: { summary: { text, evidence_ids: ["P3"] }, observations: [], hypotheses: [], actions: [] },
        },
    } }));
    await page.goto(url);
    const panel = page.locator(".weekly-report");
    await panel.locator(":scope > summary").click();
    await panel.getByText("醒来与睡感 · 2 对，涉及 3 晚", { exact: true }).click();
    await expect(panel.getByText("反例", { exact: true })).toBeVisible();
    await panel.getByText("11 月 9 日的夜晚证据", { exact: true }).click();
    await expect(panel.getByRole("link", { name: "打开 2026-11-09 夜晚详情" })).toBeVisible();
});

test("new weekly interpretation shows the whole window and shows the model comparison", async ({page}) => {
    await page.route("**/api/v1/sleep-reviews/*", r => r.fulfill({json:{window, state:"complete",report:{
        generated_at:"2026-11-09T01:01:00Z",evidence:{window,target_date:END,nights:Array(7).fill({})},
        content:{summary:{text:"本周睡眠时长有波动。"},garmin:"手表记录。",environment:"卧室较暖。",
            feeling:"起床时自评一般。",comparison:"前半周与后半周的起床时间有差异。",possible_reasons:[]},
    }}}));
    await page.goto(url);
    const panel=page.locator(".weekly-report");
    await panel.locator(":scope > summary").click();
    await expect(panel.getByText("前半周与后半周的起床时间有差异。",{exact:true})).toBeVisible();
    await expect(panel.getByText("起床时自评一般。",{exact:true})).toBeVisible();
    await expect(panel.getByText(/复盘窗口 2026-11-03 至 2026-11-09 · 共 7 晚/)).toBeVisible();
});
