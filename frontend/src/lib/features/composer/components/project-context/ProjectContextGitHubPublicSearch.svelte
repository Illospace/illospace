<script lang="ts">
  let {
    query = $bindable(''),
    loading = false,
    onQueryInput,
    onSearch,
  }: {
    query?: string;
    loading?: boolean;
    onQueryInput?: (value: string) => void;
    onSearch?: () => void;
  } = $props();

  function handleInput(value: string) {
    query = value;
    onQueryInput?.(value);
  }
</script>

<div class="github-search-row">
  <input
    aria-label="Search GitHub repositories"
    placeholder="Type public repository search"
    value={query}
    oninput={(event) => handleInput((event.currentTarget as HTMLInputElement).value)}
    onkeydown={(event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        onSearch?.();
      }
    }}
  />
  <button type="button" onclick={onSearch} disabled={loading || !query.trim()}>
    {loading ? 'Searching...' : 'Public search'}
  </button>
</div>
