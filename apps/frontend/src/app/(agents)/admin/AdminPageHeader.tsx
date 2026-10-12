export const ADMIN_PAGE = 'mx-auto w-full max-w-[1440px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8';

function AdminMark() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      className="h-5 w-5"
    >
      <rect x="4" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5" />
      <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5" />
      <rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5" />
    </svg>
  );
}

export function AdminPageHeader({
  section,
  description,
}: {
  section: string;
  description: string;
}) {
  return (
    <header className="overflow-hidden rounded-2xl border border-border bg-surface shadow-card">
      <div className="flex items-start gap-4 px-5 py-5 sm:px-6 sm:py-6">
        <span
          aria-hidden
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-accent-subtle text-accent"
        >
          <AdminMark />
        </span>
        <div className="min-w-0 pt-0.5">
          <h1 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-accent">
            Admin
          </h1>
          <p className="mt-1 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
            {section}
          </p>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">{description}</p>
        </div>
      </div>
    </header>
  );
}
