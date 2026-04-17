import { useState } from "react";

const RANGES = ["7d", "30d", "90d", "All"] as const;
export type TimeRange = (typeof RANGES)[number];

interface TimeRangeFilterProps {
  value: TimeRange;
  onChange: (range: TimeRange) => void;
}

export function TimeRangeFilter({ value, onChange }: TimeRangeFilterProps) {
  return (
    <div className="flex gap-1" role="group" aria-label="Time range filter">
      {RANGES.map((range) => (
        <button
          key={range}
          onClick={() => onChange(range)}
          className={`px-3 py-1 text-[10px] font-medium tracking-wider uppercase rounded-full transition-all duration-150 ${
            value === range
              ? "bg-[#BDF000] text-black"
              : "bg-[#2A2A2A] text-[#7A776E] hover:text-[#C8C4B8] hover:bg-[#333]"
          }`}
          aria-pressed={value === range}
        >
          {range}
        </button>
      ))}
    </div>
  );
}

/** Filter data array by time range. Items need a date field. */
export function filterByTimeRange<T>(
  items: T[],
  dateAccessor: (item: T) => string | null | undefined,
  range: TimeRange,
): T[] {
  if (range === "All") return items;
  const days = range === "7d" ? 7 : range === "30d" ? 30 : 90;
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  return items.filter((item) => {
    const d = dateAccessor(item);
    if (!d) return false;
    return new Date(d).getTime() >= cutoff;
  });
}

export { RANGES };
