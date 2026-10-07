# Old public endpoints retired

On 2026-10-07, Reda instructed: "Retire the old public addresses." The decision covers `illo.space` and `api.illo.space`, the addresses reported in [#829](https://github.com/Illospace/illospace/issues/829). The restoration ticket is closed as not planned. No DNS or origin TLS repair is required for these addresses.

The authenticated private Illo endpoint works. Current `workspace.search`, `cycles.inspect`, `knowledge.get`, submission and satisfied receipt reads succeeded. A repository search found no active client configuration referencing either retired address.

Before the decision, `api.illo.space` did not resolve and `illo.space` returned HTTP525. The Compose application and private MCP path were healthy. Retirement does not mean that public DNS or TLS was repaired. No public DNS record, certificate, host listener or application authentication was changed.

This file keeps its original path so existing issue and archive links remain valid. The earlier repair proposal is superseded by the retirement decision.
