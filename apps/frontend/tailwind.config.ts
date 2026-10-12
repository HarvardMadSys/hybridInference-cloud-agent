import type { Config } from 'tailwindcss';

export default {
  content: ['./src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        crimson: {
          DEFAULT: '#A51C30',
          dark: '#8B1729',
          light: '#F18B9B',
        },
        // Semantic colors follow the theme without per-component dark variants.
        surface: {
          DEFAULT: 'rgb(var(--ui-surface) / <alpha-value>)',
          raised: 'rgb(var(--ui-surface-raised) / <alpha-value>)',
          muted: 'rgb(var(--ui-surface-muted) / <alpha-value>)',
          hover: 'rgb(var(--ui-surface-hover) / <alpha-value>)',
        },
        foreground: 'rgb(var(--ui-foreground) / <alpha-value>)',
        muted: 'rgb(var(--ui-muted) / <alpha-value>)',
        border: {
          DEFAULT: 'rgb(var(--ui-border) / <alpha-value>)',
          strong: 'rgb(var(--ui-border-strong) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'rgb(var(--ui-accent) / <alpha-value>)',
          hover: 'rgb(var(--ui-accent-hover) / <alpha-value>)',
          subtle: 'rgb(var(--ui-accent-subtle) / <alpha-value>)',
          foreground: 'rgb(var(--ui-accent-foreground) / <alpha-value>)',
        },
        focus: 'rgb(var(--ui-focus) / <alpha-value>)',
        status: {
          success: 'rgb(var(--ui-status-success) / <alpha-value>)',
          error: 'rgb(var(--ui-status-error) / <alpha-value>)',
          warning: 'rgb(var(--ui-status-warning) / <alpha-value>)',
          info: 'rgb(var(--ui-status-info) / <alpha-value>)',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        serif: ['var(--font-serif, Georgia)', 'Cambria', 'Times New Roman', 'serif'],
      },
      borderRadius: {
        lg: '0.5rem',
      },
      boxShadow: {
        subtle: '0 1px 2px rgba(0,0,0,0.06)',
        card: '0 8px 24px -8px rgb(var(--ui-foreground) / 0.12), 0 2px 6px rgb(var(--ui-foreground) / 0.04)',
      },
    },
  },
  plugins: [],
} satisfies Config;
