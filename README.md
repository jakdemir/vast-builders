# Vast Builder — Team 18

Apps built on the VSS video understanding stack for the Vast Builders Challenge.

**Live catalog:** https://team-18-app.thecosmoslabs.com/app/

## Apps in this repo

| App | Path | Description |
|-----|------|-------------|
| **Builders Catalog** | `tools/team-catalog/` | Landing page listing team projects |
| **RoadPlay** | `tools/roadplay/` | NL-pick a road clip → pixelized video world with live YOLO movers + missions |
| **CrossWatch** | `tools/crosswatch/` | Jaywalk & against-signal case board from re-ingested street cameras |

## Other team apps (deployed, not in this repo)

| App | URL path |
|-----|----------|
| Link Road | `/app/link-road/` |
| Wind Watch | `/app/wind` |

## Run locally

Each app is a Flask service. Credentials come from the workshop environment (`VSS_URL`, `VSS_USERNAME`, `VSS_PASSWORD`).

```bash
cd tools/<app>
pip install -r requirements.txt
python main.py
```
