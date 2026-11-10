/* Safe DOM primitives; supplied values are rendered as text, never HTML. */
import { text, STATE_NAMES, stateName } from "./format.js";

const $ = (selector, root = document) => root.querySelector(selector);
const el = (tag, className = "", value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    // Empty text is intentional for icon buttons and unitless metrics.
    if (value !== undefined) node.textContent = value === "" ? "" : text(value);
    return node;
};
const add = (parent, ...children) => {
    children.filter(Boolean).forEach((child) => parent.append(child));
    return parent;
};
const button = (label, callback, className = "") => {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", callback);
    return node;
};
const ICONS = {
    moon: "M20.5 14A8.5 8.5 0 0 1 10 3.5 8.5 8.5 0 1 0 20.5 14Z",
    heart: "M20.5 5.5a5 5 0 0 0-7 0L12 7l-1.5-1.5a5 5 0 0 0-7 7L12 21l8.5-8.5a5 5 0 0 0 0-7Z",
    hrv: "M2 12h5l3-8 4 16 3-8h5",
    load: "m13 2-9 12h7l-1 8 10-13h-7l1-7Z",
    left: "m14 6-6 6 6 6",
    right: "m10 6 6 6-6 6",
    clock: "M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
};
function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", ICONS[name] || ICONS.clock);
    svg.append(path);
    return svg;
}
const chip = (value, label) =>
    el(
        "span",
        `chip ${Object.hasOwn(STATE_NAMES, value) ? value : "missing"}`,
        label || stateName(value),
    );

export { $, el, add, button, icon, chip };
