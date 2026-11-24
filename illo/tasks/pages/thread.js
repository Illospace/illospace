/* Append posts and keep the reply form in place. */
(() => {
  const posts = document.getElementById('posts'), status = document.getElementById('status');
  const live = document.getElementById('live'), form = document.querySelector('#reply form[action$="/reply"]');
  const text = form?.elements.text, draft = form?.elements.draft, select = form?.elements.reply_to;
  const csrf = document.querySelector('.top input[name="csrf"]').value;
  const {thread, viewer, sent} = posts.dataset;
  const path = '/web/board/t/' + thread;
  const key = `illo-draft:${viewer}:${thread}`;
  const save = () => {
    try { localStorage.setItem(key, JSON.stringify({draft: draft.value, text: text.value})); }
    catch (_) { /* Storage is optional. */ }
  };
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && saved.draft === sent) localStorage.removeItem(key);
    else if (text && !text.value && typeof saved?.text === 'string') { text.value = saved.text; save(); }
  } catch (_) { /* Storage is optional. */ }
  text?.addEventListener('input', save);
  form?.addEventListener('submit', save);

  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let busy = false, stopped = false, lastPost = Date.now(), timer, firstNew, newCount = 0;
  const defaultChoice = () => [...(select?.options || [])].find(option => option.defaultSelected)?.value ?? '';
  const scroll = node => node.scrollIntoView({behavior: reduced.matches ? 'auto' : 'smooth'});
  const unread = new Map();

  function stop() {
    stopped = true;
    const link = document.createElement('a');
    link.href = '/web/signin?next=' + encodeURIComponent(path);
    link.textContent = 'Sign in again to see new posts.';
    live.replaceChildren(link);
  }

  function ack(node) {
    clearTimeout(unread.get(node));
    if (document.hidden || stopped) return;
    unread.set(node, setTimeout(async () => {
      if (document.hidden || stopped) return;
      try {
        const response = await fetch(path + '/seen', {method: 'POST', credentials: 'same-origin',
          redirect: 'manual', body: new URLSearchParams({csrf, upto: node.id.slice(1)})});
        if (response.status === 204) {
          for (const candidate of unread.keys()) {
            if (BigInt(candidate.id.slice(1)) <= BigInt(node.id.slice(1))) {
              clearTimeout(unread.get(candidate));
              unread.delete(candidate);
              observer.unobserve(candidate);
            }
          }
        } else if ([401, 404].includes(response.status) || response.type === 'opaqueredirect') stop();
      } catch (_) { /* The Mark as read form stays available. */ }
    }, 2000));
  }
  const observer = 'IntersectionObserver' in window && new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) ack(entry.target);
      else { clearTimeout(unread.get(entry.target)); unread.set(entry.target, null); }
    }
  });

  function updateStatus(template) {
    const next = document.importNode(template.content, true);
    const mark = status.querySelector('[data-mark-read]'), newMark = next.querySelector('[data-mark-read]');
    if (mark && newMark) mark.elements.upto.value = newMark.elements.upto.value;
    if (mark) {
      mark.hidden = !newMark;
      newMark?.remove();
    }
    // An open answer form may contain text. Keep it while the person uses it.
    const active = status.querySelector('.answer:focus-within, .answer:has(details[open])');
    if (active) next.querySelector('.answer')?.remove();
    for (const child of [...status.childNodes]) if (child !== mark && child !== active) child.remove();
    status.prepend(next);
  }

  function append(items) {
    const near = posts.getBoundingClientRect().bottom <= innerHeight + 200;
    const first = items.firstElementChild;
    const children = [...items.children];
    for (const node of children) {
      if (!reduced.matches) node.classList.add('new');
      if (node.hasAttribute('data-unread') && observer) observer.observe(node);
    }
    posts.append(items);
    posts.dataset.after = children.at(-1).id.slice(1);
    lastPost = Date.now();
    if (!firstNew && near && (!text?.value || document.activeElement !== text)) return scroll(first);
    firstNew ||= first;
    newCount += children.length;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = `${newCount} new ${newCount === 1 ? 'post' : 'posts'}`;
    button.onclick = () => { scroll(firstNew); firstNew = null; newCount = 0; live.replaceChildren(); };
    live.replaceChildren(button);
  }

  function queue(more = false) {
    clearTimeout(timer);
    if (!stopped && !document.hidden) {
      const delay = Date.now() - lastPost >= 600000 ? 20000 : 5000;
      timer = setTimeout(poll, more ? 0 : delay);
    }
  }

  async function poll() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    let more = false;
    try {
      const response = await fetch(path + '/posts?after=' + posts.dataset.after,
        {cache: 'no-store', credentials: 'same-origin'});
      if (document.hidden) return;
      if (response.redirected || [401, 404].includes(response.status)) return stop();
      if (response.status !== 200 || new URL(response.url).origin !== location.origin ||
          !response.headers.get('content-type')?.startsWith('text/html')) return;
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (document.hidden) return;
      const templates = ['next', 'status', 'reply-to'].map(id => doc.getElementById(id));
      if (!templates.every(node => node?.tagName === 'TEMPLATE')) return;
      const [items, state, choices] = templates;
      if (![...items.content.children].every(node => node.matches('li.post') && /^p[1-9][0-9]*$/.test(node.id)) ||
          ![...choices.content.children].every(node => node.tagName === 'OPTION')) return;
      updateStatus(state);
      if (select && select !== document.activeElement && select.value === defaultChoice()) {
        select.replaceChildren(document.importNode(choices.content, true));
      }
      if (items.content.children.length) append(document.importNode(items.content, true));
      more = items.hasAttribute('data-more') && items.content.children.length > 0;
    } catch (_) { /* Keep the page and retry. */ }
    finally { busy = false; queue(more); }
  }

  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    for (const [node, pending] of unread) {
      clearTimeout(pending);
      if (!document.hidden) { observer.unobserve(node); observer.observe(node); }
    }
    if (!document.hidden) poll();
  });
  queue();
})();
