# bitewell/foodhealthco-claude-code-plugins

Claude Code plugins for the FoodHealth Co engineering org. Distributed as a [marketplace](https://docs.claude.com/en/docs/claude-code/plugins) so members install once and pull updates automatically.

## Install

In any Claude Code session, run:

```
/plugin marketplace add bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-prototype@foodhealthco
```

Then start a session inside `fhs-mobile-app` or `foodhealth-score-extension` and ask Claude to "prototype" something — the skill will auto-trigger.

## Plugins

| Name | What it does |
|---|---|
| [`foodhealthco-prototype`](plugins/foodhealthco-prototype/) | Detects the repo (mobile vs extension), runs prereq checks, starts the right dev server, references repo conventions, and verifies (`type-check` / `lint`) before handing back. |

## Local development

To iterate on the marketplace itself without pushing:

```
/plugin marketplace add /absolute/path/to/this/repo
/plugin install foodhealthco-prototype@foodhealthco
```

After editing a skill, run `/reload-plugins` (or restart Claude Code) to pick up changes.

## Adding a new plugin

1. Create `plugins/<plugin-name>/.claude-plugin/plugin.json`.
2. Add skills under `plugins/<plugin-name>/skills/<skill-name>/SKILL.md`.
3. Append the plugin to `.claude-plugin/marketplace.json#plugins`.
4. Bump the plugin's `version` in `plugin.json`.
