export const ADMIN_PAGE = 'mx-auto w-full max-w-[1440px] px-4 py-4 sm:px-5 lg:px-6';

export function AdminPageHeader({
  section,
  description,
}: {
  section: string;
  description: string;
}) {
  return (
    <header>
      <h1 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-accent">Admin</h1>
      <p className="mt-0.5 text-xl font-semibold tracking-[-0.015em] text-foreground">{section}</p>
      <p className="mt-1 max-w-3xl text-[13px] leading-5 text-muted">{description}</p>
    </header>
  );
}
