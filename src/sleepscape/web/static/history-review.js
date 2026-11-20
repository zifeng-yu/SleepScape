import { add, el } from "./dom.js";
import { duration, number, numericText } from "./format.js";
import { FEELING_LABELS } from "./morning-feeling.js";

export function feelingUnavailable(state) {
    return state === "not_prepared" ? "晨起睡感尚未启用" : "晨起睡感暂不可读取";
}

function clockTime(seconds) {
    const day = Math.floor(seconds / 86400);
    const minute = Math.floor((seconds - day * 86400) / 60);
    const hours = String(Math.floor(minute / 60)).padStart(2, "0");
    const minutes = String(minute % 60).padStart(2, "0");
    const prefix = day === -1 ? "前日 " : day < -1 ? `前${-day}日 ` : "";
    return `${prefix}${hours}:${minutes}`;
}

export function historyReview(review) {
    const section = add(
        el("section", "card history-review"),
        el("h2", "history-review-title", "7 日睡眠复盘"),
    );
    const ratings = add(
        el("div", "history-review-ratings"),
        el("h3", "", "晨起睡感"),
    );
    if (review.feeling_state === "available") {
        const counts = el("ul", "history-rating-counts");
        for (const [key, label] of [...FEELING_LABELS, ["unfilled", "未填写"]]) {
            counts.append(el("li", "", `${label} ${review.feeling_counts[key]}晚`));
        }
        ratings.append(counts);
    } else {
        ratings.append(el("p", "history-review-note", feelingUnavailable(review.feeling_state)));
    }
    const metrics = el("dl", "history-review-metrics");
    for (const [key, label, format] of [
        ["sleep_start_offset_seconds", "入睡", clockTime],
        ["sleep_end_offset_seconds", "醒来", clockTime],
        ["sleep_seconds", "主睡眠时长", duration],
        ["average_heart_rate", "夜间平均心率", (value) => `${numericText(value, 1)} 次/分`],
        ["night_average_hrv", "昨夜 HRV", (value) => `${numericText(value, 1)} ms`],
    ]) {
        const { value, valid_n: valid } = review.medians[key];
        const caption = valid === 1 ? "单晚值" : "中位数";
        metrics.append(add(
            el("div", "history-review-metric"),
            el("dt", "", label),
            add(
                el("dd"),
                el("strong", "", number(value) ? format(value) : "暂无记录"),
                el("span", "", `${valid ? `${caption} · ` : ""}${valid}/${review.night_count} 晚`),
            ),
        ));
    }
    return add(section, ratings, metrics);
}
