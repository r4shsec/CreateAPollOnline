import os
import jwt
import time
import json
import uuid
import string
import bcrypt
import sqlite3
import secrets
from haikunator import Haikunator
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import Flask, render_template, request, redirect, make_response, url_for, jsonify, g, abort
from datetime import datetime, timedelta, timezone
from jwt.exceptions import InvalidSignatureError, ExpiredSignatureError, InvalidTokenError

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")
haikunator = Haikunator()

DATABASE = 'createapoll.db'
secret = "1op9smt_TX3fI27-L_QOpHfErqdUmqXXPgyIkAjUt3Y"

MAX_OPTIONS = 10
MAX_QUESTION_LEN = 200
MAX_OPTION_LEN = 100


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def gen_pass(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())


def verify_password(password: str, hash: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hash)


class Flask_Database_Connector:
    def setup(self):
        conn = sqlite3.connect(DATABASE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pollDB (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                createdByName TEXT NOT NULL,
                participants TEXT NULL,
                createdByID TEXT NOT NULL,
                pollID TEXT NOT NULL UNIQUE,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                isPrivate BOOLEAN DEFAULT FALSE,
                passwordRequired BOOLEAN DEFAULT FALSE,
                password TEXT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
            )
        """)
        conn.commit()
        conn.close()

    def createPoll(self, createdByName, createdByID, pollID, question,
                   options, isPrivate, passwordRequired, password):
        conn = get_db()
        conn.execute("""
            INSERT INTO pollDB (
                createdByName, createdByID, pollID, question,
                options, isPrivate, passwordRequired, password
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (createdByName, createdByID, pollID, question,
              options, isPrivate, passwordRequired, password))
        conn.commit()

    def getPoll(self, pollID):
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM pollDB WHERE pollID = ?", (pollID,)
        ).fetchone()
        if row is None:
            return None
        poll = dict(row)
        poll['options'] = json.loads(poll['options'])
        poll['participants'] = json.loads(poll['participants'] or '[]')
        poll['userID'] = poll['createdByID']
        poll['requirePassword'] = bool(poll['passwordRequired'])
        return poll

    def setPollStatus(self, pollID, status):
        conn = get_db()
        conn.execute(
            "UPDATE pollDB SET status = ? WHERE pollID = ?", (status, pollID)
        )
        conn.commit()

    def add_participant(self, pollID, userID, username):
        """Record that a user joined. Returns True if newly added."""
        conn = get_db()
        row = conn.execute(
            "SELECT participants FROM pollDB WHERE pollID = ?", (pollID,)
        ).fetchone()
        if row is None:
            return False
        participants = json.loads(row['participants'] or '[]')
        if any(p.get('userID') == userID for p in participants):
            return False
        participants.append({'userID': userID, 'username': username})
        conn.execute(
            "UPDATE pollDB SET participants = ? WHERE pollID = ?",
            (json.dumps(participants), pollID),
        )
        conn.commit()
        return True

    def castVote(self, pollID, userID, username, voteID):
        """Cast a vote. Returns (error, tally) — error is None on success."""
        conn = get_db()
        row = conn.execute(
            "SELECT options FROM pollDB WHERE pollID = ?", (pollID,)
        ).fetchone()
        if row is None:
            return "Poll not found.", None
        options = json.loads(row['options'])

        # Double-vote protection
        for opt in options.values():
            if any(v.get('userID') == userID for v in opt.get('votesBy', [])):
                return "You have already voted.", None

        # FIX: find the option whose voteID field matches, instead of
        # treating voteID as a dict key (keys are option1, option2, ...)
        target = next(
            (o for o in options.values() if o.get('voteID') == voteID), None
        )
        if target is None:
            return "Invalid option.", None

        target['votes'] = target.get('votes', 0) + 1
        target.setdefault('votesBy', []).append(
            {'userID': userID, 'username': username}
        )
        conn.execute(
            "UPDATE pollDB SET options = ? WHERE pollID = ?",
            (json.dumps(options), pollID),
        )
        conn.commit()
        return None, self.getTally(options)

    @staticmethod
    def getTally(options):
        return [{'voteID': o['voteID'], 'name': o['name'], 'votes': o.get('votes', 0)}
                for o in options.values()]


db = Flask_Database_Connector()
db.setup()


class Flask_JWT_Manager:
    @staticmethod
    def generate_jwt() -> str:
        return jwt.encode(
            {
                "userID": str(uuid.uuid4()),
                "expired": str(datetime.now(tz=timezone.utc) + timedelta(days=3)),
                "username": str(haikunator.haikunate(token_length=5, delimiter='-')),
            },
            secret,
            algorithm="HS256",
        )

    @staticmethod
    def validate_JWT(token: str) -> dict:
        if not token:
            return {"error": "Invalid Token"}
        try:
            decoded = jwt.decode(token, secret, algorithms=["HS256"])
        except Exception:
            return {"error": "Invalid Token"}
        if decoded.get('expired') < str(datetime.now(tz=timezone.utc)):
            return {"error": "Expired Token"}
        return decoded


# ---------- Live state (in-memory, per running server) ----------
# pollID -> {userID: username} of *currently connected* members
room_members = {}


def members_payload(pollID):
    members = room_members.get(pollID, {})
    return {
        'count': len(members),
        'members': [{'userID': uid, 'username': name} for uid, name in members.items()],
    }


@app.before_request
def check_request_token():
    token = request.cookies.get('token')
    if not token:
        g.generate_new_token = True
    else:
        try:
            jwt.decode(token, secret, algorithms=["HS256"])
        except Exception:
            g.clear_token = True


@app.after_request
def handle_response_cookies(response):
    if getattr(g, 'generate_new_token', False):
        response.set_cookie('token', Flask_JWT_Manager.generate_jwt(),
                            httponly=True, secure=True)
    if getattr(g, 'clear_token', False):
        response.delete_cookie('token')
    return response


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html'), 200


@app.route('/create', methods=['GET', 'POST'])
def create_poll():
    if request.method == 'GET':
        return render_template('poll_create.html')

    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        return redirect(url_for('index'))

    question = request.form.get('poll_name', '').strip()

    answers, seen = [], set()
    for raw in request.form.getlist('options'):
        text = raw.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            answers.append(text)

    error = None
    if not question or len(question) > MAX_QUESTION_LEN:
        error = f"Question is required (max {MAX_QUESTION_LEN} characters)."
    elif len(answers) < 2:
        error = "Please provide at least 2 different answers."
    elif len(answers) > MAX_OPTIONS:
        error = f"You can have at most {MAX_OPTIONS} answers."
    elif any(len(a) > MAX_OPTION_LEN for a in answers):
        error = f"Answers can be at most {MAX_OPTION_LEN} characters."

    password = request.form.get('password', '')
    if request.form.get('requirePassword') and not password:
        error = "Please enter a password or turn off password protection."

    if error:
        return render_template('poll_create.html', error=error,
                               question=question, options=answers), 400

    options = {
        f"option{i}": {
            "name": text,
            "votes": 0,
            "votesBy": [],
            "voteID": secrets.token_urlsafe(8),
        }
        for i, text in enumerate(answers, start=1)
    }

    poll_id = "".join(secrets.choice(string.digits) for _ in range(8))
    db.createPoll(
        decoded.get('username'), decoded.get('userID'), poll_id,
        question, json.dumps(options),
        request.form.get('isPrivate') == 'on',
        bool(password), gen_pass(password) if password else None,
    )
    return redirect(url_for('view_poll', poll_id=poll_id))


@app.route('/join', methods=['GET'])
def join_poll():
    poll_id = request.args.get('poll_id', '').strip()
    if not poll_id:
        return render_template('index.html', error="Poll ID is required."), 400
    poll = db.getPoll(poll_id)
    if poll is None:
        return render_template('index.html', error="Invalid Poll ID."), 400
    # /poll/<id> already handles owner vs participant vs not-started logic
    return redirect(url_for('view_poll', poll_id=poll_id))


@app.route('/poll/<poll_id>', methods=['GET'])
def view_poll(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        # token is auto-refreshed by after_request, so just render the page
        return redirect(url_for('view_poll', poll_id=poll_id))

    poll = db.getPoll(poll_id)
    if poll is None:
        abort(404)

    user_id = decoded.get('userID')
    username = decoded.get('username')
    is_owner = poll.get('userID') == user_id

    # Record participant presence (persisted), but owners don't count themselves
    if not is_owner:
        db.add_participant(poll_id, user_id, username)

    tally = Flask_Database_Connector.getTally(poll['options'])
    return render_template(
        'poll_view.html',
        poll=poll,
        isOwner=is_owner,
        notStarted=poll.get('status') != 'live',
        tallyJson=json.dumps(tally),
        username=username,
    )


@app.route('/poll/<poll_id>/start', methods=['POST'])
def start_poll(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        return redirect(url_for('view_poll', poll_id=poll_id))

    poll = db.getPoll(poll_id)
    if poll is None:
        abort(404)
    if poll.get('userID') != decoded.get('userID'):
        abort(403)

    db.setPollStatus(poll_id, 'live')

    poll = db.getPoll(poll_id)  # fresh copy
    socketio.emit(
        'poll_started',
        {'tally': Flask_Database_Connector.getTally(poll['options'])},
        to=f'poll_{poll_id}',
    )
    return redirect(url_for('view_poll', poll_id=poll_id))


# ---------------------- WebSocket events ----------------------

@socketio.on('join_poll')
def handle_join_poll(data):
    pollID = str(data.get('pollID', ''))
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        emit('socket_error', {'message': 'Invalid session. Reload the page.'})
        return

    poll = db.getPoll(pollID)
    if poll is None:
        emit('socket_error', {'message': 'Poll not found.'})
        return

    room = f'poll_{pollID}'
    join_room(room)

    userID = decoded['userID']
    username = decoded['username']

    room_members.setdefault(pollID, {})
    already_present = userID in room_members[pollID]
    room_members[pollID][userID] = username

    # Tell this client its own state, everyone else who joined
    if not already_present:
        socketio.emit('member_joined', {'username': username}, to=room)
    socketio.emit('members_update', members_payload(pollID), to=room)


@socketio.on('disconnect')
def handle_disconnect():
    # find which rooms this sid was in and clean up
    for pollID, members in list(room_members.items()):
        # we don't know userID from sid directly, so store reverse mapping
        pass


# sid -> (pollID, userID) reverse map for clean disconnects
sid_map = {}


@socketio.on('vote')
def handle_vote(data):
    pollID = str(data.get('pollID', ''))
    voteID = data.get('voteID')
    password = data.get('password', '')

    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        emit('vote_result', {'ok': False, 'message': 'Invalid session. Reload the page.'})
        return

    poll = db.getPoll(pollID)
    if poll is None:
        emit('vote_result', {'ok': False, 'message': 'Poll not found.'})
        return
    if poll.get('status') != 'live':
        emit('vote_result', {'ok': False, 'message': 'Vote has not started yet.'})
        return
    if poll.get('userID') == decoded.get('userID'):
        emit('vote_result', {'ok': False, 'message': "You can't vote on your own poll."})
        return
    if poll.get('requirePassword') and not verify_password(
            password, poll.get('password') or b''):
        emit('vote_result', {'ok': False, 'message': 'Wrong poll password.'})
        return

    error, tally = db.castVote(pollID, decoded['userID'],
                               decoded['username'], voteID)
    if error:
        emit('vote_result', {'ok': False, 'message': error})
        return

    emit('vote_result', {'ok': True, 'message': 'Vote counted!'})
    socketio.emit('tally_update', {'tally': tally}, to=f'poll_{pollID}')


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)