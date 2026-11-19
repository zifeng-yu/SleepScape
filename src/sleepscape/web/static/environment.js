/* Safe environment presentation. Source observations are never inferred from page state. */
import { el, add, button } from "./dom.js";
import { number, numericText } from "./format.js";
import { metricLabel, metricInfo } from "./metrics.js";

const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const array = (value, limit = 100) => Array.isArray(value) ? value.slice(0, limit) : [];
const STATES = {
    available: "可查询", not_initialized: "尚无环境记录", index_not_initialized: "时间索引尚未启用",
    unavailable: "环境数据暂不可用", missing: "缺失", invalid: "无效", partial: "部分有效",
    warming: "暖机中", synced: "已校时", estimated: "估计时间", unknown: "时间未知",
    conflict: "校时冲突", valid: "有效", ready: "可用", not_indexed: "尚未完成时间索引", never_received: "尚未收到记录",
    recently_received: "最近收到记录", not_recently_received: "近期未收到记录",
    clock_conflict: "接收时间异常", mapped: "已有可映射采集时间", no_mapped_capture: "没有可映射采集时间",
};
const QUALITY = {
    partial_window: "非完整分钟", sensor_error: "传感器异常", sensor_unavailable: "传感器不可用",
    missing: "读数缺失", bus_error: "传感器总线异常", crc_error: "读数校验失败", dma_overflow: "音频DMA溢出",
    incomplete: "记录不完整", merged: "摘要合并", ranges_truncated: "丢失区间记录达到上限",
    missing_samples: "读数缺失", out_of_range: "超出有效量程", low_light_limit: "低光量化限制",
    audio_gap: "音频采样缺口", clipped: "音频削波", clipping: "音频削波", warming: "暖机中",
    below_numeric_floor: "低于数值下限，不能作为有效0值", time_unknown: "时间未知",
    clock_error_unknown: "绝对时钟误差未知", clock_conflict: "校时冲突", time_conflict: "校时冲突",
    device_restarted: "启动周期切换", queue_overflow: "队列溢出", event_merged: "合并事件",
    events_dropped: "事件已丢弃", event_limit: "事件摘要达到上限", event_detector_warming: "事件检测暖机",
    clock_drift_unverified: "时钟漂移未经核验", anchor_quality_rejected: "时间锚点质量未通过",
    no_usable_time_anchor: "缺少可用时间锚点", anchor_conflict: "时间锚点冲突",
    whole_boot_conflict: "整个启动周期存在校时冲突", utc_out_of_range: "映射UTC超出有效范围",
};
const stateLabel = (value) => STATES[value] || "状态未知";
const ref = (value) => typeof value === "string" && /^(source|boot|device|placement)-[a-f0-9]{24}$/.test(value)
    ? value.slice(-8) : "未知";
const version = (value) => typeof value === "string" && /^[0-9][0-9A-Za-z._-]{0,39}$/.test(value) ? value : "未知";
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const count = (value) => integer(value) ? numericText(value) : "未知";
const percent = (value) => number(value) && value >= 0 && value <= 100 ? `${numericText(value, 1)}%` : "未知";
const stamp = (value, time) => typeof value === "string" && /^20\d\d-\d\d-\d\dT/.test(value)
    && Number.isFinite(Date.parse(value)) ? time.dateTime(value, true) : "未知";
function qualityText(values) {
    return [...new Set(array(values, 30).map((value) => QUALITY[value]).filter(Boolean))].join("；");
}
function facts(entries) {
    const list = el("dl", "environment-facts");
    for (const [name, value] of entries) add(list, el("dt", "", name), el("dd", "", value));
    return list;
}
function timeQuality(point) {
    const uncertainty = number(point.time_uncertainty_ms)
        ? `映射不确定度 ±${numericText(point.time_uncertainty_ms, 1)}毫秒` : "绝对误差未知";
    return `${stateLabel(point.time_quality)} · ${uncertainty}`;
}
function environmentPanel(value, time) {
    const data = object(value), panel = el("section", "environment-overview");
    panel.id = "environment-panel";
    panel.dataset.state = Object.hasOwn(STATES, data.state) ? data.state : "unavailable";
    panel.setAttribute("aria-label", "环境观测与时间质量");
    add(panel, el("h3", "", "环境观测"), el("p", "environment-state", stateLabel(data.state)));
    if (data.state === "index_not_initialized")
        panel.append(el("p", "environment-note", "已保存的环境事实尚未建立时间索引。需要显式初始化，刷新不会修改数据库。"));
    else if (data.state === "not_initialized")
        panel.append(el("p", "environment-note", "当前没有可查询的环境记录；未记录不等于温度、光照或声音为零。"));
    else if (data.state !== "available")
        panel.append(el("p", "environment-note", "当前无法读取环境序列。Garmin观测继续显示，请稍后刷新重试。"));
    if (data.state === "available") {
        const coverage = object(data.coverage), global = object(data.global_time_state);
        const details = add(el("details", "environment-details"), el("summary", "", "查看通道覆盖、配置与时间分段"));
        add(details, facts([
            ["当前范围记录", count(data.record_count)],
            ["可见来源／启动分段", `${array(data.sources).length} / ${array(data.segments, 200).length}`],
            ["分钟窗口覆盖", number(coverage.covered_duration_ms) ? `${numericText(coverage.covered_duration_ms / 60000, 1)}分钟` : "未计算"],
            ["时间缺口／重叠", data.coverage ? `${coverage.gaps_truncated ? "至少" : ""}${array(coverage.gaps, 10000).length} / ${coverage.overlaps_truncated ? "至少" : ""}${array(coverage.overlaps, 10000).length} 段` : "未计算"],
            ["历史未知／冲突时间", `${count(global.unknown_record_count)} / ${count(global.conflict_record_count)} 条`],
            ["待更新时间索引", `${count(global.stale_source_record_count)} 条历史记录`],
        ]), el("p", "environment-note", "覆盖仅描述当前查询返回的分钟时间窗口，不代表四通道齐全或整夜睡眠完整。未知、冲突和待更新的时间不会硬叠加到图中。"));
        if (data.truncated || data.series_truncated || data.events_truncated || coverage.gaps_truncated || coverage.overlaps_truncated)
            panel.append(el("p", "environment-warning", "当前查询达到展示上限，部分序列或事件未展示；请缩小时间范围，不能据此判定完整覆盖。"));
        if (data.configuration_boundaries || array(data.sources).length > 1 || array(data.segments, 1000).length > 1)
            panel.append(el("p", "environment-warning", "当前范围有多个测量配置、来源或启动周期，按配置与启动周期分段展示，不跨段连线或混合比较相对声音。"));
        const summaries = array(data.channel_summaries, 80);
        if (summaries.length) {
            const list = el("div", "environment-channel-list");
            for (const row of summaries) {
                if (!metricInfo(row.metric)) continue;
                const item = add(el("article", "environment-channel"), el("h4", "", `${metricLabel(row.metric)} · 配置 ${ref(row.source_ref)} · 启动 ${ref(row.boot_ref)}`), facts([
                    ["窗口：有有效采样（含部分）／部分／无有效采样", `${count(row.valid_window_count)} / ${count(row.partial_window_count)} / ${count(row.missing_window_count)}`],
                    ["有效／已读／预期", `${count(row.valid_count)} / ${count(row.observed_count)} / ${count(row.expected_count)}`],
                    ["有效读数比例", percent(row.coverage_percent)],
                    ["已返回窗口极值", `${numericText(row.min, 2)} — ${numericText(row.max, 2)} ${metricInfo(row.metric).unit}`],
                ]));
                const quality = qualityText(row.quality);
                if (quality) item.append(el("p", "environment-note", quality));
                list.append(item);
            }
            details.append(list);
        }
        for (const source of array(data.sources, 32)) {
            const light = object(source.light_measurement);
            details.append(facts([
                ["配置／设备／摆位", `${ref(source.source_ref)} / ${ref(source.device_ref)} / ${ref(source.placement_ref)}`],
                ["固件", version(source.firmware_version)],
                ["传感器", `${sensor(source.temperature_sensor)} / ${sensor(source.light_sensor)} / ${sensor(source.microphone)}`],
                ["聚合窗口", `${numericText(source.aggregation_ms / 1000, 1)}秒`],
                ["温湿度／光照采样间隔", `${numericText(source.temperature_period_ms / 1000, 1)} / ${numericText(source.light_period_ms / 1000, 1)}秒`],
                ["声音采样率", `${count(source.sample_rate_hz)} Hz；相对声音 dBFS，未作绝对校准`],
                ["光照分辨率／低光标记", `${numericText(light.resolution_lux, 4)} / ${numericText(light.low_light_limit_lux, 4)} lx`],
                ["事件检测阈值", thresholdText(source.event_detection)],
            ]));
        }
        const segments = array(data.segments, 1000);
        if (segments.length) {
            const list = el("ul", "environment-segments");
            for (const segment of segments) list.append(el("li", "", `配置 ${ref(segment.source_ref)} · 启动 ${ref(segment.boot_ref)}：${stamp(segment.utc_start, time)} — ${stamp(segment.utc_end, time)}；${count(segment.record_count)}个窗口`));
            details.append(list);
        }
        panel.append(details);
    }
    panel.append(el("p", "environment-footnote", "环境数据只代表传感器摆放处。光照低读数不证明绝对无光；相对声音不是dBA，也不识别声源或判断觉醒原因。"));
    return panel;
}

function environmentPointLines(point, series, time, peerPoint) {
    const sound = Object.keys(object(point.coverage)).length > 0;
    const lines = [
        `分钟窗口 ${time.clockTime(point.window_start)} — ${time.clockTime(point.window_end)}`,
        ...(sound ? [`${series.series_type === "minute_peak" ? "分钟能量均值" : "最高1秒声音"} ${numericText(peerPoint?.value, 1)} dBFS；未作绝对声学校准`] : [`min / max ${numericText(point.min, 2)} / ${numericText(point.max, 2)} ${metricInfo(series.metric)?.unit || ""}`]),
        `有效 ${count(point.valid_count)} / 已读 ${count(point.observed_count)} / 预期 ${count(point.expected_count)}`,
        timeQuality(point),
        `配置 ${ref(point.source_ref)} · 启动 ${ref(point.boot_ref)}`,
        `接收 ${stamp(point.received_at, time)}`,
    ];
    const coverage = object(point.coverage), detection = object(point.event_detection);
    if (Object.keys(coverage).length) {
        lines.push(`有效音频 ${numericText(coverage.valid_duration_ms / 1000, 1)}秒 · 可用1秒窗口 ${count(coverage.valid_1s_count)}个`);
        lines.push(`PCM有效／已读／预期 ${count(coverage.valid_samples)} / ${count(coverage.observed_samples)} / ${count(coverage.expected_samples)}`);
        lines.push(`DMA丢样 ${count(coverage.dma_overflow_count)}次溢出 · 削波采样 ${count(coverage.clipped_samples)}`);
    }
    if (Object.keys(detection).length) {
        lines.push(detection.detector_state === "ready" && detection.valid_duration_ms >= 60000
            ? `事件检测 ${count(detection.detected_count)}次 · 保存 ${count(detection.stored_count)} · 合并 ${count(detection.merged_count)} · 丢弃 ${count(detection.dropped_count)}`
            : "事件检测覆盖不足或暖机中，不能当作0次事件");
    }
    const quality = qualityText(point.quality);
    if (quality) lines.push(quality);
    if (["missing", "invalid", "partial", "warming"].includes(point.channel_state)) lines.push(`通道：${stateLabel(point.channel_state)}`);
    return lines;
}

const sensor = (value) => ({"dfrobot-sen0528-aht20":"AHT20 SEN0528", "dfrobot-sen0228-veml7700":"VEML7700 SEN0228", "spm1423":"SPM1423"})[value] || "型号未知";
function thresholdText(value) {
    const config = object(value);
    if (!Object.keys(config).length) return "配置未知";
    return `基线 ${numericText(config.event_baseline_seconds)}秒；启动高于基线 ${numericText(config.event_start_delta_db, 1)}dB；停止差值 ${numericText(config.event_stop_delta_db, 1)}dB、持续 ${numericText(config.event_stop_seconds, 1)}秒；合并间隔 ${numericText(config.event_merge_gap_seconds, 1)}秒`;
}

function environmentEventDetail(event, time, source, minute) {
    const root = el("div", "environment-event-detail"), metrics = object(event.metrics);
    add(root, facts([
        ["事件包络", `${stamp(event.start_at, time)} — ${stamp(event.end_at, time)}`],
        ["实际活跃时长", `${numericText(metrics.active_duration_ms / 1000, 1)}秒`],
        ["最高1秒声音／基线", `${numericText(metrics.max_1s_rms_dbfs, 1)} / ${numericText(metrics.baseline_dbfs, 1)} dBFS`],
        ["时间映射", timeQuality(event)],
        ["接收时刻", stamp(event.received_at, time)],
        ["配置／启动", `${ref(event.source_ref)} / ${ref(event.boot_ref)}`],
        ["检测阈值", thresholdText(object(source).event_detection)],
        ["摘要状态", [event.merged ? "包含合并事件" : "独立事件", event.incomplete ? "记录不完整" : "", event.continued_from_previous || event.continues_next ? "跨摘要延续" : ""].filter(Boolean).join("；")],
    ]), el("p", "environment-note", "包络不是持续声音；区间内可能有安静间隔。该摘要不能确认声源或说明睡眠受到影响。"));
    const quality = qualityText(event.quality);
    if (quality) root.append(el("p", "environment-note", quality));
    const detection = object(object(minute).event_detection);
    if (Object.keys(detection).length) root.append(el("p", "environment-note",
        detection.detector_state === "ready" && detection.valid_duration_ms >= 60000
            ? `起始分钟检测／保存／合并／丢弃：${count(detection.detected_count)} / ${count(detection.stored_count)} / ${count(detection.merged_count)} / ${count(detection.dropped_count)}。这是整分钟计数，不是本事件独立计数。`
            : "起始分钟检测覆盖不足或暖机中，不能当作0次事件。"));
    else root.append(el("p", "environment-note", "当前返回窗口没有该事件起始分钟的检测计数；不据此填0。"));
    return root;
}

function environmentStatusPanel(value, time, onCapture) {
    const data = object(value), root = el("section", "card environment-status");
    root.id = "environment-status-panel";
    root.dataset.state = Object.hasOwn(STATES, data.state) ? data.state : "unavailable";
    add(root, el("h2", "", "环境设备与采集状态"), el("p", "environment-state", stateLabel(data.state)));
    const global = object(data.global_time_state);
    root.append(facts([
        ["历史记录／已索引", `${count(global.source_record_count)} / ${count(global.current_index_record_count)}`],
        ["未知／冲突时间", `${count(global.unknown_record_count)} / ${count(global.conflict_record_count)}`],
        ["待更新索引", count(global.stale_source_record_count)],
        ["当前是否采集", "未知；接收记录与最近设备快照均不代表实时在线"],
    ]));
    if (data.state === "index_not_initialized") root.append(el("p", "environment-note", "需要显式启用时间索引。刷新页面不会迁移或修复数据库。"));
    if (data.state === "unavailable") root.append(el("p", "environment-warning", "环境状态当前不可读取；其他数据来源继续可用。"));
    if (data.devices_truncated) root.append(el("p", "environment-warning", "设备或启动周期数量超出本次状态查询上限，以下不是全部历史。"));
    for (const device of array(data.devices, 100)) {
        const latest = object(device.last_received_snapshot), capture = object(device.last_mapped_capture);
        const item = add(el("article", "environment-device"), el("h3", "", `设备 ${ref(device.device_ref)}`), facts([
            ["接收证据", stateLabel(device.receive_state)],
            ["最近接收", stamp(device.last_received_at, time)],
            ["最近可映射采集", `${stamp(capture.utc_end, time)} · ${stateLabel(capture.time_quality)}`],
            ["设备上传授权", device.enabled === true ? "已启用" : device.enabled === false ? "未启用" : "未知"],
            ["最近缓存快照", stamp(latest.received_at, time)],
            ["待传／隔离记录", `${count(latest.queued_records)} / ${count(latest.quarantined_count)}`],
        ]));
        const lost = object(latest.lost_counts);
        if (Object.keys(lost).length) item.append(facts(Object.entries(lost)
            .filter(([key]) => ["minute", "event", "anchor", "status"].includes(key))
            .map(([key, value]) => [{minute:"丢失分钟",event:"丢失事件",anchor:"丢失锚点",status:"丢失状态"}[key],count(value)])));
        const profile = object(device.last_measurement_profile);
        if (Object.keys(profile).length) item.append(facts([
            ["最近配置", ref(profile.source_ref)], ["固件版本", version(profile.firmware_version)],
            ["传感器", `${sensor(profile.temperature_sensor)} / ${sensor(profile.light_sensor)} / ${sensor(profile.microphone)}`],
        ]));
        const channels = object(device.channel_health || device.latest_channel_health);
        if (Object.keys(channels).length) item.append(facts(Object.entries(channels)
            .filter(([key]) => metricInfo(key))
            .map(([key, value]) => [metricLabel(key), `${stateLabel(value?.state)}；有效／已读／预期 ${count(value?.valid_count)} / ${count(value?.observed_count)} / ${count(value?.expected_count)}；${qualityText(value?.quality) || "无额外质量标记"}`])));
        const quality = qualityText(latest.quality);
        if (quality) item.append(el("p", "environment-note", quality));
        if (onCapture && Number.isFinite(Date.parse(capture.utc_end)))
            item.append(button("查看最近采集窗口", () => onCapture(capture.utc_end), "environment-open-capture"));
        item.append(el("p", "environment-footnote", "缓存、丢弃和通道状态来自最后收到的证据，不能推断当前仍在采集或当前队列已清空。"));
        root.append(item);
    }
    return root;
}

export { environmentPanel, environmentStatusPanel, environmentEventDetail, environmentPointLines };
