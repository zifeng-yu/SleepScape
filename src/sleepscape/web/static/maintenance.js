/* Read-only maintenance metadata; never display paths, upstream text or backup contents. */
import { add, el } from "./dom.js";
import { createTimeFormatter } from "./format.js";

const RUN_STATES = {
    running: "正在维护",
    completed: "本轮维护已完成",
    partial: "本轮维护部分完成",
    failed: "本轮维护失败",
    interrupted: "维护已中断",
    blocked: "维护暂未执行",
};
const PHASES = {
    preparing: "检查备份条件",
    backup: "备份与恢复核验",
    export: "核验并导出副本",
    retention: "整理受控备份",
    finished: "本轮结束",
};
const DISK_STATES = {
    normal: "空间正常",
    warning: "空间预警",
    restricted: "空间不足，限制大任务",
    critical: "空间临界，暂停新数据写入",
    unknown: "空间状态未确认",
};
const ERRORS = {
    maintenance_state_unavailable: "维护记录暂不可用，请检查本机状态",
    maintenance_already_running: "已有维护任务正在运行",
    maintenance_interrupted: "上次维护意外中断",
    previous_run_interrupted: "上次维护意外中断",
    maintenance_failed: "本轮维护未完成，请检查运行记录",
    maintenance_source_busy: "采集或模型任务正在运行，等待下一次维护",
    maintenance_source_unavailable: "源数据暂不可用，已有备份保留",
    maintenance_bundle_invalid: "备份包核验未通过，已有副本保留",
    maintenance_retention_failed: "备份整理未完成，需检查受控清单",
    maintenance_export_failed: "副本导出未成功，本地备份保留",
    maintenance_capacity_blocked: "磁盘条件不满足，未开始新备份",
    backup_failed: "创建备份未成功，已有副本保留",
    backup_restore_failed: "恢复核验未通过，已有副本保留",
    backup_source_unavailable: "没有可用的源数据库",
    backup_manifest_unavailable: "备份清单暂不可用",
    backup_integrity_failed: "备份完整性核验未通过",
    backup_retention_failed: "备份整理未完成，需检查受控清单",
    insufficient_disk_space: "磁盘空间不足，未开始新备份",
    disk_space_restricted: "磁盘空间不足，未开始新备份",
    disk_space_critical: "磁盘空间临界，写入已受保护",
    disk_usage_unavailable: "无法核验磁盘空间，任务已暂停",
    capacity_unavailable: "无法核验磁盘空间，任务已暂停",
    capacity_restricted: "磁盘空间不足，未开始新备份",
    capacity_critical: "磁盘空间临界，写入已受保护",
    storage_capacity_unavailable: "无法核验磁盘空间，任务已暂停",
    storage_capacity_restricted: "磁盘空间不足，未开始新备份",
    storage_capacity_critical: "磁盘空间临界，写入已受保护",
    backup_space_insufficient: "可用空间不足以安全生成新备份",
    collection_already_running: "采集正在运行，等待下一次维护",
    model_already_running: "模型任务正在运行，等待下一次维护",
    offsite_export_failed: "副本导出未成功，本地备份保留",
    offsite_verification_failed: "目标副本核验未通过，本地备份保留",
    offsite_configuration_invalid: "备份目标配置需要检查",
};
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const known = (table, value, fallback) => typeof value === "string" && Object.hasOwn(table, value) ? table[value] : fallback;
const count = (value, limit = 1000000) => Number.isSafeInteger(value) && value >= 0 && value <= limit;
const stamp = (value) => typeof value === "string" && value.length <= 40 &&
    /^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(value) &&
    Number.isFinite(Date.parse(value));

function bytes(value) {
    if (!count(value, Number.MAX_SAFE_INTEGER)) return null;
    if (value >= 1073741824) return `${(value / 1073741824).toFixed(2)} GiB`;
    if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MiB`;
    if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${value} B`;
}

function facts(rows) {
    const list = el("dl", "analysis-facts maintenance-facts");
    for (const [label, value] of rows) add(list, el("dt", "", label), el("dd", "", value));
    return list;
}

function offsiteLabel(value) {
    if (value.status === "offsite_not_configured" || value.mode === "none") return "尚未配置";
    if (value.status === "failed") return "副本导出或核验失败";
    if (value.status === "verified") {
        if (value.mode === "ssh") return "SSH 目标副本已核验";
        if (value.mode === "path") return "目标副本已核验，离机位置未核验";
    }
    if (value.status === "copied_unverified_location") return "目标副本已核验，离机位置未核验";
    if (value.status === "not_checked") return "已配置，尚未完成目标核验";
    return "目标备份状态未确认";
}

function maintenanceStatusPanel(payload) {
    const data = object(payload), panel = el("section", "card analysis-card maintenance-card"),
        time = createTimeFormatter("Asia/Shanghai");
    panel.id = "maintenance-status-panel";
    panel.setAttribute("aria-label", "自动维护与备份");
    panel.setAttribute("aria-live", "polite");
    add(panel, el("h2", "", "自动维护与备份"));
    if (!["available", "not_run", "unavailable"].includes(data.state)) {
        panel.append(el("p", "analysis-note", "暂无维护信息，其他已保存数据仍可查看。"));
        return panel;
    }
    const schedule = object(data.schedule), rows = [];
    rows.push(["计划时间", schedule.timezone === "Asia/Shanghai" && schedule.time === "04:00"
        ? "每天 04:00 · 上海时间" : "计划信息暂不可用"]);
    rows.push(["定时器安装", "未核验；计划时间不代表服务器已启用"]);
    if (stamp(schedule.next_planned_at)) rows.push(["下次计划时间", time.dateTime(schedule.next_planned_at, true)]);
    const latest = object(data.latest);
    rows.push(["最近维护", data.state === "unavailable" ? "维护记录暂不可用"
        : data.state === "not_run" ? "尚未运行" : known(RUN_STATES, latest.status, "维护状态未确认")]);
    if (data.state === "available") {
        if (latest.phase) rows.push(["当前步骤", known(PHASES, latest.phase, "步骤状态未确认")]);
        if (stamp(latest.started_at)) rows.push(["开始时间", time.dateTime(latest.started_at, true)]);
        if (stamp(latest.finished_at)) rows.push(["结束时间", time.dateTime(latest.finished_at, true)]);
        if (latest.error_code) rows.push(["运行提示", known(ERRORS, latest.error_code, "本轮维护未完成，请检查运行记录")]);
    }
    panel.append(facts(rows));
    const backup = object(data.backups), backupRows = [];
    if (data.state !== "unavailable") {
        if (count(backup.verified_count)) backupRows.push(["受控已核验副本", `${backup.verified_count} 份`]);
        if (bytes(backup.total_bytes) !== null) backupRows.push(["受控副本总量", bytes(backup.total_bytes)]);
        if (stamp(backup.latest_verified_at)) backupRows.push(["最近备份核验", time.dateTime(backup.latest_verified_at, true)]);
        if (count(backup.legacy_count) && backup.legacy_count > 0)
            backupRows.push(["保留的手动备份", `${backup.legacy_count} 份，不参与自动清理`]);
        if (backup.retention_over_budget === true)
            backupRows.push(["保留预算", "超出预算；最新有效副本仍受保护"]);
        if (backupRows.length) add(panel, el("h3", "", "本地备份"), facts(backupRows));
    } else panel.append(el("p", "analysis-note", "无法确认备份清单；不会因刷新页面执行修复、删除或恢复。"));
    const disk = object(data.disk), diskRows = [["磁盘状态", known(DISK_STATES, disk.state, DISK_STATES.unknown)]];
    if (stamp(disk.measured_at)) diskRows.push(["空间检查时间", time.dateTime(disk.measured_at, true)]);
    const seenRoles = new Set();
    for (const entry of (Array.isArray(disk.volumes) ? disk.volumes : []).slice(0, 2)) {
        const volume = object(entry);
        if (!["system", "data"].includes(volume.role) || seenRoles.has(volume.role)) continue;
        const label = volume.role === "system" ? "系统分区" : "数据分区";
        seenRoles.add(volume.role);
        if (bytes(volume.free_bytes) !== null) diskRows.push([`${label}剩余`, bytes(volume.free_bytes)]);
        if (typeof volume.free_percent === "number" && Number.isFinite(volume.free_percent) && volume.free_percent >= 0 && volume.free_percent <= 100)
            diskRows.push([`${label}空闲比例`, `${volume.free_percent.toFixed(1)}%`]);
    }
    const sizes = object(disk.bytes);
    for (const [key, label] of [["database", "数据库文件"], ["wal", "待合并写入日志"], ["shm", "共享索引文件"], ["runtime", "任务运行记录"]])
        if (bytes(sizes[key]) !== null) diskRows.push([label, bytes(sizes[key])]);
    add(panel, el("h3", "", "容量保护"), facts(diskRows));
    if (disk.state === "restricted") panel.append(el("p", "analysis-note", "新的模型外呼和大备份任务暂停；正常小批环境接收优先保留。"));
    else if (disk.state === "critical") panel.append(el("p", "analysis-note", "环境上传暂不确认入库，设备保留待传数据；Garmin 暂停新写入，已有数据仍可读取。"));
    const offsite = object(data.offsite), offsiteRows = [["目标副本", offsiteLabel(offsite)]];
    if (stamp(offsite.last_success_at)) offsiteRows.push(["最近目标核验", time.dateTime(offsite.last_success_at, true)]);
    if (count(offsite.retained_count)) offsiteRows.push(["目标受控副本", `${offsite.retained_count} 份`]);
    if (bytes(offsite.retained_bytes) !== null) offsiteRows.push(["目标受控副本总量", bytes(offsite.retained_bytes)]);
    if (offsite.over_budget === true) offsiteRows.push(["目标保留预算", "超出预算；最新有效副本仍受保护"]);
    if (offsite.error_code) offsiteRows.push(["导出提示", known(ERRORS, offsite.error_code, "目标核验未完成，请检查备份配置")]);
    add(panel, el("h3", "", "离机备份"), facts(offsiteRows));
    if (offsite.mode === "path") panel.append(el("p", "analysis-scope", "路径副本是否位于另一台设备、存储是否加密，需要单独核验；复制成功不等于离机保护已建立。"));
    else if (offsite.mode === "ssh") panel.append(el("p", "analysis-scope", "SSH 传输经过加密；目标是否独立于当前主机、远端存储加密和长期可恢复性仍需部署验收。"));
    add(panel,
        el("p", "analysis-scope", "只整理程序管理的备份，保留最新有效副本；原始观测不作为清理对象。"),
        el("p", "analysis-scope", "核验记录表示该次检查通过，不保证文件此刻仍存在。页面只读，恢复须显式执行。"),
    );
    return panel;
}

export { maintenanceStatusPanel };
