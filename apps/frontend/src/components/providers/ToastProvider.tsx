'use client';

import { Toaster } from 'react-hot-toast';
import { useTheme } from './ThemeProvider';

// `react-hot-toast` takes a style object rather than class names, so — like the
// terminal — it is one of the two surfaces a `dark:` variant cannot reach.
const PALETTE = {
  light: {
    background: '#fff',
    color: '#363636',
    boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)',
    border: '1px solid #e5e7eb',
  },
  dark: {
    background: '#1f2937',
    color: '#f3f4f6',
    boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.4), 0 2px 4px -2px rgb(0 0 0 / 0.3)',
    border: '1px solid rgba(255,255,255,0.1)',
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
          borderRadius: '0.5rem',
          padding: '12px 16px',
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
