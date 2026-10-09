# Team catalog deploy rules

Public App URL: `https://team-18-app.thecosmoslabs.com/app/`

## Do not overwrite teammates

| App | K8s name | Path | Rule |
|-----|----------|------|------|
| Catalog | `team-catalog` | `/app`, `/app/` | Own only exact landing |
| RoadPlay | `roadplay` | `/app/roadplay/` | Only touch `roadplay-*` |
| Link Road | `hackathon-app` | `/app/link-road/` | Never delete CM/secret |
| CrossWatch | `crosswatch` | `/app/crosswatch/` | Only touch `crosswatch-*` |
| Wind Watch | `windwatch` | `/app/wind` | **Never change paths or replace Ingress wholesale** |

Wind Watch must keep:

- `/app/(wind.*)`
- `/app/(api/wind.*)`
- `/app/(api/stream.*)`

When editing Ingress hosts, **add** `team-18-app.thecosmoslabs.com` alongside the lab host; do not remove Wind Watch path rules.

## Add a new project to the catalog

1. Deploy it under its own path (not catch-all `/app(/|$)(.*)`).
2. Append an entry to `tools/team-catalog/projects.json`.
3. Update only ConfigMap `team-catalog-code` and restart `deploy/team-catalog`.
