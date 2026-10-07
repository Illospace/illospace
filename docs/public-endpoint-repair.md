# Public endpoint repair

Issue [#829](https://github.com/Illospace/illospace/issues/829) remains an operator task. On 2026-10-07, `api.illo.space` did not resolve and `illo.space` returned HTTP525. The hosted MCP path and the Compose application were healthy. A public DNS probe is not evidence that the MCP receipt path failed.

The host has nginx on public port80; port443 is bound to Tailscale addresses. The current SSH account cannot use passwordless sudo. Cloudflare zone access is not available to this checkout. The Compose deploy binds its web entrypoint to loopback; a code redeploy does not install public DNS or TLS.

If the addresses remain supported, the operator must:

1. Confirm the current origin address. Restore the proxied `api` DNS record in the `illo.space` zone. Check the apex record too; do not reuse an old IP without verification.
2. Install an origin certificate whose SANs include the supported hostnames. Use a publicly trusted certificate or Cloudflare Origin CA. Keep the key outside this repository.
3. Add an nginx TLS virtual host on the public interface, proxying the existing loopback web entrypoint. Preserve the Tailscale listener and application authentication. Validate with `nginx -t` before reload.
4. Use Cloudflare Full(strict), which requires an unexpired matching origin certificate and HTTPS on443. [Cloudflare setup](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/) and [Origin CA setup](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/).
5. Verify DNS, TLS and an authenticated MCP read from outside the host. Record the time and the response on#829. A GET returning405 on a POST-only MCP route proves routing, not authenticated tool operation.

Read-only checks:

```sh
dig +short api.illo.space
curl --max-time 20 -I https://illo.space/
curl --max-time 20 -I https://api.illo.space/api/mcp
ssh illo-dev 'ss -ltn; tailscale serve status'
```

Do not apply the historical suggestion to switch to Flexible TLS. Restore the origin TLS listener. Cloudflare describes525 as failure during the origin TLS handshake in its [error reference](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-525/).

If the product owner retires these addresses, close the ticket as not planned, update client references to the supported endpoint, and record the decision. Do not describe retirement as a successful TLS repair.
