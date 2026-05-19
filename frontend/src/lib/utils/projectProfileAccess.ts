export type ProjectAccessMember = {
  user_id?: string | null;
  name?: string | null;
  email?: string | null;
};

export type ProjectAccessSummary = {
  isPublic: boolean;
  members: ProjectAccessMember[];
  visibleMembers: ProjectAccessMember[];
  overflowCount: number;
  tooltip: string;
  ariaLabel: string;
};

export const PROJECT_ACCESS_BADGE_LIMIT = 3;

export function projectAccessMemberName(member: ProjectAccessMember | null | undefined): string {
  return String(member?.name ?? member?.email ?? '').trim();
}

export function projectAccessMemberKey(member: ProjectAccessMember, index = 0): string {
  const key = String(member.user_id ?? member.email ?? projectAccessMemberName(member)).trim();
  return key || `member-${index}`;
}

export function projectAccessInitial(memberOrName: ProjectAccessMember | string | null | undefined): string {
  const name = typeof memberOrName === 'string'
    ? memberOrName
    : projectAccessMemberName(memberOrName);
  const initial = Array.from(name.trim())[0];
  return initial ? initial.toUpperCase() : '?';
}

export function projectAccessMembers(access: ProjectAccessMember[] | null | undefined): ProjectAccessMember[] {
  const seen = new Set<string>();
  const members: ProjectAccessMember[] = [];

  for (const member of access ?? []) {
    const name = projectAccessMemberName(member);
    if (!name) continue;

    const key = String(member.user_id ?? member.email ?? name).trim().toLowerCase();
    if (!key || seen.has(key)) continue;

    seen.add(key);
    members.push(member);
  }

  return members;
}

export function summarizeProjectAccess(
  profile: { visibility?: string | null; access?: ProjectAccessMember[] | null },
  limit = PROJECT_ACCESS_BADGE_LIMIT,
  owner?: ProjectAccessMember | null,
): ProjectAccessSummary {
  const isPublic = profile.visibility === 'public';
  if (isPublic) {
    return {
      isPublic: true,
      members: [],
      visibleMembers: [],
      overflowCount: 0,
      tooltip: 'Public project',
      ariaLabel: 'Public project',
    };
  }

  const members = projectAccessMembers([
    ...(owner ? [owner] : []),
    ...(profile.access ?? []),
  ]);
  const safeLimit = Math.max(0, limit);
  const visibleMembers = members.slice(0, safeLimit);
  const overflowCount = Math.max(0, members.length - visibleMembers.length);
  const memberNames = members.map(projectAccessMemberName);
  const memberList = memberNames.join(', ');

  return {
    isPublic: false,
    members,
    visibleMembers,
    overflowCount,
    tooltip: memberList ? `Shared with ${memberList}` : 'Private project',
    ariaLabel: memberList ? `Private project shared with ${memberList}` : 'Private project',
  };
}
