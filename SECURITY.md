# Security

## Report a vulnerability

Do not open a public issue for a vulnerability or leaked credential. Use GitHub's private vulnerability reporting for this repository.

## Secrets

Never commit keys, tokens, passwords, OAuth state, credential files, databases, session transcripts, or backups. Use `fleet auth set NAME` for MCP secrets.

Before every push, run:

```bash
./bin/fleet check
```

