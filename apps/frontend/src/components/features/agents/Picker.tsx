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
    <label className="relative inline-flex min-h-9 max-w-full items-center gap-1.5 rounded-lg border border-transparent px-2.5 py-2 text-[13px] font-medium text-gray-600 transition-colors hover:border-gray-200 hover:bg-gray-50 focus-within:border-crimson/30 focus-within:bg-white focus-within:ring-2 focus-within:ring-crimson/15 dark:text-gray-400 dark:hover:border-white/10 dark:hover:bg-white/5 dark:focus-within:bg-gray-900">
      <span className="sr-only">{label}</span>
      {/* The width of the control. Truncated rather than unbounded so one
          pathological model id cannot push the run button off the row. */}
      <span aria-hidden className="min-w-0 max-w-[13rem] truncate text-gray-700 dark:text-gray-300">
        {value}
      </span>
      <svg
        aria-hidden
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m6 8 4 4 4-4" />
      </svg>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="absolute inset-0 h-full w-full cursor-pointer opacity-0 focus:outline-none"
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
