# Garvyn Labs

Personal portfolio and engineering lab site for AI agent work, development notes,
architecture write-ups, and public demos.

Production Jarvis runs on Azure. This project manages the public website hosted
on the old VPS.

## Structure

```text
GarvynLabs/
  apps/
    web/       Static public site for now
    api/       Backend placeholder for future APIs/CMS/demo services
  deploy/
    nginx/     Nginx site config templates
    scripts/   Deployment helpers
  docs/        Architecture notes and site planning
```

## Local Preview

Open this file in a browser:

```text
apps/web/public/index.html
```

Or run a simple static server:

```bash
cd GarvynLabs/apps/web/public
python3 -m http.server 8080
```

## Deploy Static Site

The deploy script reads the root `.env.deploy` by default and uploads the static
site to the old web server.

```bash
cd GarvynLabs
bash deploy/scripts/deploy-static.sh
```

The script does not copy secrets into this project.

## Content Workflow

Current content is Markdown-first:

```text
apps/web/public/content/
  manifest.json
  ai-news/*.md
  ai-thinking/*.md
  ai-technology/*.md
```

Add a Markdown file under the right category, then add an entry to
`manifest.json`. Public article URLs use:

```text
/article/?slug=<slug>
```

Supported Markdown includes headings, lists, tables, blockquotes, code blocks,
links, inline HTML classes such as `<span class="color-teal">text</span>`, and
`==highlight==` markers.
