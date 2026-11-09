import type { Config } from 'tailwindcss';
import colors from 'tailwindcss/colors';

export default {
  content: ['./src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // One neutral family. The semantic tokens below are zinc, and Tailwind's
        // own `gray` is blue-tinted, so every legacy `gray-*` utility read as a
        // second grey beside them. Mapping `gray` onto zinc makes them one
        // palette without touching the components that use either.
        gray: colors.zinc,
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
          // The frame of a field or a secondary button: firmer than a divider,
          // lighter than `strong`, which is the hover state's.
          control: 'rgb(var(--ui-border-control) / <alpha-value>)',
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
        md: '0.5rem',
        lg: '0.625rem',
        xl: '0.875rem',
        '2xl': '1.125rem',
      },
      // Elevation is a theme token: a shadow drawn from a fixed dark colour
      // disappears on a dark ground and one drawn from the foreground glows
      // there, so each level is defined once per theme in globals.css.
      boxShadow: {
        xs: 'var(--ui-shadow-xs)',
        subtle: 'var(--ui-shadow-xs)',
        sm: 'var(--ui-shadow-sm)',
        DEFAULT: 'var(--ui-shadow-sm)',
        md: 'var(--ui-shadow-md)',
        lg: 'var(--ui-shadow-lg)',
        xl: 'var(--ui-shadow-xl)',
        '2xl': 'var(--ui-shadow-xl)',
        card: 'var(--ui-shadow-card)',
      },
    },
  },
  plugins: [],
} satisfies Config;
