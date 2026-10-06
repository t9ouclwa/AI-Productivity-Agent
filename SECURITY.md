# Security Policy

## Sensitive local files

DOCTOR handles credentials and personal context locally. Never commit or share:

- `.env` or Telegram bot tokens
- `credentials.json` or `token.json`
- chat IDs, chat history, reminder/check-in state, or downloaded media
- customized files under `~/.config/doctor/`
- private Obsidian vault content

The repository `.gitignore` covers the standard runtime filenames, but contributors must still inspect staged changes before every commit.

## OAuth scope

The Gmail integration requests `https://www.googleapis.com/auth/gmail.readonly`. Changes that broaden this scope should receive explicit security review.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or a private security advisory when available. Do not include real credentials, tokens, private messages, or personal vault content in a public issue.
