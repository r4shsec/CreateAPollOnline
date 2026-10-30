# Create A Poll Online

A Flask app for creating polls, inviting people to vote, and watching results update live. Polls can be public or private. Private polls support a join password, host approval, and per-member voting power.

![Create A Poll logo](src/static/images/logo.svg)

## Features

- Create public polls or password-protected private polls.
- Let a host approve or deny private poll join requests.
- Set approved members' voting power as whole votes or a percentage.
- Run club elections with four candidate names.
- Export closed poll results to CSV.

## Configuration

The app reads `config.json` from its working directory. `config.json.example` contains the application limits and default database name. Keep the real `config.json` private; it is ignored by Git.

| Setting | Default | Description |
| --- | --- | --- |
| `APP_SECRET` environment variable | `APP_SECRET` in `config.json` | Secret used to sign session tokens. Set a unique random value for every deployment. |
| `DB_NAME` environment variable | `DB_NAME` in `config.json` | SQLite database file path. The parent directory must exist and be writable by the app user. |
| `MAX_OPTIONS` | `10` | Maximum number of answers in a poll. |
| `MAX_QUESTION_LEN` | `200` | Maximum poll question length. |
| `MAX_OPTION_LEN` | `100` | Maximum answer length. |

## Project layout

```text
.
├── config.json.example
├── README.md
└── src/
    ├── app.py
    ├── requirements.txt
    ├── static/
    └── templates/
```
