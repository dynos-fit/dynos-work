/**
 * POST helpers for dynos-work dashboard API.
 * Each helper calls fetch with POST method and JSON body,
 * appending ?project=<project> to the URL.
 */

import type { PolicyConfig, AutofixPolicyConfig } from "./types";

interface SaveResponse {
  ok: boolean;
}

interface DaemonResponse {
  ok: boolean;
  stdout?: string;
  stderr?: string;
}

function apiUrl(endpoint: string, project: string): string {
  return `/api/${endpoint}?project=${encodeURIComponent(project)}`;
}

async function postJson<T>(url: string, data: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return res.json() as Promise<T>;
}

/**
 * Save the main policy configuration for a project.
 */
export async function savePolicy(
  project: string,
  data: PolicyConfig,
): Promise<SaveResponse> {
  return postJson<SaveResponse>(apiUrl("policy", project), data);
}

/**
 * Save the autofix policy configuration for a project.
 */
export async function saveAutofixPolicy(
  project: string,
  data: AutofixPolicyConfig,
): Promise<SaveResponse> {
  return postJson<SaveResponse>(apiUrl("autofix-policy", project), data);
}

/**
 * Trigger a daemon action (start, stop, run-once, etc.) for a project.
 */
export async function daemonAction(
  project: string,
  action: string,
  taskDir?: string,
): Promise<DaemonResponse> {
  const payload: Record<string, string> = {};
  if (taskDir !== undefined) {
    payload.taskDir = taskDir;
  }
  return postJson<DaemonResponse>(apiUrl(`daemon/${encodeURIComponent(action)}`, project), payload);
}
