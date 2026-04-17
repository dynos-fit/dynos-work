import { motion } from "motion/react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";

interface MetricCardProps {
  label: string;
  value: string | number;
  trend?: number | null; // percentage change, positive = up
  trendLabel?: string; // e.g. "vs last period"
  icon?: React.ReactNode;
  delay?: number;
}

export function MetricCard({ label, value, trend, trendLabel = "vs last period", icon, delay = 0 }: MetricCardProps) {
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

  return (
    <motion.div
      className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5 card-hover-glow"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.28, ease: "easeOut" }}
    >
      <div className="flex items-center gap-1.5 mb-3">
        {icon}
        <span className="text-[10px] text-[#7A776E] tracking-[0.12em] uppercase">
          {label}
        </span>
      </div>
      <div className="text-[28px] font-bold text-[#F0F0E8] leading-none" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
        {value}
      </div>
      {trend !== undefined && trend !== null && (
        <div className="flex items-center gap-1.5 mt-2">
          <TrendIcon className={`w-3 h-3 ${trendColor}`} />
          <span className={`text-[11px] font-medium ${trendColor}`}>
            {trend > 0 ? "+" : ""}{trend.toFixed(1)}%
          </span>
          <span className="text-[10px] text-[#5A574E]">{trendLabel}</span>
        </div>
      )}
    </motion.div>
  );
}
