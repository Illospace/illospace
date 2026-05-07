<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { GitHubRepo } from '$lib/utils/projectContextGithub';

  let {
    open = $bindable(false),
    query = $bindable(''),
    connected = false,
    loading = false,
    visibleRepos = [],
    filteredCount = 0,
    hiddenCount = 0,
    selectedRepoNames = [],
    selectedRepoCount = 0,
    selectedSummary = 'Choose repositories',
    error = '',
    needsTokenReplacement = false,
    bindAgentToken = $bindable(true),
    bindingAgentToken = false,
    onSearch,
    onToggleRepo,
    onAddSelected,
    onQueryInput,
    onReplaceToken,
  }: {
    open?: boolean;
    query?: string;
    connected?: boolean;
    loading?: boolean;
    visibleRepos?: GitHubRepo[];
    filteredCount?: number;
    hiddenCount?: number;
    selectedRepoNames?: string[];
    selectedRepoCount?: number;
    selectedSummary?: string;
    error?: string;
    needsTokenReplacement?: boolean;
    bindAgentToken?: boolean;
    bindingAgentToken?: boolean;
    onSearch?: () => void;
    onToggleRepo?: (repo: GitHubRepo) => void;
    onAddSelected?: () => void;
    onQueryInput?: (value: string) => void;
    onReplaceToken?: () => void;
  } = $props();

  function handleQueryInput(value: string) {
    query = value;
    onQueryInput?.(value);
  }
</script>

<div class="github-repo-selector">
  <div class="github-repo-select-row">
    <button
      type="button"
      class="github-repo-dropdown-trigger"
      aria-expanded={open}
      onclick={() => { open = !open; }}
    >
      <span>
        <strong>Repositories</strong>
        <small>{selectedSummary}</small>
      </span>
      <ConstellationIcon name="chevron-down" size={14} stroke={2} />
    </button>
    <div class="github-repo-select-actions">
      {#if open}
        <button type="button" onclick={() => { open = false; }}>
          Done
        </button>
      {/if}
      <button type="button" onclick={onAddSelected} disabled={!selectedRepoCount || bindingAgentToken}>
        {bindingAgentToken ? 'Binding...' : `Add selected${selectedRepoCount ? ` (${selectedRepoCount})` : ''}`}
      </button>
    </div>
  </div>
  {#if connected}
    <label class="github-agent-token-toggle">
      <input type="checkbox" bind:checked={bindAgentToken} disabled={bindingAgentToken} />
      <span>
        <strong>Agent token</strong>
        <small>GH_TOKEN for selected repos</small>
      </span>
    </label>
  {/if}
  {#if open}
    <div class="github-repo-menu">
      <div class="github-repo-search-control">
        <ConstellationIcon name="search" size={14} stroke={2} />
        <input
          aria-label={connected ? 'Search connected GitHub repositories' : 'Search GitHub repositories'}
          placeholder="Search repositories"
          value={query}
          oninput={(event) => handleQueryInput((event.currentTarget as HTMLInputElement).value)}
          onkeydown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              onSearch?.();
            } else if (event.key === 'Escape') {
              event.preventDefault();
              open = false;
            }
          }}
        />
        <button type="button" onclick={onSearch} disabled={loading || !query.trim()}>
          {loading ? 'Searching...' : 'Search'}
        </button>
      </div>
      <div class="github-repo-options" aria-label="GitHub repositories">
        {#if filteredCount === 0}
          <div class="project-context-muted compact">No matching repositories.</div>
        {:else}
          {#each visibleRepos as repo}
            {@const selected = selectedRepoNames.includes(repo.full_name)}
            <button
              type="button"
              class="github-repo-option"
              class:selected
              aria-pressed={selected}
              onclick={() => onToggleRepo?.(repo)}
            >
              <span class="github-repo-tick">
                {#if selected}
                  <ConstellationIcon name="check" size={13} stroke={2.2} />
                {/if}
              </span>
              <span class="github-repo-option-copy">
                <strong>{repo.full_name}</strong>
                <small>
                  {repo.private ? 'Private' : 'Public'} · {repo.default_branch ?? 'main'}{repo.language ? ` · ${repo.language}` : ''}
                </small>
              </span>
            </button>
          {/each}
        {/if}
      </div>
      {#if hiddenCount > 0}
        <p class="project-context-muted compact">{hiddenCount} more matches. Keep typing to narrow.</p>
      {/if}
      {#if connected && query.trim() && filteredCount === 0}
        <p class="project-context-muted compact">Search can add public repos or private repos visible to this token.</p>
      {/if}
    </div>
  {/if}
</div>

{#if error}
  <p class="project-context-error">{error}</p>
  {#if needsTokenReplacement}
    <div class="github-error-actions">
      <button type="button" onclick={onReplaceToken}>Replace selected token</button>
    </div>
  {/if}
{/if}
