# Deployment Notes

## Current Responsibility Split

- Azure VPS: production Jarvis runtime, Telegram Bot, monitor daemon, real keys.
- Old VPS: public Garvyn Labs website, sanitized project notes, static demos.

## DNS

`garvynlabs.com` and `www.garvynlabs.com` point to the old VPS:

```text
172.245.147.100
```

## HTTPS

HTTPS is managed by Certbot on the old VPS. The certificate name is:

```text
garvynlabs.com
```

## Deploy

```bash
cd GarvynLabs
bash deploy/scripts/deploy-static.sh
```

The script expects the root `.env.deploy` file to provide `DEPLOY_HOST` and
`DEPLOY_USER`.

## Editing Roadmap

The current site is static and Markdown-driven. For Notion-like online editing,
use a protected backend instead of exposing raw filesystem writes publicly.

Recommended next phase:

1. `apps/api` provides a small authenticated editor API.
2. `/admin/` and `/api/` are protected with HTTP Basic Auth.
3. Markdown files are stored under `/var/www/garvynlabs/content`.
4. The first editor is a stable Markdown textarea. It can later be replaced by
   Milkdown, TipTap, or Toast UI Editor.
5. On save, the backend writes Markdown to disk and updates `manifest.json`.

Do not let the public website access production Jarvis secrets, raw sessions,
or profile SQLite files.
