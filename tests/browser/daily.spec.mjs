import { test, expect } from "@playwright/test";

const STATUS_URL = "/sleep?view=status";
const CARD = "#daily-status-panel";

function daily(status = "completed", sleep = "available") {
    return {
        state: "available",
        lookback_days: 7,
        schedule: {
            timezone: "Asia/Shanghai",
            time: "09:00",
            installation_state: "unknown",
            next_planned_at: "2026-11-18T09:00:00+08:00",
            next_run_kind: "planned_not_installed",
        },
        analysis: "automatic_disabled",
        latest: {
            day: "2026-11-17", status, phase: status === "running" ? "collecting" : "finished",
            attempt: 1,
            started_at: "2026-11-17T01:00:00+00:00",
            finished_at: status === "running" ? null : "2026-11-17T01:02:00+00:00",
            retry_not_before: status === "deferred" ? "2026-11-17T02:00:00+00:00" : null,
            window: { start_date: "2026-11-11", end_date: "2026-11-17" },
            request_counts: { planned: 80, completed: 79, failed: 1, http: 82 },
            error_code: status === "failed" ? "network_retry_exhausted" : null,
            analysis: "skipped_deferred",
        },
        observations: [{ date: "2026-11-17", sleep }],
        history: [],
    };
}

test.beforeEach(async ({ request, page }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => {
        throw new Error(`Browser runtime error: ${error.message}`);
    });
});

async function routeDaily(page, value) {
    await page.route("**/api/v1/status", async (route) => {
        const response = await route.fetch();
        const payload = await response.json();
        if (value === undefined) delete payload.daily;
        else payload.daily = typeof value === "function" ? value() : value;
        await route.fulfill({ response, json: payload });
    });
}

async function openStatus(page) {
    await page.goto(STATUS_URL);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator(CARD)).toBeVisible();
}

test("older status payload without daily metadata degrades only the daily card", async ({ page }) => {
    await routeDaily(page, undefined);
    await openStatus(page);
    await expect(page.locator(CARD)).toContainText("暂无每日任务信息");
    await expect(page.getByRole("heading", { name: "采集资源", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "模型连接与分析状态", exact: true })).toBeVisible();
});

test("no daily runs shows configured schedule without claiming the timer is installed", async ({ page }) => {
    await routeDaily(page, { ...daily(), state: "not_run", latest: null, observations: [] });
    await openStatus(page);
    const card = page.locator(CARD);
    await expect(card).toContainText("尚未运行");
    await expect(card).toContainText("每天 09:00 · 上海时间");
    await expect(card).toContainText("含任务当天的最近 7 天");
    await expect(card).toContainText("未核验；计划时间不代表服务器已启用");
    await expect(card).toContainText("自动分析未启用：采集与完整性检查继续运行");
    await expect(card.locator("button")).toHaveCount(0);
});

for (const [status, label, sleep, expectedObservation] of [
    ["running", "正在运行", "not_collected", "尚未采集"],
    ["completed", "本轮请求已完成", "missing", "缺少睡眠记录，等待下次窗口内补取"],
    ["partial", "部分请求未完成", "request_failed", "请求失败，睡眠记录待核实"],
    ["failed", "本轮运行失败", "request_failed", "请求失败，睡眠记录待核实"],
    ["deferred", "等待服务允许重试", "missing", "缺少睡眠记录，等待下次窗口内补取"],
    ["interrupted", "运行已中断", "invalid", "睡眠记录无效，等待补取"],
]) {
    test(`daily ${status} is separate from sleep observation availability`, async ({ page }) => {
        await routeDaily(page, daily(status, sleep));
        await openStatus(page);
        const card = page.locator(CARD);
        await expect(card).toContainText(label);
        await expect(card).toContainText(expectedObservation);
        await expect(card).toContainText("请求完成不代表手表已同步");
        await expect(card).toContainText("每日流程先采集，再检查双源数据");
        await expect(card).toContainText("HTTP 请求次数（含重试）");
        if (status === "running") {
            await expect(card).toContainText("采集 Garmin 数据");
            await expect(card).not.toContainText("结束时间");
        }
        if (status === "failed") await expect(card).toContainText("网络重试未成功");
        if (status === "deferred") {
            await expect(card).toContainText("允许重试时间");
            await expect(card).toContainText("10:00:00");
        }
    });
}

test("unavailable ledger leaves stored observations and other cards usable", async ({ page }) => {
    await routeDaily(page, { ...daily(), state: "unavailable", latest: null });
    await openStatus(page);
    await expect(page.locator(CARD)).toContainText("日任务记录暂不可用");
    await expect(page.locator(CARD)).toContainText("不会据此启动采集");
    await expect(page.getByRole("heading", { name: "采集资源", exact: true })).toBeVisible();
    await expect(page.locator(CARD)).not.toContainText("已有睡眠记录");
});

test("daily metadata renders fixed safe labels and rejects private or injected fields", async ({ page }) => {
    const malicious = "<img src=x onerror=alert(1)> /private/account sk-secret";
    const value = daily();
    Object.assign(value.latest, {
        status: malicious, phase: malicious, error_code: malicious, run_id: malicious,
        started_at: malicious, finished_at: malicious, retry_not_before: malicious,
        request_counts: { planned: -1, completed: malicious, failed: Number.MAX_SAFE_INTEGER, http: 0 },
    });
    value.observations = [{ date: "2026-11-17", sleep: malicious }, { date: malicious, sleep: "available" }];
    value.schedule.installation_state = "installed";
    value.schedule.next_planned_at = malicious;
    await routeDaily(page, value);
    await page.addInitScript(() => {
        Storage.prototype.setItem = () => { throw new Error("Status must not persist health data"); };
    });
    await openStatus(page);
    const card = page.locator(CARD);
    await expect(card).toContainText("任务状态未确认");
    await expect(card).toContainText("睡眠记录状态未确认");
    await expect(card).toContainText("未核验；计划时间不代表服务器已启用");
    await expect(card).not.toContainText("sk-secret");
    await expect(card).not.toContainText("/private/");
    await expect(card.locator("img,script,iframe")).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test("status refresh reads newly available sleep data without sending mutation requests", async ({ page }) => {
    let current = daily("completed", "missing");
    const methods = [];
    page.on("request", (request) => { if (request.url().includes("/api/")) methods.push(request.method()); });
    await routeDaily(page, () => current);
    await openStatus(page);
    await expect(page.locator(CARD)).toContainText("缺少睡眠记录");
    current = daily("completed", "available");
    await page.locator("#refresh-button").click();
    await expect(page.locator(CARD)).toContainText("已有睡眠记录");
    await expect(page.locator(CARD)).not.toContainText("缺少睡眠记录");
    expect(methods.length).toBeGreaterThan(1);
    expect(methods.every((method) => method === "GET")).toBe(true);
});

test("daily details remain readable on narrow screens", async ({ page }) => {
    const value = daily("partial", "request_failed");
    value.lookback_days = 31;
    await routeDaily(page, value);
    for (const width of [320, 390]) {
        await page.setViewportSize({ width, height: 860 });
        await openStatus(page);
        await expect(page.locator(CARD)).toContainText("最近 31 天");
        await expect(page.locator(CARD)).toContainText("请求失败，睡眠记录待核实");
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
});

for (const [state, expected] of [["automatic_enabled", "自动分析已启用"], ["automatic_disabled", "自动分析未启用"], ["private-token-text", "自动分析配置暂不可确认"]]) {
    test(`daily report configuration ${state} is distinct from collection completion`, async ({ page }) => {
        await routeDaily(page, { ...daily(), analysis: state });
        await openStatus(page);
        await expect(page.locator(CARD)).toContainText(expected);
        await expect(page.locator(CARD)).toContainText("采集完成不代表报告已生成");
        await expect(page.locator(CARD)).toContainText("当时报告生成尚未启用，不代表当前配置");
        await expect(page.locator(CARD)).not.toContainText("private-token-text");
    });
}
