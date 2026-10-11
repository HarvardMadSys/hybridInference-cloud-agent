export const ADMIN_PAGE = 'mx-auto w-full max-w-[1440px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8';

export function AdminPageHeader({
  section,
  description,
}: {
  section: string;
  description: string;
}) {
  return (
    <header className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">Admin</h1>
        <span className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted">
          {section}
        </span>
      </div>
      <p className="max-w-2xl text-sm leading-relaxed text-muted">{description}</p>
    </header>
  );
}
