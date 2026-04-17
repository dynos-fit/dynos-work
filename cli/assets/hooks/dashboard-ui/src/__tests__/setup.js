/**
 * Vitest global test setup.
 * Mocks fetch, localStorage, and ResizeObserver for all tests.
 */
import { vi, beforeEach, afterEach } from "vitest";
// ---- Global fetch mock ----
// Each test file should configure fetch responses via mockFetchResponses()
const fetchMock = vi.fn();
globalThis.fetch = fetchMock;
export { fetchMock };
/**
 * Helper: configure fetch mock to return specific responses by URL pattern.
 * Usage:
 *   mockFetchResponses({ '/api/tasks': { ok: true, json: async () => [...] } })
 */
export function mockFetchResponses(map) {
    fetchMock.mockImplementation(async (url) => {
        const urlStr = typeof url === "string" ? url : url instanceof URL ? url.toString() : url.url;
        for (const [pattern, response] of Object.entries(map)) {
            if (urlStr.includes(pattern)) {
                return {
                    ok: response.ok ?? true,
                    status: response.status ?? (response.ok === false ? 500 : 200),
                    json: response.json ?? (async () => ({})),
                    text: response.text ?? (async () => ""),
                };
            }
        }
        return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
    });
}
/**
 * Helper: create a simple successful JSON response for fetch mock.
 */
export function jsonResponse(data) {
    return { ok: true, status: 200, json: async () => data };
}
/**
 * Helper: create an error response for fetch mock.
 */
export function errorResponse(status, body = {}) {
    return { ok: false, status, json: async () => body };
}
// ---- localStorage mock ----
const localStorageMock = (() => {
    let store = {};
    return {
        getItem: vi.fn((key) => store[key] ?? null),
        setItem: vi.fn((key, value) => {
            store[key] = value;
        }),
        removeItem: vi.fn((key) => {
            delete store[key];
        }),
        clear: vi.fn(() => {
            store = {};
        }),
        get length() {
            return Object.keys(store).length;
        },
        key: vi.fn((index) => Object.keys(store)[index] ?? null),
    };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock });
export { localStorageMock };
// ---- ResizeObserver mock (needed by Recharts ResponsiveContainer) ----
class ResizeObserverMock {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
}
globalThis.ResizeObserver = ResizeObserverMock;
// ---- matchMedia mock (needed by some responsive components) ----
Object.defineProperty(globalThis, "matchMedia", {
    value: vi.fn().mockImplementation((query) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
    })),
});
// ---- Reset mocks between tests ----
beforeEach(() => {
    fetchMock.mockReset();
    localStorageMock.clear();
    vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
});
