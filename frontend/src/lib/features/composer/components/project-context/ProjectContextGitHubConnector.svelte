<script lang="ts">
  import { onMount } from 'svelte';
  import {
    bindProjectContextGitHubToken,
    connectProjectContextGitHub,
    searchProjectContextGitHub,
  } from '$lib/features/cortex/api/cortexApi';
  import { createSecret, listSecrets, vaultUnlock } from '$lib/features/vault/api/vaultApi';
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import {
    filterGitHubRepos,
    githubRepoToProjectResource,
    isGitHubSecret,
    isMaskedToken,
    mergeGitHubRepoNames,
    mergeGitHubRepos,
    mergeVaultSecrets,
    normalizeGitHubToken,
    prioritizeGitHubRepos,
    type GitHubRepo,
    type VaultSecret,
  } from '$lib/utils/projectContextGithub';
  import {
    projectContextErrorDetail,
    vaultProjectContextErrorMessage,
  } from './projectContextProfiles';
  import ProjectContextGitHubPublicSearch from './ProjectContextGitHubPublicSearch.svelte';
  import ProjectContextGitHubRepoSelector from './ProjectContextGitHubRepoSelector.svelte';
  import ProjectContextGitHubTokenControl from './ProjectContextGitHubTokenControl.svelte';

  let {
    onAddResources,
  }: {
    onAddResources?: (resources: ProjectContextResource[]) => void;
  } = $props();

  const GITHUB_REPO_VISIBLE_LIMIT = 4;

  let githubQuery = $state('');
  let githubRepos = $state<GitHubRepo[]>([]);
  let githubAccountRepos = $state<GitHubRepo[]>([]);
  let githubLoading = $state(false);
  let githubError = $state('');
  let vaultSecrets = $state<VaultSecret[]>([]);
  let vaultLoading = $state(false);
  let vaultLocked = $state(false);
  let vaultError = $state('');
  let vaultToken = $state<string | null>(null);
  let vaultPin = $state('');
  let vaultUnlocking = $state(false);
  let selectedGithubVaultKey = $state('');
  let githubConnectedVaultKey = $state('');
  let githubConnectedLogin = $state('');
  let selectedGithubRepoNames = $state<string[]>([]);
  let githubRepoDropdownOpen = $state(false);
  let newGithubToken = $state('');
  let newGithubTokenKey = $state('GITHUB_TOKEN');
  let githubTokenSaving = $state(false);
  let githubAuthOpen = $state(false);
  let bindGitHubTokenForAgents = $state(true);
  let githubBindingSaving = $state(false);

  const githubVaultSecrets = $derived(vaultSecrets.filter(isGitHubSecret));
  const selectedGitHubVaultSecret = $derived(
    vaultSecrets.find((secret) => secret.key_name === selectedGithubVaultKey),
  );
  const githubConnected = $derived(Boolean(githubConnectedVaultKey && githubConnectedVaultKey === selectedGithubVaultKey));
  const githubRepoOptions = $derived(mergeGitHubRepos(githubAccountRepos, githubRepos));
  const filteredGitHubRepoOptions = $derived(filterGitHubRepos(githubRepoOptions, githubQuery));
  const prioritizedGitHubRepoOptions = $derived(
    prioritizeGitHubRepos(filteredGitHubRepoOptions, selectedGithubRepoNames),
  );
  const visibleGitHubRepoOptions = $derived(prioritizedGitHubRepoOptions.slice(0, GITHUB_REPO_VISIBLE_LIMIT));
  const hiddenGitHubRepoOptionCount = $derived(
    Math.max(0, prioritizedGitHubRepoOptions.length - visibleGitHubRepoOptions.length),
  );
  const selectedGithubRepos = $derived(
    mergeGitHubRepos(githubAccountRepos, githubRepos)
      .filter((repo) => selectedGithubRepoNames.includes(repo.full_name)),
  );
  const selectedGithubRepoSummary = $derived(
    selectedGithubRepoNames.length
      ? `${selectedGithubRepoNames.length} selected`
      : 'Choose repositories',
  );
  const githubNeedsTokenReplacement = $derived(
    githubError.includes('GitHub rejected') || githubError.includes('masked value'),
  );

  function openGitHubAuth() {
    githubAuthOpen = true;
    vaultError = '';
  }

  function closeGitHubAuth() {
    githubAuthOpen = false;
    vaultError = '';
    newGithubToken = '';
  }

  function clearVaultError() {
    vaultError = '';
  }

  function replaceSelectedGitHubToken() {
    newGithubTokenKey = selectedGithubVaultKey || newGithubTokenKey || 'GITHUB_TOKEN';
    newGithubToken = '';
    vaultError = '';
    githubAuthOpen = true;
  }

  function pickDefaultGitHubVaultKey(secrets: VaultSecret[]) {
    if (selectedGithubVaultKey && secrets.some((secret) => secret.key_name === selectedGithubVaultKey)) return;
    const match = secrets.find(isGitHubSecret);
    selectedGithubVaultKey = match?.key_name ?? '';
  }

  async function loadVaultSecrets() {
    vaultLoading = true;
    vaultError = '';
    try {
      const githubCategorySecrets = await listSecrets('github', vaultToken);
      const allSecrets = await listSecrets(undefined, vaultToken).catch(() => []);
      vaultSecrets = mergeVaultSecrets(
        Array.isArray(githubCategorySecrets) ? githubCategorySecrets : [],
        Array.isArray(allSecrets) ? allSecrets.filter(isGitHubSecret) : [],
      );
      vaultLocked = false;
      pickDefaultGitHubVaultKey(vaultSecrets);
    } catch (err: any) {
      if (err?.status === 423) {
        vaultLocked = true;
        vaultError = '';
      } else {
        vaultError = vaultProjectContextErrorMessage(err);
      }
      vaultSecrets = [];
      selectedGithubVaultKey = '';
    } finally {
      vaultLoading = false;
    }
  }

  async function unlockVaultForGitHub() {
    if (!vaultPin.trim() || vaultUnlocking) return;
    vaultUnlocking = true;
    vaultError = '';
    try {
      const unlocked = await vaultUnlock(vaultPin.trim());
      vaultToken = unlocked.token;
      vaultPin = '';
      vaultLocked = false;
      await loadVaultSecrets();
    } catch (err: any) {
      vaultError = vaultProjectContextErrorMessage(err, 'Could not unlock vault.');
    } finally {
      vaultUnlocking = false;
    }
  }

  async function saveGitHubTokenToVault() {
    const keyName = newGithubTokenKey.trim() || 'GITHUB_TOKEN';
    const tokenValue = normalizeGitHubToken(newGithubToken);
    if (!tokenValue || githubTokenSaving) {
      vaultError = 'Paste a token to save it in Vault.';
      return;
    }
    if (isMaskedToken(tokenValue)) {
      vaultError = 'Paste the real GitHub token value, not a masked password field.';
      return;
    }
    githubTokenSaving = true;
    vaultError = '';
    try {
      const saved = await createSecret({
        key_name: keyName,
        value: tokenValue,
        description: 'GitHub token for Project Context',
        category: 'github',
      }, vaultToken);
      newGithubToken = '';
      selectedGithubVaultKey = saved.key_name ?? keyName;
      await loadVaultSecrets();
      githubAuthOpen = false;
    } catch (err: any) {
      if (err?.status === 423) vaultLocked = true;
      vaultError = vaultProjectContextErrorMessage(err, 'Could not save token to Vault.');
    } finally {
      githubTokenSaving = false;
    }
  }

  function addGitHubRepoOptions(repos: GitHubRepo[]) {
    const existingAccountNames = new Set(githubAccountRepos.map((repo) => repo.full_name));
    const accountRepos: GitHubRepo[] = [];
    const publicRepos: GitHubRepo[] = [];
    for (const repo of repos) {
      if (repo.private || existingAccountNames.has(repo.full_name)) {
        accountRepos.push(repo);
      } else {
        publicRepos.push(repo);
      }
    }
    if (accountRepos.length) githubAccountRepos = mergeGitHubRepos(accountRepos, githubAccountRepos);
    if (publicRepos.length) githubRepos = mergeGitHubRepos(publicRepos, githubRepos);
  }

  function handleGitHubQueryInput(value: string) {
    githubQuery = value;
    githubError = '';
    if (githubConnected) {
      githubRepoDropdownOpen = true;
    }
  }

  function selectGitHubVaultKey(keyName: string) {
    selectedGithubVaultKey = keyName;
    githubError = '';
    if (keyName !== githubConnectedVaultKey) {
      githubAccountRepos = [];
      selectedGithubRepoNames = [];
      githubRepoDropdownOpen = false;
      githubConnectedVaultKey = '';
      githubConnectedLogin = '';
    }
  }

  function toggleGitHubRepo(repo: GitHubRepo) {
    if (!repo?.full_name) return;
    if (selectedGithubRepoNames.includes(repo.full_name)) {
      selectedGithubRepoNames = selectedGithubRepoNames.filter((name) => name !== repo.full_name);
    } else {
      selectedGithubRepoNames = mergeGitHubRepoNames(selectedGithubRepoNames, [repo.full_name]);
    }
    githubError = '';
  }

  async function bindSelectedGitHubReposForAgents() {
    if (!githubConnected || !bindGitHubTokenForAgents) return true;
    if (!selectedGithubVaultKey) {
      githubError = 'Choose a Vault token first.';
      return false;
    }
    githubBindingSaving = true;
    githubError = '';
    try {
      const results = await Promise.allSettled(
        selectedGithubRepos.map((repo) => bindProjectContextGitHubToken({
          vault_key: selectedGithubVaultKey,
          repo: repo.full_name,
          env_name: 'GH_TOKEN',
        }, vaultToken)),
      );
      const failed = results.find((result) => result.status === 'rejected');
      if (failed?.status === 'rejected') {
        if (failed.reason?.status === 423) {
          vaultLocked = true;
          githubAuthOpen = true;
        }
        githubError = projectContextErrorDetail(failed.reason, 'Could not bind this token for agents.');
        return false;
      }
      return true;
    } finally {
      githubBindingSaving = false;
    }
  }

  async function addSelectedGitHubRepos() {
    if (!selectedGithubRepos.length) {
      githubError = 'Choose at least one repository to attach.';
      return;
    }
    const bound = await bindSelectedGitHubReposForAgents();
    if (!bound) return;
    const vaultKey = githubConnected ? selectedGithubVaultKey : undefined;
    onAddResources?.(selectedGithubRepos.map((repo) => githubRepoToProjectResource(repo, vaultKey)));
    githubRepoDropdownOpen = false;
  }

  async function connectSelectedGitHubToken() {
    if (!selectedGithubVaultKey) {
      githubError = 'Choose a Vault token first.';
      return;
    }
    githubAuthOpen = false;
    await listMyGitHubRepos(selectedGithubVaultKey);
  }

  async function searchGitHubRepos() {
    const query = githubQuery.trim();
    if (!query) {
      githubError = 'Search for an owner, repo, or keyword.';
      return;
    }
    githubLoading = true;
    githubError = '';
    try {
      const result = await searchProjectContextGitHub({
        query,
        vault_key: githubConnected ? selectedGithubVaultKey : undefined,
      }, vaultToken);
      const repos = Array.isArray(result?.repos) ? result.repos : [];
      addGitHubRepoOptions(repos);
      if (result?.matched_exact && repos[0]?.full_name) {
        selectedGithubRepoNames = mergeGitHubRepoNames(selectedGithubRepoNames, [repos[0].full_name]);
        githubQuery = repos[0].full_name;
      }
      githubRepoDropdownOpen = true;
      if (!repos.length) githubError = githubConnected
        ? 'No public or token-visible repositories matched this search.'
        : 'No public repositories matched this search.';
    } catch (err: any) {
      if (err?.status === 423) {
        vaultLocked = true;
        githubAuthOpen = true;
      }
      githubError = projectContextErrorDetail(err, 'GitHub search failed.');
      if (!githubConnected) githubRepos = [];
    } finally {
      githubLoading = false;
    }
  }

  async function listMyGitHubRepos(keyName = selectedGithubVaultKey) {
    if (!keyName) return;
    githubLoading = true;
    githubError = '';
    try {
      const result = await connectProjectContextGitHub({ vault_key: keyName }, vaultToken);
      const repos = Array.isArray(result?.repos) ? result.repos : [];
      githubAccountRepos = repos;
      githubConnectedVaultKey = keyName;
      githubConnectedLogin = typeof result?.login === 'string' ? result.login : '';
      selectedGithubRepoNames = [];
      githubRepoDropdownOpen = true;
    } catch (err: any) {
      if (err?.status === 423) {
        vaultLocked = true;
        githubAuthOpen = true;
      }
      githubError = projectContextErrorDetail(err, 'Could not list GitHub repos.');
      githubAccountRepos = [];
      selectedGithubRepoNames = [];
      githubRepoDropdownOpen = false;
      githubConnectedVaultKey = '';
      githubConnectedLogin = '';
      if (githubError.includes('GitHub rejected this token')) {
        replaceSelectedGitHubToken();
      }
    } finally {
      githubLoading = false;
    }
  }

  onMount(() => {
    void loadVaultSecrets();
  });
</script>

<div class="connector-panel">
  <ProjectContextGitHubTokenControl
    vaultSecrets={githubVaultSecrets}
    {vaultLocked}
    {vaultLoading}
    {vaultError}
    bind:vaultPin
    {vaultUnlocking}
    bind:vaultTokenKey={selectedGithubVaultKey}
    {githubLoading}
    {githubConnected}
    {githubConnectedLogin}
    githubRepoCount={githubAccountRepos.length}
    authOpen={githubAuthOpen}
    bind:newTokenKey={newGithubTokenKey}
    bind:newTokenValue={newGithubToken}
    tokenSaving={githubTokenSaving}
    onOpenAuth={openGitHubAuth}
    onCloseAuth={closeGitHubAuth}
    onRefresh={() => void loadVaultSecrets()}
    onConnect={() => void connectSelectedGitHubToken()}
    onSelectVaultKey={selectGitHubVaultKey}
    onUnlockVault={() => void unlockVaultForGitHub()}
    onSaveToken={() => void saveGitHubTokenToVault()}
    onClearVaultError={clearVaultError}
  />

  {#if githubConnected || githubRepos.length}
    <ProjectContextGitHubRepoSelector
      bind:open={githubRepoDropdownOpen}
      bind:query={githubQuery}
      connected={githubConnected}
      loading={githubLoading}
      visibleRepos={visibleGitHubRepoOptions}
      filteredCount={filteredGitHubRepoOptions.length}
      hiddenCount={hiddenGitHubRepoOptionCount}
      selectedRepoNames={selectedGithubRepoNames}
      selectedRepoCount={selectedGithubRepos.length}
      selectedSummary={selectedGithubRepoSummary}
      error={githubError}
      needsTokenReplacement={githubNeedsTokenReplacement}
      bind:bindAgentToken={bindGitHubTokenForAgents}
      bindingAgentToken={githubBindingSaving}
      onSearch={() => void searchGitHubRepos()}
      onToggleRepo={toggleGitHubRepo}
      onAddSelected={() => void addSelectedGitHubRepos()}
      onQueryInput={handleGitHubQueryInput}
      onReplaceToken={replaceSelectedGitHubToken}
    />
  {:else}
    <ProjectContextGitHubPublicSearch
      bind:query={githubQuery}
      loading={githubLoading}
      onQueryInput={handleGitHubQueryInput}
      onSearch={() => void searchGitHubRepos()}
    />
    {#if githubError}
      <p class="project-context-error">{githubError}</p>
      {#if githubNeedsTokenReplacement}
        <div class="github-error-actions">
          <button type="button" onclick={replaceSelectedGitHubToken}>Replace selected token</button>
        </div>
      {/if}
    {/if}
  {/if}
  {#if githubLoading && !githubConnected}
    <div class="project-context-muted">Loading repositories...</div>
  {/if}
</div>
