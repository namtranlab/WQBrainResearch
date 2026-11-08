# wq

The WorldQuant BRAIN research workflow, as a Claude Code plugin. One orchestrator skill runs the loop; each step is its own skill. Every hard rule lives in `research.py`, not here.

## The loop

```
/wq

0. setup     wq-setup      seeds, settings, policy → R init          CHECKPOINT
1. read      wq            R status, lessons, patterns, history
2. lessons   wq            judge, retire, add
3. batch     wq-generate   new ideas (parent null)
             wq-improve    variants: settings, playbook, correlation fixes
4. queue     wq            R queue, read the summary
5. submit    wq-submit     R daily_submit + a report per submitted alpha
```

## Where things live

| Path | Contains |
|---|---|
| `PROTOCOL.md` | Shared contracts: the rules the code enforces, the commands, file formats, grade lines |
| `skills/wq/` | Orchestrator: the research loop and continuation |
| `skills/wq-<step>/` | One per step |
| `templates/` | The submission report's shape |

All paths in the skills are relative to the research root (the folder holding `research.py`). Run from there.

## Install

```bash
/plugin marketplace add ./workflow
/plugin install wq@wq
```

`/plugin marketplace update wq` picks up local edits.

Unattended runs (`./autorun.sh`) do not use the plugin: they disable slash commands and point Claude at the skill files by path.

## Author

Nam Tran (vannam.tran@ntu.edu.sg)

## License

MIT. See `LICENSE`.
