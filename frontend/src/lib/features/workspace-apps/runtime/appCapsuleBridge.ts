import { jsonForScript } from './workspaceAppRuntime';

export type AppCapsuleRuntimeApp = {
  id: string;
  key: string;
  name: string;
};

export function appCapsuleBridgeScript(app: AppCapsuleRuntimeApp) {
  return `<script>
    (function () {
      const pending = new Map();
      let sequence = 0;
      let currentState = {};
      let lastStateSignature = '';
      let lastThemeSignature = '';
      let lastAppSignature = '';

      function nextId() {
        sequence += 1;
        return 'illo-' + Date.now().toString(36) + '-' + sequence.toString(36);
      }

      function request(type, payload) {
        const requestId = nextId();
        parent.postMessage({ source: 'illo-app', type, requestId, ...(payload || {}) }, '*');
        return new Promise((resolve, reject) => {
          pending.set(requestId, { resolve, reject });
          setTimeout(() => {
            if (!pending.has(requestId)) return;
            pending.delete(requestId);
            reject(new Error('Illo app bridge timed out'));
          }, 8000);
        });
      }

      function stableSignature(value) {
        try {
          return JSON.stringify(value || {});
        } catch (error) {
          return String(Date.now());
        }
      }

      function applyTheme(nextTheme) {
        const mode = nextTheme && nextTheme.mode === 'light' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-illo-theme', mode);
        document.documentElement.style.colorScheme = mode;
      }

      function normalizeWheelDelta(event, axis) {
        const raw = axis === 'x' ? event.deltaX : event.deltaY;
        if (!raw) return 0;
        if (event.deltaMode === 1) return raw * 16;
        if (event.deltaMode === 2) {
          return raw * Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0, 1);
        }
        return raw;
      }

      function canScrollElement(element, deltaX, deltaY, allowsX, allowsY) {
        const maxTop = Math.max(0, element.scrollHeight - element.clientHeight);
        const maxLeft = Math.max(0, element.scrollWidth - element.clientWidth);
        return (
          (allowsY && deltaY < 0 && element.scrollTop > 0) ||
          (allowsY && deltaY > 0 && element.scrollTop < maxTop) ||
          (allowsX && deltaX < 0 && element.scrollLeft > 0) ||
          (allowsX && deltaX > 0 && element.scrollLeft < maxLeft)
        );
      }

      function wheelScrollTarget(start, deltaX, deltaY) {
        let element = start && start.nodeType === Node.ELEMENT_NODE ? start : start && start.parentElement;
        while (element && element !== document.body && element !== document.documentElement) {
          const style = window.getComputedStyle(element);
          const allowsY = style.overflowY !== 'visible' && style.overflowY !== 'hidden' && style.overflowY !== 'clip';
          const allowsX = style.overflowX !== 'visible' && style.overflowX !== 'hidden' && style.overflowX !== 'clip';
          if (canScrollElement(element, deltaX, deltaY, allowsX, allowsY)) return element;
          element = element.parentElement;
        }
        const root = document.scrollingElement || document.documentElement || document.body;
        return root && canScrollElement(root, deltaX, deltaY, true, true) ? root : null;
      }

      function installWheelScrollBridge() {
        window.addEventListener('wheel', (event) => {
          if (event.defaultPrevented || event.ctrlKey) return;
          const deltaX = normalizeWheelDelta(event, 'x');
          const deltaY = normalizeWheelDelta(event, 'y');
          if (!deltaX && !deltaY) return;
          const target = wheelScrollTarget(event.target, deltaX, deltaY);
          if (!target) return;
          const previousTop = target.scrollTop;
          const previousLeft = target.scrollLeft;
          target.scrollTop += deltaY;
          target.scrollLeft += deltaX;
          if (target.scrollTop !== previousTop || target.scrollLeft !== previousLeft) {
            event.preventDefault();
          }
        }, { passive: false });
      }

      function binding(alias) {
        const normalizedAlias = String(alias || '').trim();
        if (!normalizedAlias) throw new Error('window.illo.data(alias) requires an alias');
        function run(operation, payload) {
          return request('illo:binding', {
            alias: normalizedAlias,
            operation,
            payload: payload || {}
          });
        }
        const api = {
          schema: (options) => run('schema', options),
          list: (options) => run('list', options),
          query: (options) => run('query', options),
          get: (recordId, options) => run('get', { recordId, ...(options || {}) }),
          create: (data, options) => run('create', { data: data || {}, ...(options || {}) }),
          update: (recordId, dataPatch, options) => run('update', { recordId, dataPatch: dataPatch || {}, ...(options || {}) }),
          archive: (recordId, options) => run('archive', { recordId, ...(options || {}) }),
          aggregate: (options) => run('aggregate', options),
          subscribe: (handler, options) => {
            if (typeof handler !== 'function') throw new Error('data(alias).subscribe(handler) requires a function');
            const config = options || {};
            const intervalMs = Math.max(1000, Math.min(Number(config.intervalMs || config.interval_ms || 5000), 60000));
            let active = true;
            let timer = null;
            async function tick() {
              if (!active) return;
              try {
                const records = await api.list(config);
                if (active) handler(records);
              } catch (error) {
                if (active && typeof config.onError === 'function') config.onError(error);
              } finally {
                if (active) timer = setTimeout(tick, intervalMs);
              }
            }
            tick();
            return function unsubscribe() {
              active = false;
              if (timer) clearTimeout(timer);
            };
          }
        };
        return api;
      }

      const stateApi = {
        get: () => request('illo:state:get'),
        set: (data) => request('illo:state:set', { data: data || {} }),
        update: (patch) => request('illo:state:update', { patch: patch || {} }),
        value: () => currentState
      };

      window.illo = {
        app: ${jsonForScript(app)},
        theme: {},
        data: binding,
        state: stateApi,
        actions: {
          run: (actionKey, payload) => request('illo:action:run', { actionKey: String(actionKey || ''), payload: payload || {} })
        },
        toast: (message) => parent.postMessage({ source: 'illo-app', type: 'illo:toast', message: String(message || '') }, '*'),
        getState: stateApi.get,
        setState: stateApi.set,
        updateState: stateApi.update
      };

      window.addEventListener('message', (event) => {
        const message = event.data || {};
        if (message.source !== 'illo-host') return;
        if (message.type === 'illo:init' || message.type === 'illo:state') {
          const nextApp = message.app || window.illo.app;
          const nextState = message.state || {};
          const nextTheme = message.theme || window.illo.theme || {};
          const appSignature = stableSignature(nextApp);
          const stateSignature = stableSignature(nextState);
          const themeSignature = stableSignature(nextTheme);
          const changed =
            appSignature !== lastAppSignature ||
            stateSignature !== lastStateSignature ||
            themeSignature !== lastThemeSignature;
          currentState = nextState;
          window.illo.app = nextApp;
          window.illo.theme = nextTheme;
          if (themeSignature !== lastThemeSignature) applyTheme(window.illo.theme);
          lastAppSignature = appSignature;
          lastStateSignature = stateSignature;
          lastThemeSignature = themeSignature;
          if (changed) {
            window.dispatchEvent(new CustomEvent('illo:state', { detail: currentState }));
          }
          return;
        }
        if (message.type === 'illo:response' && pending.has(message.requestId)) {
          const handlers = pending.get(message.requestId);
          pending.delete(message.requestId);
          if (Array.isArray(message.warnings)) {
            message.warnings.forEach((warning) => console.warn('[Illo app bridge]', warning));
          }
          if (message.error) handlers.reject(new Error(String(message.error)));
          else handlers.resolve(message.data);
        }
      });

      function ready() {
        parent.postMessage({ source: 'illo-app', type: 'illo:ready' }, '*');
      }

      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ready, { once: true });
      } else {
        setTimeout(ready, 0);
      }
      installWheelScrollBridge();
    })();
  <\/script>`;
}
