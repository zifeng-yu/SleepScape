import { test, expect } from "@playwright/test";

const NIGHT = "2026-11-09";
const NIGHT_URL = `/sleep?view=night&date=${NIGHT}`;
const START = "2026-11-08T22:00:00+08:00";
const END = "2026-11-09T06:00:00+08:00";
const CHECKED = "2026-11-09T09:05:00+08:00";
const CHANNELS = ["temperature_c", "relative_humidity_pct", "illuminance_lux", "sound_relative"];

// All health inputs below are synthetic. Never target the private dashboard.
test.beforeEach(async ({ request, page }) => {
    const response = await request.get("/api/v1/status");
    expect(response.ok()).toBe(true);
    expect((await response.json()).demo).toBe(true);
    page.on("pageerror", (error) => { throw error; });
});

function gap(overrides = {}) {
    return {
        code: "missing_environment_minutes", source: "environment", metric: "environment.window",
        date: NIGHT, window_start: "2026-11-09T01:00:00+08:00", window_end: "2026-11-09T01:02:00+08:00",
        expected: 480, observed: 478, recoverability: "await_upload", last_checked_at: CHECKED,
        ...overrides,
    };
}

function readiness(state = "waiting_data", overrides = {}) {
    const complete = state === "ready";
    const result = {
        policy_version: "dual-source-coverage-2", night_date: NIGHT, checked_at: CHECKED,
        policy: { min_channel_coverage: 0.99, max_affected_span_seconds: 300 },
        data_state: state, core_ready: complete, quality_state: complete ? "complete" : "insufficient",
        warnings: [],
        missing: complete ? [] : [gap({ code: "insufficient_channel_coverage", metric: "sound_relative", expected: 0.99, observed: 0.98 }), gap({
            code: "missing_garmin_core", source: "garmin", metric: "sleep.night_average_hrv",
            expected: "available", observed: "missing", recoverability: "await_sync",
            window_start: START, window_end: END,
        })],
        optional_missing: [gap({
            code: "optional_unavailable", source: "manual", metric: "lifestyle.context",
            expected: "available", observed: "not_provided", recoverability: "unknown",
        })],
        window: { start: START, end: END, duration_seconds: 28800 },
        environment_summary: {
            expected_windows: 480, observed_windows: complete ? 480 : 478,
            missing_windows: complete ? 0 : 2, missing_windows_lower_bound: complete ? 0 : 2,
            count_exact: true, expected_seconds: 28800, covered_seconds: complete ? 28800 : 28680,
            missing_seconds: complete ? 0 : 120, overlap_seconds: 0,
            channels: Object.fromEntries(CHANNELS.map((metric) => [metric, {
                expected_windows: 480, valid_windows: complete ? 480 : 478, invalid_windows: 0,
                expected_count: metric === "sound_relative" ? 28800 : 2880,
                observed_count: metric === "sound_relative" ? (complete ? 28800 : 28680) : (complete ? 2880 : 2868),
                valid_count: metric === "sound_relative" ? (complete ? 28800 : 28680) : (complete ? 2880 : 2868),
                usable_count: metric === "sound_relative" ? (complete ? 28800 : 28224) : (complete ? 2880 : 2868),
                effective_coverage_ratio: complete ? 1 : metric === "sound_relative" ? 0.98 : 478 / 480,
                max_affected_span_seconds: complete ? 0 : 120,
                ...(metric === "sound_relative" ? { audio_missing_seconds: complete ? 0 : 576, audio_missing_scope: "whole_observed_minutes" } : {}),
                coverage_ratio: complete ? 1 : 478 / 480,
                coverage_scope: "fully_valid_windows_clipped_to_sleep",
            }])),
            gaps: complete ? [] : [{ start: "2026-11-09T01:00:00+08:00", end: "2026-11-09T01:02:00+08:00", seconds: 120 }],
        },
        garmin_summary: {
            required_count: 5, available_count: complete ? 5 : 4, window_state: "available",
            metrics: Object.fromEntries(["sleep_seconds", "average_heart_rate", "night_average_hrv", "training_load", "previous_stress"].map((metric) =>
                [metric, { state: !complete && metric === "night_average_hrv" ? "missing" : "available", source_date: metric === "training_load" || metric === "previous_stress" ? "2026-11-08" : NIGHT }])),
        },
        limitations: ["wearable_measurement_uncertain", "relative_sound_uncalibrated", "minute_statistics_include_boundary_outside_sleep"],
        ...overrides,
    };
    return {
        night_date: NIGHT, implementation_stage: "reports", data_state: result.data_state,
        execution_state: "not_configured", automatic_enabled: false, missing: result.missing,
        readiness: result, blockers: ["model_not_configured"], report: null,
        model: {
            model: "qwen3.8-max", enabled: false, configured: false,
            configuration_state: "not_configured", connection_state: "not_checked",
            budget: { used: 0, limit: 30, remaining: 30 }, usage: {},
        },
    };
}

function minorGaps() {
    const payload = readiness("ready");
    Object.assign(payload.readiness, {
        quality_state: "with_gaps",
        warnings: [gap({ code: "invalid_channel", metric: "sound_relative", expected: "complete_window", observed: "counts_incomplete", recoverability: "unrecoverable" })],
    });
    Object.assign(payload.readiness.environment_summary.channels.sound_relative, {
        valid_count: 28777, usable_count: 28777, effective_coverage_ratio: 28777 / 28800,
        valid_windows: 478, invalid_windows: 2, coverage_ratio: 478 / 480,
        max_affected_span_seconds: 120, audio_missing_seconds: 23,
    });
    return payload;
}

async function mock(page, payload) {
    await page.route("**/api/v1/nights/*/analysis", (route) => route.fulfill({ json: payload }));
}

async function open(page) {
    await page.goto(NIGHT_URL);
    await expect(page.locator("#demo-label")).toHaveText("演示数据");
    await expect(page.locator("#sleep-analysis-panel")).toHaveAttribute("aria-busy", "false");
    return page.locator("#sleep-analysis-panel");
}

test("readiness shows required gap periods, numbers and recoverability independently of model setup", async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const writes = [];
    page.on("request", (request) => {
        if (request.url().includes("/api/") && request.method() !== "GET") writes.push(request.method());
    });
    await mock(page, readiness());
    const panel = await open(page);
    await expect(panel.getByRole("heading", { name: "数据完整性检查", exact: true })).toBeVisible();
    await expect(panel.locator(".readiness-badge")).toHaveText("数据不足，暂不可分析");
    await expect(panel.locator(".quality-details")).not.toHaveAttribute("open", "");
    await expect(panel.locator(".readiness-required")).toBeHidden();
    await expect(panel.locator(".quality-heading + .analysis-note")).toContainText("2项阻塞");
    await expect(panel.locator(".readiness-required .readiness-gap")).toHaveCount(2);
    await expect(panel.locator(".readiness-required")).toContainText("99% / 98%");
    await expect(panel.locator(".readiness-required")).toContainText("01:00:00");
    await expect(panel.locator(".readiness-required")).toContainText("01:02:00");
    await expect(panel.locator(".readiness-required")).toContainText("等待设备补传");
    await expect(panel.locator(".readiness-required")).toContainText("等待手表同步后回查");
    await expect(panel.locator(".readiness-channel")).toHaveCount(4);
    await expect(panel.locator(".readiness-environment")).toContainText("120秒 / 0秒");
    await expect(panel.getByRole("heading", { name: "本晚睡眠解读", exact: true })).toBeVisible();
    await panel.getByText("模型配置与请求记录", { exact: true }).click();
    await expect(panel).toContainText("未配置完整");
    await panel.locator(".quality-details > summary").click();
    await panel.locator(".readiness-policy summary").click();
    await expect(panel.locator(".readiness-policy")).toContainText("有效采样覆盖至少99%");
    await expect(panel.locator(".readiness-policy")).toContainText("不超过300秒");
    await expect(panel.locator(".readiness-policy")).toContainText("初始工程参数，不是医学保证");
    await expect(panel).toContainText("09:05:00");
    expect(writes).toEqual([]);
    await panel.screenshot({ path: info.outputPath("synthetic-readiness-desktop.png") });
});

test("ready data remains ready without model key and optional gaps never block it", async ({ page }) => {
    const payload = readiness("ready");
    payload.readiness.optional_missing.push(gap({ code: "optional_unavailable", source: "garmin", metric: "sleep.stage_timeline", expected: "available", observed: "unsupported", recoverability: "unknown" }));
    await mock(page, payload);
    const panel = await open(page);
    await expect(panel.locator(".readiness-badge")).toHaveText("可分析，数据完整");
    await expect(panel.locator(".readiness-required")).toContainText("本次检查没有阻塞项");
    await expect(panel.locator(".readiness-channel").first()).toContainText("100%");
    await panel.locator(".quality-details > summary").click();
    await panel.locator(".readiness-optional summary").click();
    await expect(panel.locator(".readiness-optional")).toContainText("2项，不阻塞");
    await expect(panel.locator(".readiness-optional")).toContainText("咖啡因、酒精等生活记录");
    await expect(panel.locator(".readiness-optional")).toContainText("当前来源不支持此能力");
    await expect(panel.locator(".readiness-execution")).toContainText("模型未配置");
    await expect(panel.getByRole("button")).toHaveCount(0);
});

test("minor gaps permit later analysis while exposing actual sample coverage and audio loss", async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const methods = [];
    page.on("request", (request) => {
        if (request.url().includes("/api/")) methods.push(request.method());
    });
    await mock(page, minorGaps());
    const panel = await open(page);
    await expect(panel.locator(".readiness-badge")).toHaveText("可分析，有数据缺口");
    await expect(panel.locator(".readiness-required .readiness-gap")).toHaveCount(0);
    await expect(panel.locator(".readiness-warnings .readiness-gap")).toHaveCount(1);
    await panel.locator(".quality-details > summary").click();
    const warnings = panel.locator("details.readiness-warnings");
    const warningToggle = warnings.locator(":scope > summary");
    await expect(warnings).not.toHaveAttribute("open", "");
    await expect(warningToggle).toHaveText("数据缺口与质量提示（1项，不单独阻塞）");
    await expect(warnings.locator(".readiness-gap")).not.toBeVisible();
    await warningToggle.click();
    await expect(warnings.locator(".readiness-gap")).toBeVisible();
    await expect(panel.locator(".readiness-warnings")).toContainText("01:00:00");
    await expect(panel.locator(".readiness-warnings")).toContainText("01:02:00");
    await expect(panel.locator(".readiness-warnings")).toContainText("已确认无法由补采恢复");
    await warningToggle.press("Enter");
    await expect(warnings).not.toHaveAttribute("open", "");
    await warningToggle.press("Space");
    await expect(warnings).toHaveAttribute("open", "");
    const sound = panel.locator('[data-metric="sound_relative"]');
    await expect(sound.locator(":scope > .analysis-facts")).toContainText("99.92%");
    await expect(sound.locator(":scope > .analysis-facts")).toContainText("28,777 / 28,800");
    await expect(sound.locator(":scope > .analysis-facts")).toContainText("23秒");
    await expect(sound.locator(":scope > .analysis-facts")).toContainText("120秒");
    await expect(sound.locator(":scope > .analysis-facts")).not.toContainText("99.58%");
    await sound.locator("summary").click();
    await expect(sound.locator(".readiness-window-reference")).toContainText("478 / 480");
    await expect(sound.locator(".readiness-window-reference")).toContainText("99.58%");
    await expect(panel.locator(".readiness-environment")).toContainText("不能当作真实连续丢失音频的时长");
    await expect(panel.locator(".readiness-execution")).toContainText("模型未配置");
    expect(methods.every((method) => method === "GET")).toBe(true);
    await panel.screenshot({ path: info.outputPath("synthetic-coverage-desktop.png") });
});

test("a counted light bus fault displays its sample counts and warning", async ({ page }) => {
    const value = minorGaps();
    Object.assign(value.readiness.warnings[0], {
        code: "light_bus_sampling_gap", metric: "illuminance_lux",
        expected: 60, observed: 58,
    });
    await mock(page, value);
    const panel = await open(page);
    await expect(panel.locator(".readiness-badge")).toHaveText("可分析，有数据缺口");
    await panel.locator(".quality-details > summary").click();
    const warnings = panel.locator("details.readiness-warnings");
    await warnings.locator(":scope > summary").click();
    await expect(warnings).toContainText("光照通信短暂异常，保留有效读数");
    await expect(warnings).toContainText("60 / 58");
});

for (const reason of ["coverage", "span"]) {
    test(`${reason === "coverage" ? "coverage below minimum" : "span above maximum"} blocks analysis without hiding smaller gaps`, async ({ page }) => {
        const payload = minorGaps();
        payload.data_state = payload.readiness.data_state = "waiting_data";
        payload.readiness.core_ready = false;
        payload.readiness.quality_state = "insufficient";
        const sound = payload.readiness.environment_summary.channels.sound_relative;
        if (reason === "coverage") {
            sound.effective_coverage_ratio = 0.989;
            sound.usable_count = 28483;
            payload.readiness.missing = [gap({ code: "insufficient_channel_coverage", metric: "sound_relative", expected: 0.99, observed: 0.989 })];
        } else {
            sound.max_affected_span_seconds = 360;
            payload.readiness.missing = [gap({ code: "excessive_sampling_gap", metric: "sound_relative", expected: 300, observed: 360,
                window_start: "2026-11-09T01:00:00+08:00", window_end: "2026-11-09T01:06:00+08:00" })];
        }
        payload.missing = payload.readiness.missing;
        await mock(page, payload);
        const panel = await open(page);
        await expect(panel.locator(".readiness-badge")).toHaveText("数据不足，暂不可分析");
        await expect(panel.locator(".readiness-required .readiness-gap")).toHaveCount(1);
        await expect(panel.locator(".readiness-required")).toContainText(reason === "coverage" ? "99% / 98.9%" : "300秒 / 360秒");
        await expect(panel.locator(".readiness-warnings .readiness-gap")).toHaveCount(1);
        await expect(panel.getByRole("button")).toHaveCount(0);
    });
}

test("expired nights preserve actual completeness and never promise automatic backfill", async ({ page }) => {
    for (const coreReady of [false, true]) {
        await page.unroute("**/api/v1/nights/*/analysis");
        const base = readiness(coreReady ? "ready" : "waiting_data");
        base.data_state = "expired_unanalyzed";
        base.readiness.data_state = "expired_unanalyzed";
        await mock(page, base);
        const panel = await open(page);
        await expect(panel.locator(".readiness-badge")).toHaveText("超出回查期限，未分析");
        await expect(panel.locator(".analysis-note").first()).toContainText(coreReady ? "必需观测完整" : "未满足可用性要求");
        await expect(panel.locator(".readiness-required .readiness-gap")).toHaveCount(coreReady ? 0 : 2);
    }
});

test("unknown counts and channel coverage stay unknown, with bounded safe metadata", async ({ page }) => {
    const payload = readiness();
    const summary = payload.readiness.environment_summary;
    Object.assign(summary, { expected_windows: null, missing_windows: null, count_exact: false, missing_windows_lower_bound: 1 });
    Object.assign(summary.channels.sound_relative, { expected_windows: null, expected_count: null, coverage_ratio: null, effective_coverage_ratio: null, audio_missing_seconds: null, max_affected_span_seconds: null });
    summary.channels.temperature_c = null;
    summary.channels.illuminance_lux.effective_coverage_ratio = 0.999999;
    summary.channels.relative_humidity_pct.effective_coverage_ratio = 0.00000001;
    summary.missing_seconds = 0.0001;
    payload.readiness.missing.push(gap({ code: "SYNTHETIC_SECRET_CODE", metric: "SYNTHETIC_PRIVATE_METRIC", source: "SYNTHETIC_SOURCE", expected: "SYNTHETIC_EXPECTED", observed: "SYNTHETIC_OBSERVED", recoverability: "SYNTHETIC_RECOVERY", date: "SYNTHETIC_DATE", window_start: "SYNTHETIC_TIME", last_checked_at: "SYNTHETIC_CHECK" }));
    payload.readiness.source_fingerprint = "SYNTHETIC_PRIVATE_FINGERPRINT";
    payload.readiness.limitations.push("SYNTHETIC_LIMITATION");
    payload.readiness.environment_summary.channels.sound_relative.private_ref = "SYNTHETIC_PRIVATE_REF";
    await mock(page, payload);
    const panel = await open(page);
    await expect(panel.locator(".readiness-environment")).toContainText("精确数量未知，至少 1");
    await expect(panel.locator('[data-metric="sound_relative"]')).toContainText("478 / 未知");
    await expect(panel.locator('[data-metric="sound_relative"]')).toContainText("28,224 / 未知");
    await expect(panel.locator('[data-metric="sound_relative"]')).toContainText("未知秒");
    await expect(panel.locator('[data-metric="sound_relative"]')).not.toContainText("100%");
    await expect(panel.locator('[data-metric="temperature_c"]')).toContainText("未知 / 未知");
    await expect(panel.locator('[data-metric="illuminance_lux"]')).toContainText(">99.99%（未满100%）");
    await expect(panel.locator('[data-metric="relative_humidity_pct"]')).toContainText("<0.01%");
    await expect(panel.locator(".readiness-environment")).toContainText("<0.01秒 / 0秒");
    await expect(panel).toContainText("未识别的数据项");
    await expect(panel).not.toContainText("SYNTHETIC_");
});

test("malformed or mismatched readiness fails only its panel without rendering reports", async ({ page }) => {
    const variants = [
        (value) => { value.night_date = "2026-11-08"; },
        (value) => { value.report = { summary: "SYNTHETIC_REPORT_MUST_NOT_RENDER" }; },
        (value) => { value.readiness.policy_version = "SYNTHETIC_UNKNOWN_POLICY"; },
        (value) => { value.readiness.policy_version = "dual-source-strict-1"; },
        (value) => { value.readiness.policy.min_channel_coverage = null; },
        (value) => { value.readiness.quality_state = "with_gaps"; },
        (value) => { value.readiness.warnings = null; },
        (value) => { value.readiness.data_state = "waiting_data"; },
        (value) => { value.readiness.core_ready = false; },
        (value) => { value.readiness.missing = [gap()]; },
        (value) => { value.readiness.checked_at = "SYNTHETIC_CHECK_TIME"; },
    ];
    for (const mutate of variants) {
        await page.unroute("**/api/v1/nights/*/analysis");
        const value = readiness("ready");
        mutate(value);
        await mock(page, value);
        const panel = await open(page);
        await expect(panel).toContainText("暂时无法读取分析状态");
        await expect(panel).toContainText("不将读取失败当作数据缺失");
        await expect(panel).not.toContainText("SYNTHETIC_");
        await expect(page.locator(".metric-card")).toHaveCount(4);
    }
});

test("not evaluated and read failures never invent missing environmental data", async ({ page }) => {
    const value = readiness("not_evaluated");
    value.readiness = null;
    value.missing = [];
    await mock(page, value);
    let panel = await open(page);
    await expect(panel.locator(".readiness-badge")).toHaveText("尚未检查");
    await expect(panel).toContainText("不能据此判断数据齐全或缺失");
    await expect(panel.locator(".readiness-environment")).toHaveCount(0);
    await page.unroute("**/api/v1/nights/*/analysis");
    await page.route("**/api/v1/nights/*/analysis", (route) => route.fulfill({ status: 503, json: { detail: "readiness_unavailable" } }));
    panel = await open(page);
    await expect(panel).toContainText("暂时无法读取分析状态");
    await expect(panel.locator(".readiness-gap")).toHaveCount(0);
    await expect(page.locator(".metric-card")).toHaveCount(4);
});

test("history labels saved check time and supports unknown or absent checks", async ({ page }) => {
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        const states = ["ready", "waiting_data", "expired_unanalyzed", "not_evaluated", "ready", "SYNTHETIC_STATE", null];
        body.items = body.items.map((item, index) => ({ ...item,
            analysis_summary: states[index] ? { state: states[index], checked_at: index === 4 ? "SYNTHETIC_TIME" : CHECKED, label: "SYNTHETIC_LABEL", policy_version: "dual-source-coverage-2", current_policy: true, quality_state: states[index] === "ready" ? "complete" : "insufficient" } : undefined,
        }));
        await route.fulfill({ json: body });
    });
    await page.goto(`/sleep?view=history&date=${NIGHT}`);
    const states = page.locator(".analysis-history-state");
    await expect(states).toHaveCount(7);
    await expect(states.nth(0)).toContainText("可分析，数据完整 · 上次检查");
    await expect(states.nth(0)).toContainText("09:05:00");
    await expect(states.nth(1)).toContainText("数据不足，暂不可分析 · 上次检查");
    await expect(states.nth(2)).toContainText("超出回查期限，未分析 · 数据不足，暂不可分析 · 上次检查");
    await expect(states.nth(3)).toHaveText("尚未检查");
    await expect(states.nth(4)).toContainText("上次检查：未知");
    await expect(states.nth(5)).toHaveText("暂无分析状态");
    await expect(states.nth(6)).toHaveText("暂无分析状态");
    await expect(page.locator("body")).not.toContainText("SYNTHETIC_");
});

test("historical checks keep their old policy and never grant current permission retroactively", async ({ page }) => {
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        const summaries = [
            { state: "ready", quality_state: "with_gaps", policy_version: "dual-source-coverage-2", current_policy: true },
            { state: "waiting_data", quality_state: null, policy_version: "dual-source-strict-1", current_policy: false },
            { state: "ready", quality_state: null, policy_version: "dual-source-strict-1", current_policy: false },
            { state: "ready" },
            { state: "waiting_data", policy_version: "SYNTHETIC_POLICY_SECRET", current_policy: false },
            { state: "not_evaluated", policy_version: null, current_policy: null },
            { state: "blocked_implementation" },
        ];
        body.items = body.items.map((item, index) => ({ ...item, analysis_summary: { ...summaries[index], checked_at: CHECKED, scope: "last_saved_check" } }));
        await route.fulfill({ json: body });
    });
    await page.goto(`/sleep?view=history&date=${NIGHT}`);
    const states = page.locator(".analysis-history-state");
    await expect(states.nth(0)).toContainText("可分析，有数据缺口 · 上次检查");
    for (const index of [1, 2, 3, 4]) {
        await expect(states.nth(index)).toContainText("旧规则检查，待按当前规则复查");
        await expect(states.nth(index)).toContainText("上次检查");
        await expect(states.nth(index)).not.toContainText("可分析");
    }
    await expect(states.nth(1)).toContainText("当时结果：必需数据待补齐");
    await expect(states.nth(2)).toContainText("当时结果：必需数据已齐全");
    await expect(states.nth(5)).toHaveText("尚未检查");
    await expect(states.nth(6)).toHaveText("暂无分析状态");
    await expect(page.locator("body")).not.toContainText("SYNTHETIC_");
});

test("unreadable history checks show an isolated error and recover without replacing stored observations", async ({ page }) => {
    let unavailable = true;
    const mutations = [];
    page.on("request", (request) => {
        if (request.url().includes("/api/") && request.method() !== "GET") mutations.push(request.method());
    });
    await page.route("**/api/v1/nights?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.items = body.items.map((item, index) => ({ ...item, analysis_summary: {
            state: unavailable && index === 0 ? "not_evaluated" : "ready",
            checked_at: unavailable && index === 0 ? null : CHECKED,
            scope: "last_saved_check", unavailable: unavailable && index < 2,
            policy_version: "dual-source-coverage-2", current_policy: true, quality_state: "with_gaps",
            error: "SYNTHETIC_PRIVATE_STORAGE_ERROR",
        } }));
        await route.fulfill({ json: body });
    });
    const url = `/sleep?view=history&date=${NIGHT}`;
    await page.goto(url);
    const states = page.locator(".analysis-history-state");
    await expect(states).toHaveCount(7);
    for (const index of [0, 1]) {
        await expect(states.nth(index)).toHaveText("检查记录暂不可用，请稍后刷新");
        await expect(states.nth(index)).not.toContainText("尚未检查");
        await expect(states.nth(index)).not.toContainText("可分析");
    }
    await expect(states.nth(2)).toContainText("可分析，有数据缺口");
    const observedRow = await states.nth(0).locator("xpath=..").innerText();
    expect(observedRow).toContain(NIGHT);
    expect(observedRow.replace("检查记录暂不可用，请稍后刷新", "")).toMatch(/\d/);
    await expect(page.locator("body")).not.toContainText("SYNTHETIC_");
    unavailable = false;
    await page.reload();
    await expect(states.nth(0)).toContainText("可分析，有数据缺口 · 上次检查");
    await expect(states.nth(0)).toContainText("09:05:00");
    await expect(states.nth(0)).not.toContainText("暂不可用");
    expect(mutations).toEqual([]);
});

test("390px completeness layout and independent disclosures remain readable without overflow", async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mock(page, minorGaps());
    const panel = await open(page);
    await panel.locator(".quality-details > summary").click();
    const warnings = panel.locator("details.readiness-warnings");
    await expect(warnings).not.toHaveAttribute("open", "");
    await warnings.locator(":scope > summary").click();
    await expect(warnings.locator(".readiness-gap")).toBeVisible();
    await panel.locator(".readiness-optional summary").click();
    await expect(panel.locator(".readiness-optional")).toHaveAttribute("open", "");
    await expect(panel.locator(".readiness-policy")).not.toHaveAttribute("open", "");
    await panel.locator(".readiness-policy summary").click();
    await expect(panel.locator(".readiness-policy")).toHaveAttribute("open", "");
    const geometry = await panel.evaluate((element) => ({
        pageWidth: document.documentElement.scrollWidth, viewport: innerWidth,
        panelWidth: element.scrollWidth, boxWidth: element.clientWidth,
        overflowing: [...element.querySelectorAll(".readiness-gap, .readiness-channel, dd, dt")].filter((node) => node.scrollWidth > node.clientWidth + 1).length,
    }));
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewport);
    expect(geometry.panelWidth).toBeLessThanOrEqual(geometry.boxWidth);
    expect(geometry.overflowing).toBe(0);
    // A viewport capture avoids fixed offscreen skip-links entering an oversized element capture.
    await panel.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.screenshot({ path: info.outputPath("synthetic-readiness-mobile.png") });
});
