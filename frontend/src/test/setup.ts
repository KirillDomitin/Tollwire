import "@testing-library/jest-dom/vitest";

// В jsdom нет matchMedia, а uPlot вызывает её уже при импорте (плотность пикселей экрана). Заглушка: «ни одно
// медиа-условие не выполнено» — как обычный экран со светлой темой и без «уменьшить движение».
if (typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
