'use client';

import { createContext, Fragment, useContext, useMemo, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { useT } from '@/components/providers/LocaleProvider';
import { highlightFence, TOKEN_CLASS } from '@/lib/codeHighlight';
import { workspaceLinkPath } from '@/lib/workspaceLink';

const WorkspaceLinkContext = createContext<((path: string) => void) | null>(null);

// Set inside a rendered link, so an image there does not become a second,
// nested one: `[![build](badge.svg)](https://ci)` is a single link.
const InsideLinkContext = createContext(false);

export function WorkspaceLinkProvider({
  onOpenFile,
  children,
}: {
  onOpenFile: (path: string) => void;
  children: ReactNode;
}) {
  return (
    <WorkspaceLinkContext.Provider value={onOpenFile}>{children}</WorkspaceLinkContext.Provider>
  );
}

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const onOpenFile = useContext(WorkspaceLinkContext);
  const path = href && onOpenFile ? workspaceLinkPath(href) : null;
  if (path && onOpenFile) {
    return (
      <button
        type="button"
        onClick={() => onOpenFile(path)}
        className="ui-focus-ring rounded-sm text-accent underline underline-offset-2 hover:opacity-80"
        title={path}
      >
        <InsideLinkContext.Provider value={true}>{children}</InsideLinkContext.Provider>
      </button>
    );
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-crimson dark:text-crimson-light underline underline-offset-2 hover:opacity-80"
    >
      <InsideLinkContext.Provider value={true}>{children}</InsideLinkContext.Provider>
    </a>
  );
}

/**
 * An image, shown as a link to itself rather than fetched.
 *
 * Everything this renders is agent output or repository content, so an image
 * URL is someone else's choice. An `<img>` would request it the moment the
 * conversation opens — a beacon that tells whoever chose it when, from where
 * and in what browser the message was read — and a relative one would be a
 * request to this origin carrying the session cookie. So the image is named by
 * its alt text and opening it is the reader's decision: a new tab without a
 * referrer, or the workspace pane for a file reference, exactly like a link.
 * Inside a link it is only its label, and the link around it stays the action.
 */
function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const t = useT();
  const insideLink = useContext(InsideLinkContext);
  const name = alt?.trim();
  const label = name
    ? t('conversation.markdownImage', { alt: name })
    : t('conversation.markdownImageUntitled');
  if (insideLink || !src) return <span>{label}</span>;
  return <MarkdownLink href={src}>{label}</MarkdownLink>;
}

// react-markdown gives `code` no way to tell a fenced block from inline code
// (the `inline` prop went away in v9), so `pre` marks its subtree and `code`
// reads the mark. Sniffing `language-*` is not enough: an unlabelled fence has
// no class at all, and used to be styled as inline code — one chip per line,
// crimson on a dark block.
const FencedContext = createContext(false);

const INLINE_CODE =
  'rounded-[4px] bg-gray-950/[0.06] px-[5px] py-px font-mono text-[0.85em] text-gray-800 dark:bg-white/10 dark:text-gray-200';

function FencedCode({ className, children }: { className?: string; children?: ReactNode }) {
  const language = /language-([\w+#.-]+)/.exec(className ?? '')?.[1];
  // The text of a fence ends in the newline that closed it; drop that one so
  // the block does not finish on an empty line.
  const source = typeof children === 'string' ? children.replace(/\n$/, '') : null;
  if (source === null) return <code className={className}>{children}</code>;
  if (!language) return <code className={className}>{source}</code>;
  const lines = highlightFence(source, language);
  return (
    <code className={className}>
      {lines.map((tokens, line) => (
        <Fragment key={line}>
          {line > 0 ? '\n' : null}
          {tokens.map((token, at) =>
            token.kind === 'plain' ? (
              token.text
            ) : (
              <span key={at} className={TOKEN_CLASS[token.kind]}>
                {token.text}
              </span>
            ),
          )}
        </Fragment>
      ))}
    </code>
  );
}

function Code({ className, children }: { className?: string; children?: ReactNode }) {
  if (useContext(FencedContext)) return <FencedCode className={className}>{children}</FencedCode>;
  return <code className={INLINE_CODE}>{children}</code>;
}

// Render each element explicitly with Tailwind utilities: the
// @tailwindcss/typography (`prose`) plugin isn't installed, so element defaults
// would otherwise be stripped by the CSS reset (flat lists, unsized headings).
const components: Components = {
  p: ({ children }) => <p className="mb-2 leading-relaxed">{children}</p>,
  a: ({ href, children }) => <MarkdownLink href={href}>{children}</MarkdownLink>,
  img: ({ src, alt }) => (
    <MarkdownImage src={typeof src === 'string' ? src : undefined} alt={alt} />
  ),
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  h1: ({ children }) => <h3 className="mb-2 mt-1 text-base font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-2 mt-1 text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1 mt-1 text-sm font-semibold">{children}</h4>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-gray-300 dark:border-white/15 pl-3 italic text-gray-600 dark:text-gray-400">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-gray-200 dark:border-white/10" />,
  pre: ({ children }) => (
    <FencedContext.Provider value={true}>
      <pre className="mb-3 overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 px-3.5 py-3 font-mono text-[12px] leading-[1.6] text-gray-800 dark:border-white/10 dark:bg-white/[0.04] dark:text-gray-200">
        {children}
      </pre>
    </FencedContext.Provider>
  ),
  code: ({ className, children }) => <Code className={className}>{children}</Code>,
  table: ({ children }) => (
    <div className="mb-3 overflow-x-auto">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-gray-200 bg-gray-50 px-2 py-1 text-left font-semibold dark:border-white/10 dark:bg-white/5">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-gray-200 dark:border-white/10 px-2 py-1">{children}</td>
  ),
};

/** Render a Markdown string (GitHub-flavored) as styled HTML. */
export function Markdown({ text }: { text: string }) {
  const workspaceLinks = useContext(WorkspaceLinkContext) !== null;
  // Settled messages should not be parsed and highlighted again when the
  // composer or a live sibling updates. Link actions still receive the current
  // workspace callback through MarkdownLink's own context subscription.
  const content = useMemo(
    () => (
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components}
        // An image's source is only ever a link target (see MarkdownImage),
        // so it may name a workspace file exactly as a link may.
        urlTransform={(url, key) =>
          (key === 'href' || key === 'src') && workspaceLinks && workspaceLinkPath(url)
            ? url
            : defaultUrlTransform(url)
        }
      >
        {text}
      </ReactMarkdown>
    ),
    [text, workspaceLinks],
  );
  return <div className="break-words text-sm leading-relaxed [&>*:last-child]:mb-0">{content}</div>;
}
