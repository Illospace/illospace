import type { ProjectContextResource } from './projectContext';

export type GitHubRepo = {
  full_name: string;
  html_url: string;
  description?: string | null;
  default_branch?: string;
  language?: string | null;
  topics?: string[];
  private?: boolean;
  permissions?: Record<string, boolean>;
};

export type VaultSecret = {
  id: number;
  key_name: string;
  description?: string;
  category?: string;
};

export function normalizeGitHubToken(value: string): string {
  let token = value.trim();
  const assignment = token.match(/^(?:export\s+)?(?:GITHUB_TOKEN|GH_TOKEN)\s*=\s*['"]?([^'"\s]+)['"]?$/i);
  if (assignment?.[1]) token = assignment[1];
  token = token.replace(/^Bearer\s+/i, '').replace(/^token\s+/i, '');
  token = token.replace(/^['"]+|['"]+$/g, '');
  return token.replace(/\s+/g, '');
}

export function isMaskedToken(value: string): boolean {
  return /^[*•●]+$/.test(value.trim());
}

export function isGitHubSecret(secret: VaultSecret): boolean {
  const haystack = `${secret.key_name} ${secret.description ?? ''} ${secret.category ?? ''}`.toLowerCase();
  return haystack.includes('github') || haystack.includes('gh_') || haystack.includes('gh-') || haystack === 'gh';
}

export function mergeVaultSecrets(...groups: VaultSecret[][]): VaultSecret[] {
  const seen = new Set<string>();
  const merged: VaultSecret[] = [];
  for (const group of groups) {
    for (const secret of group) {
      const key = secret.key_name;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(secret);
    }
  }
  return merged;
}

export function githubAccess(repo: GitHubRepo): 'read' | 'write' {
  const permissions = repo.permissions ?? {};
  return permissions.admin || permissions.maintain || permissions.push ? 'write' : 'read';
}

export function githubRepoToProjectResource(repo: GitHubRepo, vaultKey?: string): ProjectContextResource {
  const resource: ProjectContextResource = {
    type: 'repo',
    kind: 'repo',
    label: repo.full_name,
    name: repo.full_name,
    repo: repo.full_name,
    uri: repo.html_url,
    branch: repo.default_branch,
    source: 'github',
    access: githubAccess(repo),
  };
  if (vaultKey?.trim()) {
    resource.credential_ref = {
      type: 'vault_secret',
      provider: 'github',
      key_name: vaultKey.trim(),
    };
  }
  return resource;
}

export function mergeGitHubRepos(...groups: GitHubRepo[][]): GitHubRepo[] {
  const seen = new Set<string>();
  const merged: GitHubRepo[] = [];
  for (const group of groups) {
    for (const repo of group) {
      if (!repo?.full_name || seen.has(repo.full_name)) continue;
      seen.add(repo.full_name);
      merged.push(repo);
    }
  }
  return merged;
}

export function filterGitHubRepos(repos: GitHubRepo[], query: string): GitHubRepo[] {
  const terms = query
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (!terms.length) return repos;
  return repos.filter((repo) => {
    const haystack = [
      repo.full_name,
      repo.description,
      repo.html_url,
      repo.default_branch,
      repo.language,
      ...(repo.topics ?? []),
      repo.private ? 'private' : 'public',
    ].filter(Boolean).join(' ').toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function prioritizeGitHubRepos(repos: GitHubRepo[], selectedNames: string[]): GitHubRepo[] {
  if (!selectedNames.length) return repos;
  const selected = new Set(selectedNames);
  return [...repos].sort((a, b) => {
    const aSelected = selected.has(a.full_name) ? 1 : 0;
    const bSelected = selected.has(b.full_name) ? 1 : 0;
    return bSelected - aSelected;
  });
}

export function mergeGitHubRepoNames(...groups: string[][]): string[] {
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const group of groups) {
    for (const name of group) {
      if (!name || seen.has(name)) continue;
      seen.add(name);
      merged.push(name);
    }
  }
  return merged;
}
