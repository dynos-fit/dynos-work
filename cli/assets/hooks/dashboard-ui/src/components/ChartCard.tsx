import { type ReactNode } from "react";

interface ChartCardProps {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}

export function ChartCard({ title, subtitle, action, children }: ChartCardProps) {
  return (
    <div className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <div className="section-label">{title}</div>
          {subtitle && (
            <p className="text-[10px] text-[#5A574E] mt-1">{subtitle}</p>
          )}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}
