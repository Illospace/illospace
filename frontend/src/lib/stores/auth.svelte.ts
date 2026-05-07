import { api } from '$lib/api/client';

interface User {
  id: string;
  name: string;
  email: string;
  role: string;
  color: string;
  org_id: string;
  org_name: string;
  org_slug?: string;
  attribution_enabled: boolean;
  approved: boolean;
  default_provider?: string | null;
}

class AuthStore {
  user = $state<User | null>(null);
  loading = $state(true);

  async init() {
    try {
      const data = await api.getMe();
      this.user = data ?? null;
    } catch {
      this.user = null;
    } finally {
      this.loading = false;
    }
  }

  async login(email: string, password: string) {
    const data = await api.login(email, password);
    // Login returns { ok, user }, but we re-init from /me for consistency
    this.user = data?.user ?? (data as User);
    return data;
  }

  async logout() {
    await api.logout();
    this.user = null;
  }
}

export const auth = new AuthStore();
