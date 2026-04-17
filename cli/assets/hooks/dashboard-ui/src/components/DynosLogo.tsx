import { motion } from "motion/react";

/**
 * DynosLogo — Exact replica of the LiftingApp dynos logo.
 * Three staggered lime bars + "dynos" wordmark + tagline.
 */

const BAR_COLOR = "#BDF000";
const BAR_RADIUS = 3;

const BARS = [
  { x: 16, y: 22, width: 12, height: 56, opacity: 1.0, delay: 0.2 },
  { x: 34, y: 10, width: 12, height: 80, opacity: 1.0, delay: 0.35 },
  { x: 52, y: 30, width: 12, height: 42, opacity: 0.55, delay: 0.5 },
];

export const DynosLogo = ({ className = "" }: { className?: string }) => {
  return (
    <div className={`flex flex-col items-center gap-4 ${className}`}>
      {/* Ambient purple glow */}
      <div
        className="absolute -top-20 left-1/2 -translate-x-1/2 w-64 h-64 rounded-full blur-3xl pointer-events-none"
        style={{ background: "radial-gradient(circle, rgba(110, 40, 200, 0.25), transparent 70%)" }}
        aria-hidden="true"
      />

      {/* Logo mark: three staggered bars */}
      <svg
        viewBox="0 0 80 100"
        className="w-20 h-24 relative z-10"
        xmlns="http://www.w3.org/2000/svg"
        role="img"
        aria-label="Dynos logo"
      >
        {BARS.map((bar, i) => (
          <motion.rect
            key={i}
            x={bar.x}
            y={bar.y}
            width={bar.width}
            height={bar.height}
            rx={BAR_RADIUS}
            ry={BAR_RADIUS}
            fill={BAR_COLOR}
            opacity={bar.opacity}
            initial={{ scaleY: 0, opacity: 0 }}
            animate={{ scaleY: 1, opacity: bar.opacity }}
            transition={{
              delay: bar.delay,
              duration: 0.5,
              ease: [0.34, 1.56, 0.64, 1], // spring-like overshoot
            }}
            style={{ transformOrigin: `${bar.x + bar.width / 2}px ${bar.y + bar.height}px` }}
          />
        ))}
      </svg>

      {/* Wordmark */}
      <motion.span
        className="text-3xl font-bold text-[#F0F0E8] tracking-wider relative z-10"
        style={{ fontFamily: "'Space Grotesk', sans-serif" }}
        initial={{ opacity: 0, letterSpacing: "0.5em" }}
        animate={{ opacity: 1, letterSpacing: "0.15em" }}
        transition={{ delay: 0.7, duration: 0.8, ease: "easeOut" }}
      >
        dynos
      </motion.span>

      {/* Tagline */}
      <motion.span
        className="text-[11px] font-medium uppercase tracking-[0.2em] text-[#BDF000]/50 relative z-10"
        style={{ fontFamily: "'Space Grotesk', sans-serif" }}
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 1.0, duration: 0.5, ease: "easeOut" }}
      >
        Autonomous Dev System
      </motion.span>
    </div>
  );
};
