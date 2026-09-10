import type { Config } from 'tailwindcss'

/**
 * SiteScope 360 design tokens (§36).
 *
 * Every colour is a CSS variable so the same class names work in both themes and a token can be
 * retuned in one place. Two decisions worth stating:
 *
 * - **Dark-first.** The dashboard is read on site tablets in bright sun and in trailers at dusk;
 *   dark is the default because the 360 viewer occupies most of the screen and a light chrome
 *   around a dark panorama is fatiguing.
 * - **Amber is reserved for status.** Hi-vis yellow is the obvious "construction" cliché, and
 *   spending it on branding would leave nothing legible for a warning. The brand colour is a
 *   deep teal; amber and red mean something.
 */
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: 'rgb(var(--canvas) / <alpha-value>)',
        surface: 'rgb(var(--surface) / <alpha-value>)',
        'surface-raised': 'rgb(var(--surface-raised) / <alpha-value>)',
        line: 'rgb(var(--line) / <alpha-value>)',
        'line-strong': 'rgb(var(--line-strong) / <alpha-value>)',
        ink: 'rgb(var(--ink) / <alpha-value>)',
        'ink-muted': 'rgb(var(--ink-muted) / <alpha-value>)',
        'ink-subtle': 'rgb(var(--ink-subtle) / <alpha-value>)',
        brand: {
          DEFAULT: 'rgb(var(--brand) / <alpha-value>)',
          soft: 'rgb(var(--brand-soft) / <alpha-value>)',
          ink: 'rgb(var(--brand-ink) / <alpha-value>)',
        },
        good: 'rgb(var(--good) / <alpha-value>)',
        warn: 'rgb(var(--warn) / <alpha-value>)',
        bad: 'rgb(var(--bad) / <alpha-value>)',
        info: 'rgb(var(--info) / <alpha-value>)',
        /** Provenance colours (§46) — estimated data is visually distinct from measured. */
        estimated: 'rgb(var(--estimated) / <alpha-value>)',
      },
      fontFamily: {
        sans: [
          'InterVariable',
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'Liberation Mono',
          'monospace',
        ],
      },
      fontSize: {
        // A denser scale than Tailwind's default: §36 asks for high information density, and
        // 14px base with tight leading fits a floor table without scrolling.
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.01em' }],
        xs: ['0.75rem', { lineHeight: '1.05rem' }],
        sm: ['0.8125rem', { lineHeight: '1.15rem' }],
        base: ['0.875rem', { lineHeight: '1.3rem' }],
        lg: ['1rem', { lineHeight: '1.45rem' }],
        xl: ['1.125rem', { lineHeight: '1.6rem' }],
        '2xl': ['1.375rem', { lineHeight: '1.8rem' }],
        '3xl': ['1.75rem', { lineHeight: '2.1rem' }],
      },
      borderRadius: {
        sm: '3px',
        DEFAULT: '5px',
        md: '6px',
        lg: '9px',
        xl: '12px',
      },
      boxShadow: {
        panel: '0 1px 2px rgb(0 0 0 / 0.28), 0 8px 24px -12px rgb(0 0 0 / 0.45)',
        pop: '0 10px 38px -10px rgb(0 0 0 / 0.6)',
        'focus-ring': '0 0 0 2px rgb(var(--canvas)), 0 0 0 4px rgb(var(--brand))',
      },
      transitionTimingFunction: {
        snap: 'cubic-bezier(0.2, 0.9, 0.25, 1)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        spin: { to: { transform: 'rotate(360deg)' } },
      },
      animation: {
        'fade-in': 'fade-in 140ms cubic-bezier(0.2, 0.9, 0.25, 1)',
        'slide-up': 'slide-up 160ms cubic-bezier(0.2, 0.9, 0.25, 1)',
        shimmer: 'shimmer 1.6s linear infinite',
      },
    },
  },
  plugins: [],
} satisfies Config
