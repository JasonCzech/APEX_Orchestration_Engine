import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { usePreferences } from './preferences'

/**
 * Theme names mirror the data-theme values defined by the APEX Load token
 * sheet (theme/index.css, ported verbatim from Project_Stormrunner):
 * `:root` is the dark default; the others are `[data-theme='…']` overrides.
 * Note: the reference defines `monokai-dimmed` (not `monokai`).
 */
export const THEMES = [
  'dark',
  'light',
  'solarized-dark',
  'solarized-light',
  'monokai-dimmed',
] as const

export type ThemeName = (typeof THEMES)[number]
export type ThemePreference = ThemeName | 'system'

export const DEFAULT_THEME: ThemeName = 'light'
export const THEME_STORAGE_KEY = 'apex.theme'

export const THEME_LABELS: Record<ThemeName, string> = {
  dark: 'Dark',
  light: 'Light',
  'solarized-dark': 'Solarized Dark',
  'solarized-light': 'Solarized Light',
  'monokai-dimmed': 'Monokai Dimmed',
}

export function isThemeName(value: unknown): value is ThemeName {
  return typeof value === 'string' && (THEMES as readonly string[]).includes(value)
}

export function getStoredTheme(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'apex-light') {
      // Retired compatibility palette: match the early bootstrap migration.
      try { window.localStorage.setItem(THEME_STORAGE_KEY, 'light') } catch { /* Best effort. */ }
      return 'light'
    }
    return stored === 'system' || isThemeName(stored) ? stored : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

/** `dark` has no [data-theme] override — setting it is safe and keeps tests/devtools explicit. */
export function applyTheme(theme: ThemeName): void {
  document.documentElement.setAttribute('data-theme', theme)
}

interface ThemeContextValue {
  theme: ThemeName
  preference: ThemePreference
  themes: readonly ThemeName[]
  setTheme: (theme: ThemePreference) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setThemeState] = useState<ThemePreference>(getStoredTheme)
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false)
  const { preferences } = usePreferences()
  useEffect(() => {
    document.documentElement.dataset.reduceMotion = String(preferences.reduceMotion)
  }, [preferences.reduceMotion])
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-color-scheme: dark)')
    const update = () => setSystemDark(query?.matches ?? false)
    query?.addEventListener?.('change', update)
    return () => query?.removeEventListener?.('change', update)
  }, [])
  const theme: ThemeName = preference === 'system' ? (systemDark ? 'dark' : 'light') : preference

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const setTheme = useCallback((next: ThemePreference) => {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // Persisting is best-effort; the in-memory theme still applies.
    }
    setThemeState(next)
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, preference, themes: THEMES, setTheme }),
    [theme, preference, setTheme],
  )

  return createElement(ThemeContext.Provider, { value }, children)
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}
