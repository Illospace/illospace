/* Pan and zoom on the board. With no script, or on a phone, the board is a normal page and each form works. */
(() => {
  const small = matchMedia('(max-width: 47.999rem)'), still = matchMedia('(prefers-reduced-motion: reduce)');
  const root = document.documentElement, board = document.querySelector('.board');
  const world = board.querySelector('.world'), top = document.querySelector('.top');
  const KEY = 'illo-board-camera', MIN = .4, MAX = 1.5, GLIDE = 450;
  const control = 'input,textarea,select,button,a,summary,[contenteditable="true"]';
  let cam = {x: 0, y: 0, s: 1}, drag = null, pinch = null, busy = false, timer = 0, moved = false;
  const pointers = new Map();
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  // The visible part of the paper: below the bar, with a margin on each side.
  function view() {
    const m = Math.min(48, board.clientWidth * .04), bar = top.offsetHeight;
    return {l: m, t: bar + m * .75, r: board.clientWidth - m, b: board.clientHeight - m, m};
  }
  // A part of the world always stays in view, so a person cannot lose the board.
  function keep(c) {
    const w = world.offsetWidth * c.s, h = world.offsetHeight * c.s, v = view();
    const ex = Math.min(160, w / 2), ey = Math.min(160, h / 2);
    return {s: c.s, x: clamp(c.x, v.l + ex - w, v.r - ex), y: clamp(c.y, v.t + ey - h, v.b - ey)};
  }
  function draw(glide) {
    clearTimeout(timer);
    board.classList.toggle('glide', !!glide && !still.matches);
    if (glide) timer = setTimeout(() => board.classList.remove('glide'), GLIDE + 50);
    cam = keep(cam);
    world.style.transform = `translate(${cam.x}px,${cam.y}px) scale(${cam.s})`;
    // The dots belong to the paper: they move and scale with the world.
    board.style.backgroundSize = `${22 * cam.s}px ${22 * cam.s}px`;
    board.style.backgroundPosition = `${cam.x}px ${cam.y}px`;
    save();
  }
  function save(resume = '') {
    try { sessionStorage.setItem(KEY, JSON.stringify({...cam, resume})); } catch (_) { /* Storage is optional. */ }
  }
  function zoom(scale, x, y, glide) {
    const s = clamp(scale, MIN, MAX), k = s / cam.s;
    cam = {s, x: x - (x - cam.x) * k, y: y - (y - cam.y) * k};
    draw(glide);
  }
  function centre() {
    const v = view();
    return {x: (v.l + v.r) / 2, y: (v.t + v.b) / 2};
  }
  // First view: zoom 1, the columns in the middle when they fit, else "Needs you" at the left.
  function home() {
    const v = view(), w = world.offsetWidth;
    return {s: 1, x: w <= v.r - v.l ? (board.clientWidth - w) / 2 : v.l, y: v.t};
  }
  function fit() {
    const v = view(), w = world.offsetWidth, h = world.offsetHeight;
    const s = clamp(Math.min((v.r - v.l) / w, (v.b - v.t) / h, 1), MIN, MAX);
    cam = {s, x: (board.clientWidth - w * s) / 2, y: v.t};
    draw(true);
  }
  function frame() {
    const el = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
    return el && el.matches('.f') && board.contains(el) ? el : null;
  }
  // The place of an element in the world. Offsets do not change while the world glides.
  function at(el) {
    let x = 0, y = 0;
    for (let n = el; n && n !== world; n = n.offsetParent) { x += n.offsetLeft; y += n.offsetTop; }
    return {x, y};
  }
  // A frame of the address: zoom 1, the frame near the top, in the middle.
  function show(el, glide) {
    const p = at(el);
    cam = {s: 1, x: (board.clientWidth - el.offsetWidth) / 2 - p.x, y: view().t - p.y};
    draw(glide);
  }
  // Bring an element into view with the smallest move.
  function reveal(el) {
    const p = at(el), v = view(), s = cam.s;
    const r = {l: cam.x + p.x * s, t: cam.y + p.y * s, w: el.offsetWidth * s, h: el.offsetHeight * s};
    let dx = 0, dy = 0;
    if (r.l < v.l || r.w > v.r - v.l) dx = v.l - r.l; else if (r.l + r.w > v.r) dx = v.r - r.l - r.w;
    if (r.t < v.t || r.h > v.b - v.t) dy = v.t - r.t; else if (r.t + r.h > v.b) dy = v.b - r.t - r.h;
    if (dx || dy) { cam.x += dx; cam.y += dy; draw(true); }
  }

  function start() {
    root.classList.add('canvas');
    scrollTo(0, 0);
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(KEY)); } catch (_) { /* Storage is optional. */ }
    const valid = saved && [saved.x, saved.y, saved.s].every(Number.isFinite) && saved.s >= MIN && saved.s <= MAX;
    const el = frame();
    // After a form post the person comes back to the same view. A new address wins over a saved view.
    if (valid && (location.hash ? saved.resume === location.hash : true)) {
      cam = {x: saved.x, y: saved.y, s: saved.s};
      draw();
    } else {
      cam = home();
      draw();
      if (el) setTimeout(() => show(el, true), 60);
    }
    // Pictures change the height of a frame when they load. Place the frame again if the person did not move.
    addEventListener('load', () => { if (el && !moved && !(valid && saved.resume === location.hash)) show(el, true); },
      {once: true});
  }
  function stop() {
    root.classList.remove('canvas');
    board.classList.remove('glide', 'drag');
    world.style.removeProperty('transform');
    board.style.removeProperty('background-size');
    board.style.removeProperty('background-position');
    pointers.clear(); drag = pinch = null;
  }
  if (!small.matches) start();
  small.addEventListener('change', () => (small.matches ? stop() : start()));
  addEventListener('resize', () => { if (!small.matches) draw(); });
  addEventListener('hashchange', () => { const el = frame(); if (el && !small.matches) show(el, true); });

  // The browser scrolls a box to show a focused element or a target. The script moves the world in place of that.
  board.addEventListener('scroll', () => { if (!small.matches) board.scrollTop = board.scrollLeft = 0; });
  board.addEventListener('focusin', event => {
    if (small.matches) return;
    board.scrollTop = board.scrollLeft = 0;
    reveal(event.target.closest('summary,button,a,textarea') || event.target);
  });
  const needs = document.getElementById('needs');
  const draftKey = id => `illo-draft:${board.dataset.viewer}:${id}`;
  try {
    if (needs.dataset.sentThread && needs.dataset.sent) {
      const key = draftKey(needs.dataset.sentThread), stored = JSON.parse(localStorage.getItem(key));
      if (stored?.draft === needs.dataset.sent) localStorage.removeItem(key);
    }
  } catch (_) { /* Storage is optional. */ }
  for (const form of needs.querySelectorAll('article[id^="b"] form[action$="/reply"]')) {
    const key = draftKey(form.closest('article').id.slice(1));
    const text = form.elements.text, draft = form.elements.draft;
    const backup = () => {
      try { localStorage.setItem(key, JSON.stringify({draft: draft.value, text: text.value})); }
      catch (_) { /* Storage is optional. */ }
    };
    try {
      const stored = JSON.parse(localStorage.getItem(key));
      if (!text.value && typeof stored?.text === 'string') { text.value = stored.text; backup(); }
    } catch (_) { /* Storage is optional. */ }
    text.addEventListener('input', backup);
    form.addEventListener('submit', backup);
  }
  board.addEventListener('submit', event => {
    const match = (event.target.getAttribute('action') || '').match(/^\/(t|web\/a|web\/board\/t)\/(\d+)\//);
    const prefix = {'t': 't', 'web/a': 'a', 'web/board/t': 'b'};
    if (match && !small.matches) save(`#${prefix[match[1]]}${match[2]}`);
  });
  top.querySelectorAll('[data-zoom]').forEach(button => button.addEventListener('click', () => {
    moved = true;
    if (button.dataset.zoom === 'fit') return fit();
    const c = centre();
    zoom(cam.s * (button.dataset.zoom === 'in' ? 1.25 : .8), c.x, c.y, true);
  }));

  // Two fingers on a trackpad pan. A pinch arrives as a wheel with ctrlKey: it zooms smoothly about the pointer.
  board.addEventListener('wheel', event => {
    const text = event.target.closest('textarea');
    if (small.matches || text && text.scrollHeight > text.clientHeight && !event.ctrlKey) return;
    event.preventDefault();
    moved = true;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? board.clientHeight : 1;
    if (event.ctrlKey || event.metaKey) {
      zoom(cam.s * Math.exp(-clamp(event.deltaY * unit, -40, 40) * .01), event.clientX, event.clientY);
    } else {
      cam.x -= (event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX) * unit;
      cam.y -= (event.shiftKey && !event.deltaX ? 0 : event.deltaY) * unit;
      draw();
    }
  }, {passive: false});

  // Safari sends a trackpad pinch as gesture events, not as a wheel.
  let gesture = null;
  board.addEventListener('gesturestart', event => {
    if (!small.matches) { event.preventDefault(); gesture = cam.s; }
  });
  board.addEventListener('gesturechange', event => {
    if (small.matches) return;
    event.preventDefault();
    if (gesture) { moved = true; zoom(gesture * event.scale, event.clientX, event.clientY); }
  });
  board.addEventListener('gestureend', event => { event.preventDefault(); gesture = null; });

  // A drag on the paper or on a column title pans. A drag on a frame stays a text selection, a click, or typing.
  const pair = () => {
    const [a, b] = [...pointers.values()];
    return {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, d: Math.hypot(a.x - b.x, a.y - b.y)};
  };
  board.addEventListener('pointerdown', event => {
    if (small.matches) return;
    if (event.pointerType === 'touch') {
      pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (pointers.size === 2) {
        pinch = {...pair(), cam: {...cam}};
        drag = null;
        return;
      }
    }
    if (event.button !== 0 || event.target.closest('.f,#board-changed')) return;
    drag = {id: event.pointerId, x: event.clientX - cam.x, y: event.clientY - cam.y};
    board.setPointerCapture(event.pointerId);
    board.classList.add('drag');
    board.classList.remove('glide');
  });
  board.addEventListener('pointermove', event => {
    if (pointers.has(event.pointerId)) pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pinch && pointers.size === 2 && pinch.d) {
      const p = pair(), s = clamp(pinch.cam.s * p.d / pinch.d, MIN, MAX), k = s / pinch.cam.s;
      cam = {s, x: p.x - (pinch.x - pinch.cam.x) * k, y: p.y - (pinch.y - pinch.cam.y) * k};
      moved = true;
      draw();
    } else if (drag?.id === event.pointerId) {
      cam.x = event.clientX - drag.x; cam.y = event.clientY - drag.y;
      moved = true;
      draw();
    }
  });
  const release = event => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (drag?.id === event.pointerId) { drag = null; board.classList.remove('drag'); }
  };
  board.addEventListener('pointerup', release);
  board.addEventListener('pointercancel', release);

  // The arrow keys pan in short eased steps, but not while a control, a link or a summary has the focus.
  addEventListener('keydown', event => {
    const step = {ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1]}[event.key];
    if (!step || small.matches || event.altKey || event.ctrlKey || event.metaKey ||
        document.activeElement?.closest(control) || !getSelection().isCollapsed) return;
    event.preventDefault();
    moved = true;
    const d = event.shiftKey ? 400 : 120;
    cam.x += step[0] * d; cam.y += step[1] * d;
    draw(true);
  });

  // Refresh the read-only columns while the person writes in Needs you.
  function editing() {
    return needs.contains(document.activeElement) && document.activeElement.matches('input,textarea,select,button') ||
      [...needs.querySelectorAll('textarea,input:not([type="hidden"])')].some(i => i.value !== i.defaultValue);
  }
  async function refresh() {
    if (document.hidden || busy) return;
    busy = true;
    try {
      const response = await fetch('/web/board', {cache: 'no-store', credentials: 'same-origin'});
      if (response.status !== 200 || response.redirected || new URL(response.url).origin !== location.origin ||
          !response.headers.get('content-type')?.startsWith('text/html') || document.hidden) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (document.hidden || next.querySelector('.board')?.dataset.viewer !== board.dataset.viewer) return;
      for (const id of ['work', 'threads', 'done']) {
        const column = next.querySelector('.world > section.column#' + id);
        if (column && !column.querySelector('form')) {
          document.getElementById(id).replaceChildren(...[...column.childNodes].map(node => document.importNode(node, true)));
        }
      }
      const ids = doc => [...doc.querySelectorAll('#needs > .f')].map(f => f.id).sort().join(',');
      if (!editing() && next.getElementById('needs') && ids(next) !== ids(document) &&
          !document.getElementById('board-changed')) {
        const link = document.createElement('a');
        link.id = 'board-changed'; link.className = 'btn go'; link.href = '/web/board';
        link.textContent = 'The list changed. Read the board again.';
        document.querySelector('#needs .column-line').after(link);
      }
    } catch (_) { /* The current board and its forms stay available. */ }
    finally { busy = false; }
  }
  let refreshTimer;
  function refreshInterval() {
    clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, small.matches ? 60000 : 30000);
  }
  refreshInterval();
  small.addEventListener('change', refreshInterval);
})();
