interface UploadProgressProps {
  fileName: string;
  progress: number; // 0..1
  label: string;
}

export function UploadProgress({ fileName, progress, label }: UploadProgressProps) {
  const percent = Math.round(progress * 100);
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="truncate font-medium">{fileName}</span>
        <span className="shrink-0 tabular-nums text-muted">{percent}%</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        className="h-2 overflow-hidden rounded-full bg-surface-muted"
      >
        <div
          className="h-full rounded-full bg-brand transition-[width] duration-300 ease-out"
          style={{ width: `${percent}%` }}
        />
      </div>
      <p className="text-xs text-muted">{label}</p>
    </div>
  );
}
