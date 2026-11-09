'use client';

import { Toaster } from 'react-hot-toast';
import { useTheme } from './ThemeProvider';

// `react-hot-toast` takes a style object rather than class names, so — like the
// terminal — it is one of the two surfaces a `dark:` variant cannot reach.
// The values mirror the theme tokens in globals.css: the raised surface, the
// foreground, the border and the floating (`card`) elevation.
const PALETTE = {
  light: {
    background: '#fff',
    color: '#18181b',
    boxShadow: 'var(--ui-shadow-card)',
    border: '1px solid #e4e4e7',
  },
  dark: {
    background: '#1e1e22',
    color: '#f4f4f5',
    boxShadow: 'var(--ui-shadow-card)',
    border: '1px solid rgba(255,255,255,0.08)',
  },
} as const;

export function ToastProvider() {
  const { resolved } = useTheme();
  const palette = PALETTE[resolved];
  return (
    <Toaster
      position="top-right"
      toastOptions={{
        duration: 4000,
        style: {
          ...palette,
          borderRadius: '0.875rem',
          padding: '10px 14px',
          fontSize: '14px',
          fontWeight: 500,
        },
        success: {
          iconTheme: {
            primary: '#10b981',
            secondary: palette.background,
          },
        },
        error: {
          iconTheme: {
            primary: '#ef4444',
            secondary: palette.background,
          },
        },
      }}
    />
  );
}
