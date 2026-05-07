# TOOLS.example.md - Local Operator Notes

Copy this file to `TOOLS.md` if you want a local, git-ignored place for
machine-specific notes.

Keep real infrastructure details out of the public repository. Good candidates
for a private `TOOLS.md` include:

- SSH aliases and hostnames
- GPU/server names
- camera or microphone device names
- preferred local model names
- deployment runbooks for your own instance
- local browser, TTS, or automation preferences

Example:

```markdown
## Local Deploy

- Host: example-user@example-host
- Checkout: ~/illo-brain
- Env file: ~/.config/illo-brain/production.env

## Browser Runtime

- Chrome binary: /path/to/chrome
- Browser Harness binary: /path/to/browser-harness
```
