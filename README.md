# Create A Poll Online

A Flask app for creating polls, inviting people to vote, and watching results update live. Polls can be public or private. Private polls support a join password, host approval, and per-member voting power.

![Create A Poll logo](src/static/images/logo.svg)

## Features

- Create public polls or password-protected private polls.
- Let a host approve or deny private poll join requests.
- Set approved members' voting power as whole votes or a percentage.
- Run club elections with four candidate names.
- Export closed poll results to CSV.

## Install and run with Gunicorn

Gunicorn is for Linux and macOS. The commands below assume Python 3.12 or newer and that you run them from the repository root unless noted.

1. Create and activate a virtual environment, then install the app dependencies:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r src/requirements.txt
   ```

2. Create the app configuration file:

   ```sh
   cp config.json.example src/config.json
   ```

3. Set a unique secret and start the app from the `src` directory:

   ```sh
   cd src
   export APP_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
   gunicorn --no-control-socket --worker-class gthread --workers 1 --threads 100 --bind 0.0.0.0:5000 app:app
   ```

4. Open [http://localhost:5000](http://localhost:5000).

Keep the Gunicorn process running to serve the app.

## Run locally on Windows

Gunicorn does not run natively on Windows. Use Flask-SocketIO's development server for local development:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r src\requirements.txt
Copy-Item config.json.example src\config.json
Set-Location src
$env:APP_SECRET = py -c "import secrets; print(secrets.token_urlsafe(48))"
py app.py
```

Open [http://localhost:5000](http://localhost:5000). The local SQLite database defaults to `src/createapoll.db`.

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
