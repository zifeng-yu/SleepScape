import { add, el } from "./dom.js";

export function validInterpretation(content) {
    const prose = value => typeof value === "string" && value.length > 0;
    return prose(content?.summary?.text) && prose(content.garmin) && prose(content.environment)
        && (content.feeling === null || prose(content.feeling))
        && (content.comparison === null || prose(content.comparison))
        && Array.isArray(content.possible_reasons) && content.possible_reasons.every(prose);
}

export function appendInterpretation(body, content) {
    for (const [title, lines] of [
        ["睡得怎么样", [content.summary.text]],
        ["佳明记录", [content.garmin]],
        ["卧室环境", [content.environment]],
        ["晨起睡感", [content.feeling]],
        ["与前几晚对照", [content.comparison]],
        ["可能的原因", content.possible_reasons],
    ]) {
        if (!lines.some(Boolean)) continue;
        const section = add(el("section", "report-observation-group"), el("h3", "", title));
        for (const line of lines) section.append(el("p", "report-prose", line));
        body.append(section);
    }
}
