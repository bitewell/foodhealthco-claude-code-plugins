# bitewell/foodhealthco-claude-code-plugins

Claude Code plugins for the FoodHealth Co engineering org. Distributed as a [marketplace](https://docs.claude.com/en/docs/claude-code/plugins) so members install once and pull updates automatically.

## Install

Add the marketplace once, then install the plugin(s) you want. In any Claude Code session:

```
/plugin marketplace add bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-prototype@foodhealthco
/plugin install foodhealthco-db-connect@foodhealthco
/plugin install foodhealthco-ndo-ops@foodhealthco
```

Each plugin auto-triggers based on the context of the repo you're working in or the question you're asking — see the individual plugin READMEs for usage and prerequisites.

## Plugins

| Name | What it does |
|---|---|
| [`foodhealthco-prototype`](plugins/foodhealthco-prototype/) | Prototype features in the FHS mobile app + Chrome extension. Detects the repo (mobile vs extension), runs prereq checks, starts the right dev server, references repo conventions, and verifies (`type-check` / `lint`) before handing back. |
| [`foodhealthco-db-connect`](plugins/foodhealthco-db-connect/) | Connect to NDO Postgres (DigitalOcean) and HeroDB (GCP Cloud SQL) from a local machine. Handles cloud-sql-proxy lifecycle, credential pulls from Dagster Cloud secrets, and standard `psql` invocations. |
| [`foodhealthco-ndo-ops`](plugins/foodhealthco-ndo-ops/) | Run nutrition-data-ops scoring/tagging/ingestion management commands (19 wrapped) with CSV upload to DO Spaces, preflight read-outs that bucket inputs into update/skip/block before any write, prod opt-in, and postflight verification. See [SCORING_OPS_GUIDE.md](plugins/foodhealthco-ndo-ops/docs/SCORING_OPS_GUIDE.md) for the dietitian-facing setup walkthrough. |

## Local development

To iterate on the marketplace itself without pushing:

```
/plugin marketplace add /absolute/path/to/this/repo
/plugin install <plugin-name>@foodhealthco
```

After editing a skill, run `/reload-plugins` (or restart Claude Code) to pick up changes.

## Adding a new plugin

1. Create `plugins/<plugin-name>/.claude-plugin/plugin.json`.
2. Add a top-level `plugins/<plugin-name>/README.md` describing the plugin.
3. Add skills under `plugins/<plugin-name>/skills/<skill-name>/SKILL.md`.
4. Append the plugin to `.claude-plugin/marketplace.json#plugins`.
5. Bump the plugin's `version` in `plugin.json`.
6. Update this README to list the new plugin in the table above.
