import { test, expect } from "@playwright/test";
import fs from "node:fs";

const NIGHT = "2026-11-09";
const URL = `/sleep?view=night&date=${NIGHT}`;
const PANEL = "#sleep-analysis-panel";
// Canonical example is authored synthetic evidence, never copied from the private database.
const example = JSON.parse(fs.readFileSync(new globalThis.URL("../fixtures/published-sleep-report.json", import.meta.url), "utf8"));
const clone = (value) => JSON.parse(JSON.stringify(value));

function payload() {
    const input = JSON.parse(JSON.stringify(example.input).replaceAll("2001-01-02", NIGHT).replaceAll("2001-01-01", "2026-11-08"));
    const content = { ...clone(example.output), night_date: NIGHT };
    delete content.input_hash;
    return {
        night_date: NIGHT, implementation_stage: "reports", data_state: "ready", execution_state: "complete",
        missing: [], blockers: [], attempt: null, readiness_unavailable: false,
        model: { model: "qwen3.8-max", enabled: true, configured: true, configuration_state: "configured", connection_state: "verified", budget: { used: 1, limit: 30, remaining: 29 } },
        readiness: {
            policy_version: "dual-source-coverage-2", policy: { min_channel_coverage: 0.99, max_affected_span_seconds: 300 },
            night_date: NIGHT, checked_at: "2026-11-09T01:10:00Z", data_state: "ready", core_ready: true,
            quality_state: "with_gaps", window: { start: input.sleep_window.start_at, end: input.sleep_window.end_at, duration_seconds: 25200 },
            missing: [], warnings: input.completeness.warnings.map((row) => ({ ...row, date: NIGHT, last_checked_at: "2026-11-09T01:10:00Z" })),
            optional_missing: [], limitations: [], environment_summary: { channels: input.completeness.channels }, garmin_summary: {},
        },
        report: {
            night_date: NIGHT, generated_at: "2026-11-09T01:05:00Z", model_ref: "sleep_analyst", prompt_version: "sleep-analysis-1", input_quality_state: "with_gaps",
            source_changed: false, source_changed_reasons: [], content,
            evidence: { facts: input.facts, timeline: input.timeline, events: input.events }, required_limitations: input.required_limitations,
        },
    };
}

test.beforeEach(async ({ request, page }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBe(true);
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => { throw error; });
});

async function route(page, value) {
    await page.route("**/api/v1/nights/*/analysis", (r) => r.fulfill({ json: typeof value === "function" ? value() : value }));
}
async function open(page) {
    await page.goto(URL);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator(PANEL)).toHaveAttribute("aria-busy", "false");
    return page.locator(PANEL);
}

for (const model of ["qwen3.8-max", "qwen4-max", "<img src=x onerror=alert(1)>SYNTHETIC_PRIVATE_MODEL"]) {
    test(`model identity ${model.startsWith("qwen") ? model : "untrusted"} is displayed through the public allowlist`, async ({ page }) => {
        const value = payload(); value.model.model = model;
        await route(page, value);
        const panel = await open(page);
        await panel.getByText("模型配置与请求记录", { exact: true }).click();
        await expect(panel).toContainText(model.startsWith("qwen") ? model : "暂无模型信息");
        await expect(panel).toContainText("1 / 30");
        await expect(panel).not.toContainText("SYNTHETIC_PRIVATE_MODEL");
        await expect(panel.locator("img,script,iframe")).toHaveCount(0);
    });
}

test("published report exposes four observation classes, retained gaps and clickable generation-time evidence", async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const value = payload(), methods = [];
    page.on("request", (request) => { if (request.url().includes("/api/")) methods.push(request.method()); });
    await route(page, value);
    const panel = await open(page), report = panel.locator(".sleep-report");
    await expect(report.getByRole("heading", { name: "本晚睡眠解读" })).toBeVisible();
    await expect(report.locator(".report-badge")).toHaveText("报告已生成");
    await expect(report.locator(".report-observation-group")).toHaveCount(4);
    await expect(report.locator(".analysis-heading + .report-summary")).toBeVisible();
    await expect(report.locator(".report-meta")).toContainText("生成时数据有允许范围内的缺口");
    await expect(page.locator(".cards + #sleep-analysis-panel + .sleep-card + .secondary-metrics")).toBeVisible();
    await expect(report.locator(".report-limitations ul")).toBeHidden();
    await report.locator(".report-limitations > summary").press("Enter");
    await expect(report.locator(".report-limitations ul")).toBeVisible();
    await expect(report.locator(".report-limitations")).toContainText("缺采不代表安静");
    await expect(panel.locator(".readiness-badge")).toHaveText("可分析，有数据缺口");
    await expect(report.locator(".report-hypotheses")).toContainText("本次没有提出额外假设");
    await expect(report.locator(".report-evidence")).not.toHaveAttribute("open", "");
    await report.getByRole("button", { name: "查看证据 F010", exact: true }).first().click();
    await expect(report.locator(".report-evidence")).toHaveAttribute("open", "");
    const sound = report.locator(`#report-${NIGHT}-F010`);
    await expect(sound).toBeFocused();
    await expect(sound).toContainText("-55 dBFS（相对声音）");
    await expect(sound).toContainText("25,180 / 25,200");
    await expect(report.locator(`#report-${NIGHT}-F004`)).toContainText("0 负荷单位");
    await expect(panel).not.toContainText("LLM结论暂缓");
    expect(methods.every((method) => method === "GET")).toBe(true);
    await report.locator(".report-evidence summary").click();
    await report.screenshot({ path: info.outputPath("synthetic-sleep-report-desktop.png") });
});

test("an optional morning rating is shown as cited generation-time evidence", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const value = payload();
    value.report.evidence.morning_feeling = { id: "F000", value: "good" };
    value.report.content.summary.evidence_ids.push("F000");
    await route(page, value);
    const panel = await open(page);
    const report = panel.locator(".sleep-report");
    await report.getByRole("button", { name: "查看证据 F000", exact: true }).press("Enter");
    const evidence = report.locator(`#report-${NIGHT}-F000`);
    await expect(evidence).toBeFocused();
    await expect(evidence).toContainText("晨起睡感");
    await expect(evidence).toContainText("较好");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const readinessUnavailable of [false, true]) {
    test(`published report stays successful despite a failed shared request, readiness unavailable ${readinessUnavailable}`, async ({ page }) => {
        const value = payload();
        Object.assign(value.model, {
            connection_state: "failed", last_check_status: "failed",
            last_error_code: "output_schema_required", last_check_at: "2026-11-09T01:30:00Z",
        });
        if (readinessUnavailable) {
            value.readiness = null; value.readiness_unavailable = true; value.data_state = "not_evaluated";
            value.report.source_changed = null;
        }
        await route(page, value);
        const panel = await open(page), details = panel.locator(".model-details");
        await expect(panel.locator(".report-badge")).toHaveText("报告已生成");
        await details.locator(":scope > summary").click();
        await expect(details.locator("dt").filter({ hasText: /^本晚报告$/ }).locator("xpath=following-sibling::dd[1]")).toHaveText("已生成");
        await expect(details).toContainText("最近请求未成功");
        await expect(details).toContainText("响应缺少必需字段");
        await expect(details).toContainText("不撤销已发布报告");
        await expect(details).not.toContainText("未发布");
        await expect(details).not.toContainText("连接核验失败");
        await expect(panel.locator(".report-summary")).toBeVisible();
    });
}

test("published report keeps old evidence when current core becomes insufficient", async ({ page }) => {
    const value = payload();
    value.report.source_changed = true;
    value.report.source_changed_reasons = ["source_data_changed"];
    value.data_state = value.readiness.data_state = "waiting_data";
    value.readiness.core_ready = false;
    value.readiness.quality_state = "insufficient";
    value.missing = value.readiness.missing = [{ code: "missing_garmin_core", metric: "sleep.night_average_hrv", source: "garmin", expected: "available", observed: "missing", recoverability: "await_sync" }];
    await route(page, value);
    const panel = await open(page);
    await expect(panel.locator(".report-source-warning")).toContainText("依据数据已变化");
    await expect(panel.locator(".report-summary")).toContainText(value.report.content.summary.text);
    await expect(panel.locator(".readiness-badge")).toHaveText("数据不足，暂不可分析");
    await panel.getByRole("button", { name: "查看证据 F003", exact: true }).first().click();
    await expect(panel.locator(`#report-${NIGHT}-F003`)).toContainText("42 ms");
});

test("unavailable current readiness retains published report and does not claim unchanged evidence", async ({ page }) => {
    const value = payload();
    value.readiness = null; value.readiness_unavailable = true; value.data_state = "not_evaluated";
    value.report.source_changed = null;
    await route(page, value);
    const panel = await open(page);
    await expect(panel.locator(".report-badge")).toHaveText("报告已生成");
    await expect(panel.locator(".report-source-warning")).toContainText("无法确认报告依据是否变化");
    await expect(panel).toContainText("当前完整性暂不可读取");
    await expect(panel.locator(".readiness-required")).toHaveCount(0);
});

for (const [state, label] of [["not_configured", "模型未配置"], ["disabled", "模型未启用"], ["queued", "等待生成"], ["running", "正在生成"], ["failed", "本次生成未成功"], ["blocked_budget", "今日调用额度已用完"], ["blocked_attempts", "尝试次数已达上限"], ["blocked_input_budget", "证据数据超过发送上限"], ["discarded_input_changed", "生成期间数据变化"], ["not_initialized", "报告存储尚未初始化"]]) {
    test(`report execution ${state} remains independent of data quality`, async ({ page }) => {
        const value = payload(); value.report = null; value.execution_state = state;
        value.attempt = { status: state, error_code: "private-secret-code", attempt_count: 1, retry_after: "2026-11-09T02:00:00Z" };
        await route(page, value);
        const panel = await open(page);
        await expect(panel.locator(".report-badge")).toContainText(label);
        await expect(panel.locator(".readiness-badge")).toHaveText("可分析，有数据缺口");
        await expect(panel).not.toContainText("private-secret-code");
        await expect(panel.locator(".report-summary")).toHaveCount(0);
        await expect(panel.locator(".sleep-report")).toContainText("10:00:00");
        if (state === "blocked_attempts")
            await expect(panel.locator(".sleep-report")).toContainText("跨天或数据更新不会清零");
    });
}

test("model evidence refusal is explained without a report or untrusted error output", async ({ page }) => {
    const value = payload(); value.report = null; value.execution_state = "failed";
    value.attempt = { status: "failed", error_code: "model_insufficient_input", attempt_count: 1, retry_after: null };
    value.model.last_error_code = "model_insufficient_input";
    value.model.last_check_at = "2026-11-09T01:00:00Z";
    await route(page, value);
    const panel = await open(page);
    await expect(panel.locator(".sleep-report")).toContainText("模型认为证据不足");
    await panel.getByText("模型配置与请求记录", { exact: true }).click();
    await expect(panel).toContainText("最近请求时间（共享）");
    await expect(panel).toContainText("今日模型请求次数");
    await expect(panel.locator(".model-details dt").filter({ hasText: /^本晚报告$/ }).locator("xpath=following-sibling::dd[1]")).toHaveText("本次生成未成功");
    await expect(panel.locator(".report-summary")).toHaveCount(0);
});

test("model prose is text, unknown private fields are ignored and evidence references are keyboard accessible", async ({ page }) => {
    const value = payload();
    value.report.content.summary.text = "<img src=x onerror=alert(1)> 此文字作为合成模型输出。";
    value.report.api_key = "SYNTHETIC_PRIVATE_KEY";
    value.report.evidence.facts[0].source_fingerprint = "SYNTHETIC_PRIVATE_HASH";
    await route(page, value);
    await page.addInitScript(() => { Storage.prototype.setItem = () => { throw new Error("report cannot persist in browser storage"); }; });
    const panel = await open(page);
    await expect(panel.locator(".report-summary")).toContainText("<img src=x onerror=alert(1)>");
    await expect(panel.locator(".sleep-report img,.sleep-report script,.sleep-report iframe")).toHaveCount(0);
    await expect(panel).not.toContainText("SYNTHETIC_PRIVATE");
    const reference = panel.getByRole("button", { name: "查看证据 F001", exact: true }).first();
    await reference.focus(); await reference.press("Enter");
    await expect(panel.locator(`#report-${NIGHT}-F001`)).toBeFocused();
    expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test("malformed reports never render an unvalidated conclusion or break other night cards", async ({ page }) => {
    for (const mutate of [
        (v) => { v.report = null; },
        (v) => { v.report.night_date = "2026-11-08"; },
        (v) => { v.report.content.summary.evidence_ids = ["F999"]; },
        (v) => { v.report.content.status = "insufficient_input"; },
        (v) => { v.report.content.observations.pop(); },
        (v) => { v.report.content.limitations = ["unobserved_lifestyle"]; },
    ]) {
        await page.unroute("**/api/v1/nights/*/analysis");
        const value = payload(); mutate(value); await route(page, value);
        const panel = await open(page);
        await expect(panel).toContainText("暂时无法读取分析状态");
        await expect(panel.locator(".report-summary")).toHaveCount(0);
        await expect(page.locator(".metric-card")).toHaveCount(4);
    }
});

test("timeline and event evidence preserve gaps, negative sound and actual active duration", async ({ page }) => {
    const value = payload();
    value.report.evidence.timeline = [{ id: "T001", start_at: "2026-11-08T15:00:00Z", end_at: "2026-11-08T15:15:00Z", profile_segment: "segment-1", temperature_c: 19.5, humidity_pct: 48, illuminance_lux: null, sound_dbfs: -54.1, coverage: {} }];
    value.report.evidence.events = [{ id: "E001", start_at: "2026-11-08T15:01:00Z", end_at: "2026-11-08T15:01:08Z", profile_segment: "segment-1", active_seconds: 3, max_1s_rms_dbfs: -38, merged: true, incomplete: true }];
    value.report.content.observations.find((row) => row.category === "environment").evidence_ids = ["T001", "E001"];
    value.report.content.hypotheses = [{ factor: "sound", text: "值得记录声音发生的背景，现有信息不能确认影响。", evidence_ids: ["E001"], evidence_level: "limited", alternatives: ["醒来后的活动也可能产生声音。"] }];
    await route(page, value);
    const panel = await open(page);
    await panel.getByRole("button", { name: "查看证据 E001", exact: true }).first().click();
    await expect(panel.locator(`#report-${NIGHT}-E001`)).toContainText("3秒");
    await expect(panel.locator(`#report-${NIGHT}-E001`)).toContainText("包络不等于整段持续声音");
    await expect(panel.locator(`#report-${NIGHT}-T001`)).toContainText("未知");
    await expect(panel.locator(`#report-${NIGHT}-T001`)).toContainText("-54.1 dBFS");
    await expect(panel.locator(".report-hypotheses")).toContainText("其他可能解释");
});

test("complete report remains readable on narrow screens and cards do not overflow", async ({ page }, info) => {
    await route(page, payload());
    for (const width of [320, 390]) {
        await page.setViewportSize({ width, height: 860 });
        const panel = await open(page);
        await panel.getByRole("button", { name: "查看证据 F010", exact: true }).first().click();
        const overflow = await panel.locator(".sleep-report").evaluate((node) => ({
            page: document.documentElement.scrollWidth > innerWidth,
            cards: [...node.querySelectorAll("article,dd,dt")].filter((child) => child.scrollWidth > child.clientWidth + 1).length,
        }));
        expect(overflow).toEqual({ page: false, cards: 0 });
    }
    await page.locator(".report-evidence summary").click();
    await page.locator(".sleep-report").screenshot({ path: info.outputPath("synthetic-sleep-report-mobile.png") });
});


test("history report publication is separate from saved readiness and does not infer current source changes", async ({ page }) => {
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch(); const body = await response.json();
        const states = ["complete", "not_generated", "unavailable", "SYNTHETIC_PRIVATE_STATE"];
        body.items = body.items.map((row, index) => ({ ...row, report_summary: { state: states[index % states.length], generated_at: "2026-11-09T01:05:00Z", source_changed: true, private: "SYNTHETIC_PRIVATE_KEY" } }));
        await route.fulfill({ response, json: body });
    });
    await page.goto("/sleep?view=history&date=2026-11-09&days=7");
    await expect(page.getByRole("columnheader", { name: "睡眠报告", exact: true })).toBeVisible();
    const states = page.locator(".report-history-state");
    await expect(states).toHaveCount(7);
    await expect(states.filter({ hasText: "报告已生成" })).toHaveCount(2);
    await expect(states.filter({ hasText: "尚未生成报告" })).toHaveCount(2);
    await expect(states.filter({ hasText: "报告记录暂不可用" })).toHaveCount(2);
    await expect(states.filter({ hasText: "暂无报告状态" })).toHaveCount(1);
    await expect(page.locator("#page-content")).not.toContainText("依据数据已变化");
    await expect(page.locator("#page-content")).not.toContainText("SYNTHETIC_PRIVATE");
});


test("eligible data with automatic generation disabled never promises a scheduled report", async ({ page }) => {
    const value = payload(); value.report = null; value.execution_state = "queued"; value.automatic_enabled = false;
    await route(page, value);
    const panel = await open(page);
    await expect(panel.locator(".sleep-report")).toContainText("自动分析未启用");
    await expect(panel.locator(".sleep-report")).toContainText("显式分析任务");
    await expect(panel.locator(".readiness-badge")).toHaveText("可分析，有数据缺口");
});

test("published interpretation is readable while current evidence is still being checked", async ({ page }) => {
    const value = payload();
    const summary = value.report.content.summary.text;
    await page.route(`**/api/v1/nights/${NIGHT}`, async (route) => {
        const response = await route.fetch();
        await route.fulfill({ json: { ...await response.json(), published_summary: summary } });
    });
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    await page.route("**/api/v1/nights/*/analysis", async (route) => {
        await pending;
        await route.fulfill({ json: value });
    });
    await page.goto(URL);
    try {
        await expect(page.locator(PANEL)).toHaveAttribute("aria-busy", "true");
        await expect(page.locator(PANEL).getByText(summary, { exact: true })).toBeVisible();
    } finally {
        release();
    }
    await expect(page.locator(PANEL)).toHaveAttribute("aria-busy", "false");
    await expect(page.locator(PANEL + " .report-observation-group")).toHaveCount(4);
});

test("nightly interpretation displays model-authored conclusions and optional feeling", async ({page}) => {
    const data = payload();
    data.report.content = {
        schema_version: "sleep-interpretation-1", night_date: NIGHT, status: "complete",
        summary: {text: "昨晚睡了 6 小时。"}, garmin: "可穿戴记录提示睡眠机会偏短。",
        environment: "夜间湿度有变化。", feeling: null, possible_reasons: ["可能与作息有关。"], comparison: null,
    };
    await route(page, data);
    const panel = await open(page);
    await expect(panel.getByText("昨晚睡了 6 小时。", {exact:true})).toBeVisible();
    await expect(panel.getByText("可能与作息有关。", {exact:true})).toBeVisible();
    await expect(panel.locator(".sleep-report").getByRole("heading",{name:"晨起睡感"})).toHaveCount(0);
});
