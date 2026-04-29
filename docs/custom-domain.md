# AxonGate Custom Domain Runbook

AxonGate is ready for a custom HTTPS API domain. The application already reads
`AXONGATE_PUBLIC_BASE_URL` and rewrites manifest/discovery URLs from that value.

## Recommended Domain Shape

Use a dedicated API hostname, for example:

```text
https://api.axongate.example
```

Keep `axongate.base.eth` as the onchain identity that resolves to the vault
wallet. The HTTPS custom domain is for HTTP agents, marketplaces, crawlers, and
humans.

## Railway Steps

1. Open the Railway service.
2. Add the custom domain in Railway networking settings.
3. Create the DNS record Railway asks for at your DNS provider.
4. Wait until Railway marks the domain as verified and SSL is active.
5. Set this Railway variable:

```text
AXONGATE_PUBLIC_BASE_URL=https://your-custom-domain
```

6. Redeploy.

## Validation

After redeploy, verify every discovery URL uses the new host:

```bash
curl -s https://your-custom-domain/manifest.json
curl -s https://your-custom-domain/llms.txt
curl -s https://your-custom-domain/.well-known/x402
curl -s https://your-custom-domain/.well-known/x402.json
curl -s https://your-custom-domain/.well-known/agent.json
curl -s https://your-custom-domain/.well-known/agent-card.json
curl -s https://your-custom-domain/sitemap.xml
```

The vault address should remain:

```text
0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7
```
