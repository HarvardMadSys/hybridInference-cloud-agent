/** The app's mark: a terminal prompt on the accent colour. */
export function BrandMark({ className = 'h-[22px] w-[22px]' }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-md bg-accent text-accent-foreground ${className}`}
    >
      <svg
        className="h-3.5 w-3.5"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2.2}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m5 7 5 5-5 5m8 0h6" />
      </svg>
    </span>
  );
}
