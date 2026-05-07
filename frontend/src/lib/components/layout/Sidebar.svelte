<script lang="ts">
  import { page } from '$app/stores';
  import HealthIndicator from './HealthIndicator.svelte';
  import IllospaceLogo from './IllospaceLogo.svelte';

  let collapsed = $state(false);

  const navItems = [
    { href: '/cortex', label: 'Cortex', icon: '\u269B\uFE0F' },
    { href: '/cycles', label: 'Cycles', icon: '\u267B\uFE0F' },
    { href: '/memory', label: 'Memory', icon: '\u{1F4BE}' },
    { href: '/skills', label: 'Skills', icon: '\u{1F4AA}' },
    { href: '/team', label: 'Team', icon: '\u{1F465}' },
    { href: '/vault', label: 'Vault', icon: '\u{1F510}' },
    { href: '/costs', label: 'Costs', icon: '\u{1F4B0}' },
    { href: '/system', label: 'System', icon: '\u{1F5A5}\uFE0F' },
  ];

  function isActive(href: string, pathname: string): boolean {
    if (href === '/') return pathname === '/';
    return pathname.startsWith(href);
  }

  function toggle() {
    collapsed = !collapsed;
  }
</script>

<aside class="sidebar" class:collapsed role="navigation" aria-label="Main navigation">
  <div class="sidebar-header">
    <a href="/cortex" class="sidebar-logo">
      <span class="sidebar-logo-icon" aria-hidden="true">
        <IllospaceLogo className="sidebar-logo-mark" variant="icon" title="Illospace" />
      </span>
      <span class="sidebar-logo-text">Illospace</span>
    </a>
    <button class="sidebar-toggle" onclick={toggle} aria-label="Toggle sidebar">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
        <path d="M2 3h12v1.5H2V3zm0 4.25h12v1.5H2v-1.5zm0 4.25h12V13H2v-1.5z"/>
      </svg>
    </button>
  </div>

  <nav class="sidebar-nav">
    {#each navItems as item}
      <a
        href={item.href}
        class="sidebar-item"
        class:active={isActive(item.href, $page.url.pathname)}
      >
        <span class="sidebar-icon">{item.icon}</span>
        <span class="sidebar-label">{item.label}</span>
      </a>
    {/each}
  </nav>

  <div class="sidebar-footer">
    <HealthIndicator />
    <div class="sidebar-shortcut-hint">
      <span><kbd>1</kbd>-<kbd>6</kbd> primary tabs</span>
      <span><kbd>{'\u2318'}K</kbd> search</span>
    </div>
  </div>
</aside>
