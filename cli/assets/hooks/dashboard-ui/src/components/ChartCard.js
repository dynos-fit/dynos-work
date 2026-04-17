import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function ChartCard({ title, subtitle, action, children }) {
    return (_jsxs("div", { className: "border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5", children: [_jsxs("div", { className: "flex items-center justify-between mb-4", children: [_jsxs("div", { children: [_jsx("div", { className: "section-label", children: title }), subtitle && (_jsx("p", { className: "text-[10px] text-[#5A574E] mt-1", children: subtitle }))] }), action] }), children] }));
}
