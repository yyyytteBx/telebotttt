## Plan: Add `/help <command>` Support

Keep `/help` returning the full existing guide, while extending `help_cmd` to optionally accept one command argument and return only that command’s section. Implement this with a small, explicit command-topic mapping in `bot.py` to avoid risky parsing changes, preserve current behavior by default, and add lightweight validation plus a single focused commit.

### Steps
1. Audit current help flow in [bot.py](bot.py) around `_build_help_text` and `help_cmd`.
2. Add a command-topic catalog in [bot.py](bot.py) with aliases like `negvouch` and `resolvenegvouch`.
3. Extend `help_cmd` in [bot.py](bot.py) to parse optional `context.args` and normalize `/command` input.
4. Keep no-arg path unchanged (`_build_help_text`), and return topic-only help for one valid argument.
5. Add validation responses in [bot.py](bot.py) for unknown command and extra-argument misuse.
6. Update command docs in [README.md](README.md) and [BOT_DOCUMENTATION.md](BOT_DOCUMENTATION.md), then run syntax/static checks and commit one minimal diff.

### Further Considerations
1. Unknown topic UX: strict error only, or include “Did you mean” suggestions? Option A / Option B / Option C.
2. Scope choice: support canonical commands only, or aliases plus optional leading slash and `@botname`.
3. Commit strategy: single commit (`feat(help): support /help <command>`) vs docs in a separate follow-up commit.

