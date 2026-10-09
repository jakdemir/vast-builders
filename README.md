# Vast Builder — Team 18

Apps built on the VSS video understanding stack for the Vast Builders Challenge.

**Live catalog:** https://team-18-app.thecosmoslabs.com/app/

## Apps

| App | Path | Live URL path | Description |
|-----|------|---------------|-------------|
| **Builders Catalog** | `tools/team-catalog/` | `/app/` | Landing page listing team projects |
| **RoadPlay** | `tools/roadplay/` | `/app/roadplay/` | NL-pick a road clip → pixelized video world with live YOLO movers + missions |
| **Link Road** | `tools/link-road/` | `/app/link-road/` | Road / obstruction agent — ask, ingest, and search street scenes |
| **CrossWatch** | `tools/crosswatch/` | `/app/crosswatch/` | Jaywalk & against-signal case board from re-ingested street cameras |
| **Wind Watch** | `tools/windwatch/` | `/app/wind` | Find archive clips with wind cues — sweep, cues, or custom query |

## Run

Credentials come from the workshop environment (`VSS_URL`, `VSS_USERNAME`, `VSS_PASSWORD`).

**Python apps** (catalog, RoadPlay, CrossWatch, Wind Watch):

```bash
cd tools/<app>
pip install -r requirements.txt
python main.py
```

**Link Road** (Node):

```bash
cd tools/link-road
# or unpack: tar xzf bundle.tgz
cd server && npm install --omit=dev
# see start.sh for env wiring
```
