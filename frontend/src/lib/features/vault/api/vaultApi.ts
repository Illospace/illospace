import { api } from '$lib/api/client';
import { pickTypedApiMethods } from '$lib/api/featureApi';
import type { VaultSecretPrompt } from '$lib/types/cortex';
import type { VaultSecret as ProjectContextVaultSecret } from '$lib/utils/projectContextGithub';

export type { VaultSecretPrompt };
export type VaultToken = string | null | undefined;
export type VaultSecret = ProjectContextVaultSecret & Record<string, any>;
export type CreateSecretInput = Parameters<typeof api.createSecret>[0];
export type UpdateSecretInput = Parameters<typeof api.updateSecret>[1];
export type VaultSetupPinInput = Parameters<typeof api.vaultSetupPin>[0];
export type VaultShareInput = Parameters<typeof api.vaultShare>[1];
export type VaultApproveGrantInput = Parameters<typeof api.vaultApproveGrant>[1];
export type VaultProjectSecretBindingInput = Parameters<typeof api.vaultBindProjectSecret>[1];
export type VaultUnlockResponse = Awaited<ReturnType<typeof api.vaultUnlock>>;
export type VaultPinStatus = Awaited<ReturnType<typeof api.pinStatus>>;
export type VaultSecretRead = Awaited<ReturnType<typeof api.revealSecret>>;
export type VaultAgentGrant = Awaited<ReturnType<typeof api.vaultAgentGrants>>[number];
export type VaultProjectBinding = Awaited<ReturnType<typeof api.vaultProjectBindings>>[number];
export type MissingSecret = Awaited<ReturnType<typeof api.missingSecrets>>[number];
export type VaultLogEntry = Awaited<ReturnType<typeof api.vaultLog>>[number];

type VaultApiMethods = {
  listSecrets: (category?: string, vaultToken?: VaultToken) => Promise<VaultSecret[]>;
  createSecret: (data: CreateSecretInput, vaultToken?: VaultToken) => Promise<VaultSecret>;
  revealSecret: (keyName: string, vaultToken?: VaultToken) => Promise<VaultSecretRead>;
  deleteSecret: typeof api.deleteSecret;
  updateSecret: typeof api.updateSecret;
  pinStatus: () => Promise<VaultPinStatus>;
  vaultSetupPin: typeof api.vaultSetupPin;
  vaultUnlock: (pin: string) => Promise<VaultUnlockResponse>;
  vaultLock: typeof api.vaultLock;
  vaultOrgUsers: typeof api.vaultOrgUsers;
  vaultShare: typeof api.vaultShare;
  vaultRevokeShare: typeof api.vaultRevokeShare;
  vaultLog: (vaultToken?: VaultToken) => Promise<VaultLogEntry[]>;
  missingSecrets: (vaultToken?: VaultToken) => Promise<MissingSecret[]>;
  vaultAgentGrants: (vaultToken?: VaultToken, status?: string) => Promise<VaultAgentGrant[]>;
  vaultApproveGrant: typeof api.vaultApproveGrant;
  vaultDenyGrant: typeof api.vaultDenyGrant;
  vaultProjectBindings: (vaultToken?: VaultToken) => Promise<VaultProjectBinding[]>;
  vaultBindProjectSecret: typeof api.vaultBindProjectSecret;
  vaultDeleteProjectBinding: typeof api.vaultDeleteProjectBinding;
};

export const vaultApi = pickTypedApiMethods<VaultApiMethods>([
  'listSecrets',
  'createSecret',
  'revealSecret',
  'deleteSecret',
  'updateSecret',
  'pinStatus',
  'vaultSetupPin',
  'vaultUnlock',
  'vaultLock',
  'vaultOrgUsers',
  'vaultShare',
  'vaultRevokeShare',
  'vaultLog',
  'missingSecrets',
  'vaultAgentGrants',
  'vaultApproveGrant',
  'vaultDenyGrant',
  'vaultProjectBindings',
  'vaultBindProjectSecret',
  'vaultDeleteProjectBinding',
]);

export const {
  listSecrets,
  createSecret,
  revealSecret,
  deleteSecret,
  updateSecret,
  pinStatus,
  vaultSetupPin,
  vaultUnlock,
  vaultLock,
  vaultOrgUsers,
  vaultShare,
  vaultRevokeShare,
  vaultLog,
  missingSecrets,
  vaultAgentGrants,
  vaultApproveGrant,
  vaultDenyGrant,
  vaultProjectBindings,
  vaultBindProjectSecret,
  vaultDeleteProjectBinding,
} = vaultApi;
