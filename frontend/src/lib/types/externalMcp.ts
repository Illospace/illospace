/** Organization MCP server settings, as the /api/external-mcp API returns them. */

export interface ExternalMcpConfig {
  name: string;
  url: string;
  transport: 'streamable_http';
  auth_mode: 'none' | 'bearer' | 'client_credentials';
  bearer_token_key: string | null;
  token_url: string | null;
  client_id_key: string | null;
  client_secret_key: string | null;
  scope: string | null;
  resource: string | null;
  allowed_tools: string[];
  enabled: boolean;
  timeout_seconds: number;
}

export interface ExternalMcpServer extends ExternalMcpConfig {
  id: number;
  created_at: string;
  updated_at: string;
  missing_secret_keys: string[];
}

export interface ExternalMcpTestResult {
  ok: boolean;
  tools: { name: string; description: string; allowed: boolean }[];
  error?: string;
}
