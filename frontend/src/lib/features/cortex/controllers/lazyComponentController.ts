import { browser } from '$app/environment';

export type LazyComponentLoader<T> = () => Promise<{ default: T }>;

export interface LazyComponentController {
  ensure<T>(
    key: string,
    current: () => T | null,
    assign: (component: T) => void,
    loader: LazyComponentLoader<T>,
  ): void;
  createLoader<T>(
    key: string,
    current: () => T | null,
    assign: (component: T) => void,
    loader: LazyComponentLoader<T>,
  ): () => void;
  clear(): void;
}

export function createLazyComponentController(isBrowser = browser): LazyComponentController {
  const activeLoads = new Map<string, Promise<void>>();

  function ensure<T>(
    key: string,
    current: () => T | null,
    assign: (component: T) => void,
    loader: LazyComponentLoader<T>,
  ) {
    if (!isBrowser || current() || activeLoads.has(key)) return;

    const promise = loader()
      .then((module) => assign(module.default))
      .finally(() => {
        activeLoads.delete(key);
      });

    activeLoads.set(key, promise);
  }

  return {
    ensure,
    createLoader<T>(
      key: string,
      current: () => T | null,
      assign: (component: T) => void,
      loader: LazyComponentLoader<T>,
    ) {
      return () => ensure(key, current, assign, loader);
    },
    clear() {
      activeLoads.clear();
    },
  };
}
