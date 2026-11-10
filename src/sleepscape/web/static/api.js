/* Same-origin, read-only API transport. Never stores health data or Garmin credentials. */
async function api(path, signal) {
    const response = await fetch(`/api/v1/${path}`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal,
    });
    if (!response.ok) {
        const error = new Error(`HTTP_${response.status}`);
        error.status = response.status;
        throw error;
    }
    return response.json();
}
function errorText(error) {
    if (error.status === 401 || error.status === 403)
        return "当前连接未获授权。请通过受保护的服务地址重新登录，然后刷新页面。";
    if (error.status === 422)
        return "日期或时间范围无效，请重新选择。自定义范围最多48小时。";
    return "暂时无法读取本地数据。请检查服务是否运行，再点右上角刷新。";
}

export { api, errorText };
