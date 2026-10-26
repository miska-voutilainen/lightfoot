---
description: 'How to work with context that Lightfoot condensed. Use when a tool result or a message contains a "[lightfoot: ...]" note, or the conversation starts with a lightfoot summary of earlier turns.'
user-invocable: false
---

Lightfoot is a local proxy that condenses what this session re-sends on every turn. Nothing it removes is lost: every note names the file that holds the full text. Treat a note as a pointer, not as missing information.

| Note | What happened | What to do when you need the detail |
| --- | --- | --- |
| `output trimmed to N of M lines … Full output: PATH` | A large output kept its head, its tail and every error line | Read PATH with an offset and limit for the lines you need. Don't re-run the command just to see it again |
| `older output shortened … Full output: PATH` | An old output nothing later referred to was shortened | Read PATH |
| `lines A-B omitted` | Part of a trimmed output | Read those lines from the named full-output file |
| `superseded read of FILE` | The file was read again later; the newer copy is current | Use the newer read. Re-read FILE before editing it if you haven't since |
| `this TOOL call was repeated …` / `older search results reduced …` | A repeated or old search | Use the later result, or search again if the code has changed |
| `older image removed … Saved copy: PATH` | An old screenshot or image | Read PATH to view it again |
| `previous line repeated N more times` | Identical lines were collapsed | Nothing; the count is exact |
| A summary at the start that names a `.lightfoot/history/` file | The oldest turns were replaced by a summary; the user's requests in it are verbatim | Trust the summary for decisions and progress. For an exact earlier detail (a command's output, a value, a message), read the history file. Search it before asking the user to repeat something |

Before editing a file you only know from the summary or a stub, read it again: it may have changed since.
