import { api } from "./api.js";
import { add, button, el } from "./dom.js";

export const FEELING_LABELS = new Map([["poor", "较差"], ["fair", "一般"], ["good", "较好"]]);

// Revisiting a date waits for its outstanding write before reading the saved value.
const pendingWrites = new Map();

export function morningFeeling(day, signal) {
    const group = el("div", "morning-feeling");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "晨起睡感");
    const status = el("span", "feeling-status");
    status.setAttribute("role", "status");
    const path = `nights/${day}/morning-feeling`;
    const choices = new Map();
    function busy(value) {
        for (const control of choices.values()) control.disabled = value;
    }
    function display(record) {
        for (const [value, control] of choices)
            control.setAttribute("aria-pressed", String(value === record?.feeling));
    }
    async function save(feeling) {
        busy(true);
        status.textContent = "保存中";
        try {
            const writing = api(path, undefined, {
                method: "PUT",
                referrerPolicy: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ feeling }),
            });
            pendingWrites.set(day, writing);
            const record = await writing;
            if (signal.aborted) return;
            display(record);
            status.textContent = "已保存";
        } catch {
            if (!signal.aborted) status.textContent = "保存未确认，请重试";
        } finally {
            pendingWrites.delete(day);
            busy(false);
        }
    }
    group.append(el("span", "", "晨起睡感"));
    for (const [value, label] of FEELING_LABELS) {
        const control = button(label, () => save(value), "feeling-choice");
        choices.set(value, control);
        group.append(control);
    }
    add(group, status);
    busy(true);
    Promise.resolve(pendingWrites.get(day)).catch(() => {}).then(() => api(path, signal)).then(record => {
        if (signal.aborted) return;
        display(record);
        busy(false);
    }).catch(() => {
        if (!signal.aborted) status.textContent = "读取失败，请刷新";
    });
    return group;
}
