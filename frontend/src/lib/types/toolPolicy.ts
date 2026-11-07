export interface ToolPolicy {
  disabled_tools: string[];
  version: number;
  updated_at: string | null;
  updated_by: string | null;
  warnings: { name: string; message: string }[];
  tools: { name: string; description: string }[];
}
