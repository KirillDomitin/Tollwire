export type ThemeChoice = "system" | "light" | "dark";

const KEY = "vpn-panel-theme";

/** Выбор темы — удобство конкретного браузера, поэтому localStorage (недоступен — просто тема по системе). */
export function loadTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice): void {
  if (choice === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", choice);
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch {
    /* приватный режим и т.п. — тема просто не запомнится */
  }
}
