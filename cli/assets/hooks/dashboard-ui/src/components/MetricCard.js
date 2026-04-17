import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { motion } from "motion/react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
export function MetricCard({ label, value, trend, trendLabel = "vs last period", icon, delay = 0 }) {
    const trendColor = trend === null || trend === undefined
        ? "text-[#7A776E]"
        : trend > 0
            ? "text-[#BDF000]"
            : trend < 0
                ? "text-[#FF3B3B]"
                : "text-[#7A776E]";
    const TrendIcon = trend === null || trend === undefined
        ? Minus
        : trend > 0
            ? TrendingUp
            : trend < 0
                ? TrendingDown
                : Minus;
    return (_jsxs(motion.div, { className: "border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5 card-hover-glow", initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay, duration: 0.28, ease: "easeOut" }, children: [_jsxs("div", { className: "flex items-center gap-1.5 mb-3", children: [icon, _jsx("span", { className: "text-[10px] text-[#7A776E] tracking-[0.12em] uppercase", children: label })] }), _jsx("div", { className: "text-[28px] font-bold text-[#F0F0E8] leading-none", style: { fontFamily: "'JetBrains Mono', monospace" }, children: value }), trend !== undefined && trend !== null && (_jsxs("div", { className: "flex items-center gap-1.5 mt-2", children: [_jsx(TrendIcon, { className: `w-3 h-3 ${trendColor}` }), _jsxs("span", { className: `text-[11px] font-medium ${trendColor}`, children: [trend > 0 ? "+" : "", trend.toFixed(1), "%"] }), _jsx("span", { className: "text-[10px] text-[#5A574E]", children: trendLabel })] }))] }));
}
