'use client';

// A composer control that actually selects something.
//
// These were static buttons: the row showed a repository, a branch, a runtime
// and a model, and the Run handler submitted its own hardcoded values. So the
// screen described one job and queued another — worse than an obviously
// disabled control, because nothing looked wrong.
//
// **The value is drawn, and the <select> sits invisibly on top of it.** A bare
// native select is as wide as its *widest option*, not its selected one, so the
// model picker rendered `glm-5.3` followed by a hand's width of blank — sized
// for `kimi-k2.7-code-highspeed`, which the user had not chosen. Every option
// list here is short enough that the gap read as a layout bug rather than as a
// menu that happens to contain long names. The select keeps its options, its
// keyboard behaviour and its accessible name; only the painting is ours, which
// is why the chevron below is drawn too — the native one goes with the box.
export function Picker({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (next: string) => void;
}) {
  if (options.length === 0) {
    return null;
  }
  return (
    <label
      title={`${label}: ${value}`}
      className="relative inline-flex min-h-10 max-w-full items-center gap-2 rounded-lg border border-transparent px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-hover focus-within:border-accent/30 focus-within:bg-surface focus-within:ring-2 focus-within:ring-accent/15 [@media(pointer:coarse)]:min-h-11"
    >
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-[10px] font-normal leading-none text-muted">{label}</span>
        {/* The width of the control follows the selection. Long identifiers
            truncate so the setting and its label stay inside narrow panes. */}
        <span aria-hidden className="min-w-0 max-w-[13rem] truncate leading-4">
          {value}
        </span>
      </span>
      <svg
        aria-hidden
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        className="h-3 w-3 shrink-0 text-muted"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m6 8 4 4 4-4" />
      </svg>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="absolute inset-0 h-full w-full cursor-pointer text-base opacity-0 focus:outline-none"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}
