/**
 * POST helpers for dynos-work dashboard API.
 * Each helper calls fetch with POST method and JSON body,
 * appending ?project=<project> to the URL.
 */
function apiUrl(endpoint, project) {
    return `/api/${endpoint}?project=${encodeURIComponent(project)}`;
}
async function postJson(url, data) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
    });
    return res.json();
}
/**
 * Save the main policy configuration for a project.
 */
export async function savePolicy(project, data) {
    return postJson(apiUrl("policy", project), data);
}
/**
 * Trigger a daemon action (start, stop, run-once, etc.) for a project.
 */
export async function daemonAction(project, action, taskDir) {
    const payload = {};
    if (taskDir !== undefined) {
        payload.taskDir = taskDir;
    }
    return postJson(apiUrl(`daemon/${encodeURIComponent(action)}`, project), payload);
}
