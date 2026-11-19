import { test, expect } from "@playwright/test";

const CARD = "#maintenance-status-panel";

function maintenance(status = "completed", diskState = "normal") {
    return {
        state: "available",
        schedule: { time: "04:00", timezone: "Asia/Shanghai", installation_state: "unknown", next_planned_at: "2026-11-18T04:00:00+08:00" },
        latest: {
            day: "2026-11-17", status, phase: status === "running" ? "backup" : "finished",
            started_at: "2026-11-16T20:00:00Z", finished_at: status === "running" ? null : "2026-11-16T20:02:00Z",
            error_code: status === "failed" ? "backup_failed" : null,
        },
        backups: { verified_count: 3, total_bytes: 15728640, latest_verified_at: "2026-11-16T20:02:00Z", retention_over_budget: false, legacy_count: 1, legacy_bytes: 5242880 },
        disk: { state: diskState, measured_at: "2026-11-17T04:00:00Z", volumes: [
            { role: "system", free_bytes: 10737418240, total_bytes: 21474836480, free_percent: 50, state: diskState },
            { role: "data", free_bytes: 10737418240, total_bytes: 21474836480, free_percent: 50, state: diskState },
        ], bytes: { database: 5242880, wal: 4096, shm: 32768, runtime: 2048 } },
        offsite: { status: "offsite_not_configured", mode: "none", last_success_at: null, error_code: null, location_verified: false },
        error_code: null,
    };
}

test.beforeEach(async ({ page, request }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => { throw new Error(`Browser runtime error: ${error.message}`); });
});

async function routeMaintenance(page, value) {
    await page.route("**/api/v1/status", async (route) => {
        const response = await route.fetch(), payload = await response.json();
        if (value === undefined) delete payload.maintenance;
        else payload.maintenance = typeof value === "function" ? value() : value;
        await route.fulfill({ response, json: payload });
    });
}

async function openStatus(page) {
    await page.goto("/sleep?view=status");
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator(CARD)).toBeVisible();
}

test("maintenance missing metadata and absent run preserve other status cards", async ({ page }) => {
    let value;
    await routeMaintenance(page, () => value);
    await openStatus(page);
    await expect(page.locator(CARD)).toContainText("暂无维护信息");
    await expect(page.getByRole("heading", { name: "采集资源", exact: true })).toBeVisible();
    value = { ...maintenance(), state: "not_run", latest: null };
    await page.locator("#refresh-button").click();
    await expect(page.locator(CARD)).toContainText("尚未运行");
    await expect(page.locator(CARD)).toContainText("每天 04:00 · 上海时间");
    await expect(page.locator(CARD)).toContainText("未核验；计划时间不代表服务器已启用");
    await expect(page.locator(CARD).locator("button")).toHaveCount(0);
});

for (const [status, label] of [
    ["running", "正在维护"], ["completed", "本轮维护已完成"], ["partial", "本轮维护部分完成"],
    ["failed", "本轮维护失败"], ["interrupted", "维护已中断"], ["blocked", "维护暂未执行"],
]) {
    test(`maintenance ${status} preserves last verified backup evidence`, async ({ page }) => {
        await routeMaintenance(page, maintenance(status));
        await openStatus(page);
        const card = page.locator(CARD);
        await expect(card).toContainText(label);
        await expect(card).toContainText("3 份");
        await expect(card).toContainText("15.0 MiB");
        await expect(card).toContainText("1 份，不参与自动清理");
        await expect(card).toContainText("核验记录表示该次检查通过");
        if (status === "running") {
            await expect(card).toContainText("备份与恢复核验");
            await expect(card).not.toContainText("结束时间");
        }
        if (status === "failed") await expect(card).toContainText("创建备份未成功，已有副本保留");
    });
}

test("disk restrictions describe write protection without claiming lost data", async ({ page }) => {
    let value = maintenance();
    await routeMaintenance(page, () => value);
    await openStatus(page);
    for (const [state, label] of [["normal", "空间正常"], ["warning", "空间预警"], ["restricted", "空间不足，限制大任务"], ["critical", "空间临界，暂停新数据写入"], ["unknown", "空间状态未确认"]]) {
        value = maintenance("blocked", state);
        await page.locator("#refresh-button").click();
        await expect(page.locator(CARD)).toContainText(label);
        if (state === "restricted") await expect(page.locator(CARD)).toContainText("正常小批环境接收优先保留");
        if (state === "critical") await expect(page.locator(CARD)).toContainText("环境上传暂不确认入库");
    }
});

test("offsite distinguishes no destination, verified path, SSH and export failure", async ({ page }) => {
    let value = maintenance();
    await routeMaintenance(page, () => value);
    await openStatus(page);
    const card = page.locator(CARD);
    await expect(card).toContainText("尚未配置");
    value = { ...maintenance(), offsite: { status: "verified", mode: "path", location_verified: false, last_success_at: "2026-11-16T20:03:00Z" } };
    await page.locator("#refresh-button").click();
    await expect(card).toContainText("目标副本已核验，离机位置未核验");
    await expect(card).toContainText("复制成功不等于离机保护已建立");
    value.offsite = { status: "verified", mode: "ssh", location_verified: false,
        retained_count: 10, retained_bytes: 104857600, over_budget: true,
        location_state: "remote_host", transit_encrypted: true, at_rest_encryption: "not_verified" };
    await page.locator("#refresh-button").click();
    await expect(card).toContainText("SSH 目标副本已核验");
    await expect(card).toContainText("SSH 传输经过加密");
    await expect(card).toContainText("10 份");
    await expect(card).toContainText("100.0 MiB");
    await expect(card).toContainText("目标保留预算");
    await expect(card).toContainText("最新有效副本仍受保护");
    value.offsite = { status: "failed", mode: "ssh", error_code: "offsite_export_failed" };
    await page.locator("#refresh-button").click();
    await expect(card).toContainText("副本导出或核验失败");
    await expect(card).toContainText("本地备份保留");
});

test("unavailable manifest isolates its card and does not invent verified backups", async ({ page }) => {
    await routeMaintenance(page, { ...maintenance(), state: "unavailable", error_code: "maintenance_state_unavailable" });
    await openStatus(page);
    const card = page.locator(CARD);
    await expect(card).toContainText("维护记录暂不可用");
    await expect(card).toContainText("不会因刷新页面执行修复、删除或恢复");
    await expect(card).not.toContainText("3 份");
    await expect(page.locator("#daily-status-panel")).toBeVisible();
    await expect(page.getByRole("heading", { name: "模型连接与分析状态", exact: true })).toBeVisible();
});

test("maintenance drops injected errors, paths, identifiers and invalid quantities", async ({ page }) => {
    const secret = "<img src=x onerror=alert(1)> /private/backup/account sk-private";
    const value = maintenance();
    Object.assign(value.latest, { status: secret, phase: secret, error_code: secret, started_at: secret, finished_at: secret, backup_id: secret });
    value.backups = { verified_count: secret, total_bytes: -1, latest_verified_at: secret, path: secret, legacy_count: Number.MAX_SAFE_INTEGER };
    value.disk = { state: secret, measured_at: secret, volumes: [{ role: secret, free_bytes: secret, free_percent: 200, path: secret }], bytes: { database: secret } };
    value.offsite = { status: secret, mode: secret, error_code: secret, last_success_at: secret, destination: secret,
        retained_count: secret, retained_bytes: -1, over_budget: secret, location_verified: true };
    value.schedule.installation_state = "installed";
    value.schedule.next_planned_at = secret;
    await routeMaintenance(page, value);
    await openStatus(page);
    const card = page.locator(CARD);
    await expect(card).toContainText("维护状态未确认");
    await expect(card).toContainText("空间状态未确认");
    await expect(card).toContainText("未核验；计划时间不代表服务器已启用");
    await expect(card).not.toContainText("sk-private");
    await expect(card).not.toContainText("/private/");
    await expect(card.locator("img,script,iframe")).toHaveCount(0);
});

test("maintenance refresh is readonly and narrow screen details stay accessible", async ({ page }) => {
    let value = maintenance("failed", "restricted");
    value.backups.retention_over_budget = true;
    const methods = [];
    page.on("request", (request) => { if (request.url().includes("/api/")) methods.push(request.method()); });
    await routeMaintenance(page, () => value);
    await page.addInitScript(() => { Storage.prototype.setItem = () => { throw new Error("Maintenance must not persist health data"); }; });
    for (const width of [320, 390]) {
        await page.setViewportSize({ width, height: 860 });
        await openStatus(page);
        await expect(page.locator(CARD)).toContainText("最新有效副本仍受保护");
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    value = maintenance("completed");
    await page.locator("#refresh-button").click();
    await expect(page.locator(CARD)).toContainText("本轮维护已完成");
    expect(methods.every((method) => method === "GET")).toBe(true);
    expect(await page.evaluate(() => localStorage.length)).toBe(0);
});
