/* Presentation metadata comes from the server registry; this module owns no metric list. */
const registry = new Map();
const word = (value, max) => typeof value === "string" && value.length <= max &&
    !/[<>\u0000-\u001f\u007f]/.test(value);
const finite = (value) => typeof value === "number" && Number.isFinite(value);

function configureMetrics(payload) {
    const values = Array.isArray(payload?.items) ? payload.items : [];
    const next = new Map();
    for (const item of values.slice(0, 32)) {
        if (!item || typeof item !== "object" ||
            !/^[a-z][a-z0-9_]{0,39}$/.test(item.id || "") ||
            !word(item.label, 40) || !word(item.unit, 16) ||
            !["garmin", "environment"].includes(item.source) ||
            !/^#[0-9a-fA-F]{6}$/.test(item.color || "")) continue;
        const axis = {};
        for (const key of ["min", "max"])
            if (finite(item.axis?.[key])) axis[key] = item.axis[key];
        next.set(item.id, {
            id: item.id, label: item.label, unit: item.unit, source: item.source,
            color: item.color, default_visible: item.default_visible === true,
            precision: Number.isInteger(item.precision) && item.precision >= 0 && item.precision <= 3
                ? item.precision : 1,
            aggregation: item.aggregation === "minute" ? "minute" : "sample",
            series_types: Array.isArray(item.series_types)
                ? item.series_types.filter((value) => /^[a-z_]{1,30}$/.test(value)).slice(0, 8) : [],
            axis,
        });
    }
    if (!next.size) throw new Error("METRIC_REGISTRY_UNAVAILABLE");
    registry.clear();
    for (const [key, value] of next) registry.set(key, Object.freeze(value));
}
function metricInfo(id) { return registry.get(id); }
function metricItems() { return [...registry.values()]; }
function validSeriesID(value) {
    if (typeof value !== "string" || value.length > 220 || !/^[a-z0-9_:-]+$/.test(value)) return false;
    const parts = value.split(":"), [metric, type] = parts;
    const item = registry.get(metric);
    if (!item || !/^[a-z_]{1,30}$/.test(type || "") ||
        (item.series_types.length && !item.series_types.includes(type))) return false;
    return item.source === "environment"
        ? parts.length === 4 && /^source-[a-f0-9]{24}$/.test(parts[2]) && /^boot-[a-f0-9]{24}$/.test(parts[3])
        : parts.length === 2;
}
function defaultVisible(series) {
    return typeof series.default_visible === "boolean"
        ? series.default_visible : registry.get(series.metric)?.default_visible === true;
}
function metricColor(id) { return registry.get(id)?.color || "#8b9ca5"; }
function metricLabel(id) { return registry.get(id)?.label || "未知指标"; }
function registeredSeries(values) {
    return (Array.isArray(values) ? values : []).filter((item) =>
        item && validSeriesID(item.id) && registry.has(item.metric) && item.id.split(":")[0] === item.metric,
    ).slice(0, 80);
}

export { configureMetrics, metricInfo, metricItems, validSeriesID, defaultVisible,
    metricColor, metricLabel, registeredSeries };
