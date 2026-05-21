# Related Linear tickets

Filed alongside the `ndo-run` skill, all under **Platform (Data / Infra / Ops)**:

| ID | Title | Why it exists |
|---|---|---|
| [ENG-895](https://linear.app/foodhealthco/issue/ENG-895) | Automate ingestion of client match requests → `match_products` | Skill handles the *manual* console step today; this closes the loop so client requests don't need a human to kick off matching. |
| [ENG-896](https://linear.app/foodhealthco/issue/ENG-896) | Automate ingestion of nutrition profiles → `backfill_fhs_and_refresh_view_command` | Same shape as above but for FHS scoring + index refresh when new profiles land. Highest-frequency manual ops task today. |
| [ENG-897](https://linear.app/foodhealthco/issue/ENG-897) | Wire `--db` routing through NDO management commands (HeroDB support) | The skill's `--db platform` flag is plumbing-ready but the NDO commands don't route queries through `DBAlias.PLATFORM`. Until this lands, `--db platform` prints a warning and requires `--force`. |

When ENG-897 closes: remove the `warn_herodb` banner from [scripts/ndo_run.py](../scripts/ndo_run.py) and update [SKILL.md](../SKILL.md) to drop the "BLOCKED" language around `--db platform`.
