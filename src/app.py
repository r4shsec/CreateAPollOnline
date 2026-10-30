import os
import jwt
import time
import json
import math
import csv
import io
import uuid
import string
import bcrypt
import sqlite3
import secrets
import socket
from haikunator import Haikunator
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import Flask, render_template, request, redirect, make_response, url_for, jsonify, g, abort
from datetime import datetime, timedelta, timezone
from jwt.exceptions import InvalidSignatureError, ExpiredSignatureError, InvalidTokenError

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
haikunator = Haikunator()

with open('config.json') as f:
    config = json.load(f)

DATABASE = config['DB_NAME']
secret = os.environ.get('APP_SECRET')

MAX_OPTIONS = config['MAX_OPTIONS']
MAX_QUESTION_LEN = config['MAX_QUESTION_LEN']
MAX_OPTION_LEN = config['MAX_OPTION_LEN']


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
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hash)
    except (ValueError, TypeError):
        return False


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
                status TEXT NOT NULL DEFAULT 'draft',
                clubID TEXT NULL,
                clubRole TEXT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pollDB)")}
        if 'clubID' not in columns:
            conn.execute("ALTER TABLE pollDB ADD COLUMN clubID TEXT NULL")
        if 'clubRole' not in columns:
            conn.execute("ALTER TABLE pollDB ADD COLUMN clubRole TEXT NULL")
        # The app now supports private polls only. Existing public polls move
        # behind the same password and approval flow on the next startup.
        conn.execute("UPDATE pollDB SET isPrivate = 1 WHERE isPrivate = 0")
        conn.commit()
        conn.close()

    def createPoll(self, createdByName, createdByID, pollID, question,
                   options, isPrivate, passwordRequired, password,
                   clubID=None, clubRole=None):
        conn = get_db()
        conn.execute("""
            INSERT INTO pollDB (
                createdByName, createdByID, pollID, question,
                options, isPrivate, passwordRequired, password, clubID, clubRole
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (createdByName, createdByID, pollID, question,
              options, isPrivate, passwordRequired, password, clubID, clubRole))
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
        poll['requirePassword'] = False
        poll['isClubMode'] = poll.get('clubRole') == 'election'
        return poll

    def getClubPolls(self, clubID):
        conn = get_db()
        rows = conn.execute(
            "SELECT pollID FROM pollDB WHERE clubID = ? ORDER BY id", (clubID,)
        ).fetchall()
        return [self.getPoll(row['pollID']) for row in rows]

    def closeClub(self, clubID):
        conn = get_db()
        rows = conn.execute("SELECT pollID, participants FROM pollDB WHERE clubID = ?", (clubID,)).fetchall()
        for row in rows:
            participants = json.loads(row['participants'] or '[]')
            participants = [p for p in participants if p.get('status') != 'pending']
            conn.execute("UPDATE pollDB SET status = 'closed', participants = ? WHERE pollID = ?",
                         (json.dumps(participants), row['pollID']))
        conn.commit()

    def setPollStatus(self, pollID, status):
        conn = get_db()
        conn.execute(
            "UPDATE pollDB SET status = ? WHERE pollID = ?", (status, pollID)
        )
        conn.commit()

    def setPollPassword(self, pollID, password_hash):
        conn = get_db()
        conn.execute("UPDATE pollDB SET password = ? WHERE pollID = ?", (password_hash, pollID))
        conn.commit()

    def add_participant(self, pollID, userID, username, require_approval=False):
        """Record a participant; private polls keep new users pending."""
        conn = get_db()
        row = conn.execute(
            "SELECT participants FROM pollDB WHERE pollID = ?", (pollID,)
        ).fetchone()
        if row is None:
            return False
        participants = json.loads(row['participants'] or '[]')
        existing = next((p for p in participants if p.get('userID') == userID), None)
        if existing:
            if username:
                existing['username'] = username
                conn.execute(
                    "UPDATE pollDB SET participants = ? WHERE pollID = ?",
                    (json.dumps(participants), pollID),
                )
                conn.commit()
            return existing.get('status', 'approved') == 'approved'
        participants.append({'userID': userID, 'username': username,
                             'status': 'pending' if require_approval else 'approved'})
        conn.execute(
            "UPDATE pollDB SET participants = ? WHERE pollID = ?",
            (json.dumps(participants), pollID),
        )
        conn.commit()
        return not require_approval

    def set_participant_status(self, pollID, userID, status):
        conn = get_db()
        row = conn.execute("SELECT participants FROM pollDB WHERE pollID = ?", (pollID,)).fetchone()
        if row is None:
            return False
        participants = json.loads(row['participants'] or '[]')
        found = False
        for participant in participants:
            if participant.get('userID') == userID:
                found = True
                if status == 'cancelled':
                    participants.remove(participant)
                else:
                    participant['status'] = status
                break
        if found:
            conn.execute("UPDATE pollDB SET participants = ? WHERE pollID = ?", (json.dumps(participants), pollID))
            conn.commit()
        return found

    def set_participant_power(self, pollID, userID, power, mode):
        conn = get_db()
        row = conn.execute("SELECT participants FROM pollDB WHERE pollID = ?", (pollID,)).fetchone()
        if row is None:
            return False
        participants = json.loads(row['participants'] or '[]')
        for participant in participants:
            if participant.get('userID') == userID and participant.get('status') == 'approved':
                participant['votingPower'] = power
                participant['votingPowerMode'] = mode
                conn.execute("UPDATE pollDB SET participants = ? WHERE pollID = ?", (json.dumps(participants), pollID))
                conn.commit()
                return True
        return False

    def castVote(self, pollID, userID, username, voteID, weight=1):
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

        target['votes'] = target.get('votes', 0) + weight
        target.setdefault('votesBy', []).append(
            {'userID': userID, 'username': username, 'weight': weight}
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
    poll = db.getPoll(pollID)
    members = [
        {'userID': p.get('userID'), 'username': p.get('username', 'Member')}
        for p in (poll or {}).get('participants', [])
        if p.get('status', 'approved') == 'approved'
    ]
    return {
        'count': len(members),
        'members': members,
    }


def voting_power_members_payload(poll):
    return {
        'members': [
            {
                'userID': p.get('userID'),
                'username': p.get('username', 'Member'),
                'votingPower': p.get('votingPower', 1),
                'votingPowerMode': p.get('votingPowerMode', 'votes'),
            }
            for p in poll.get('participants', [])
            if p.get('status', 'approved') == 'approved'
        ]
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
    cookie_secure = request.is_secure
    if getattr(g, 'generate_new_token', False):
        response.set_cookie('token', Flask_JWT_Manager.generate_jwt(),
                            httponly=True, secure=cookie_secure, samesite='Lax')
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

    club_mode = request.form.get('clubMode') == 'on'
    error = None
    if not question or len(question) > MAX_QUESTION_LEN:
        error = f"Question is required (max {MAX_QUESTION_LEN} characters)."
    elif club_mode and len(answers) != 4:
        error = "Club Mode requires exactly 4 candidate names."
    elif len(answers) < 2:
        error = "Please provide at least 2 different answers."
    elif len(answers) > MAX_OPTIONS:
        error = f"You can have at most {MAX_OPTIONS} answers."
    elif any(len(a) > MAX_OPTION_LEN for a in answers):
        error = f"Answers can be at most {MAX_OPTION_LEN} characters."

    password = request.form.get('password', '')
    is_private = request.form.get('isPrivate') == 'on'
    password_required = False
    if is_private and not password:
        error = "A password is required to join a private poll."

    club_id = None

    if error:
        return render_template('poll_create.html', error=error,
                               question=question, options=answers,
                               isPrivate=is_private,
                               requirePassword=password_required,
                               clubMode=club_mode), 400

    roles = ['election'] if club_mode else [None]
    first_poll_id = None
    password_hash = gen_pass(password) if is_private and password else None
    for role in roles:
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
        if first_poll_id is None:
            first_poll_id = poll_id
        poll_question = f"{question} — Club Election" if role else question
        db.createPoll(
            decoded.get('username'), decoded.get('userID'), poll_id,
            poll_question, json.dumps(options), is_private,
            password_required, password_hash, club_id, role,
        )
    return redirect(url_for('view_poll', poll_id=first_poll_id))


def build_club_results(polls):
    results = {}
    for poll in polls:
        tally = Flask_Database_Connector.getTally(poll['options'])
        high = max((item['votes'] for item in tally), default=0)
        winners = [item['name'] for item in tally if high > 0 and item['votes'] == high]
        results[poll['clubRole']] = {
            'winner': ', '.join(winners) if winners else 'No votes cast',
            'tie': len(winners) > 1,
        }
    return results


def club_officers_by_vote_count(poll):
    options = list(poll['options'].values())
    ordered = sorted(options, key=lambda option: (-option.get('votes', 0), options.index(option)))
    roles = ['President', 'Vice President', 'Secretary', 'Treasurer']
    return [{'role': role, 'name': option['name'], 'votes': option.get('votes', 0)}
            for role, option in zip(roles, ordered)]


def club_vote_progress(polls):
    progress = {}
    for poll in polls:
        approved = {
            p.get('userID') for p in poll['participants']
            if p.get('status', 'approved') == 'approved'
        }
        voted = {
            vote.get('userID')
            for option in poll['options'].values()
            for vote in option.get('votesBy', [])
        }
        progress[poll['clubRole']] = {
            'eligible': len(approved),
            'voted': len(approved & voted),
            'complete': bool(approved) and approved.issubset(voted),
        }
    return progress


@app.route('/club/<club_id>')
def view_club(club_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        abort(403)
    polls = db.getClubPolls(club_id)
    if len(polls) != 4:
        abort(404)
    is_owner = polls[0]['userID'] == decoded.get('userID')
    all_closed = all(poll['status'] == 'closed' for poll in polls)
    progress = club_vote_progress(polls)
    all_voted = all(item['complete'] for item in progress.values())
    results = build_club_results(polls) if all_closed else {}
    return render_template('club_view.html', clubID=club_id, polls=polls,
                           isOwner=is_owner, allClosed=all_closed, allVoted=all_voted,
                           progress=progress, results=results)


@app.route('/club/<club_id>/start', methods=['POST'])
def start_club(club_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    polls = db.getClubPolls(club_id)
    if not polls or polls[0]['userID'] != decoded.get('userID'):
        abort(403)
    if any(poll['status'] == 'closed' for poll in polls):
        abort(400, description='This club vote has already closed.')
    for poll in polls:
        if poll['status'] == 'draft':
            db.setPollStatus(poll['pollID'], 'live')
            socketio.emit('poll_started', {
                'tally': Flask_Database_Connector.getTally(poll['options'])
            }, to=f"poll_{poll['pollID']}")
    return redirect(url_for('view_club', club_id=club_id))


@app.route('/club/<club_id>/finish', methods=['POST'])
def finish_club(club_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    polls = db.getClubPolls(club_id)
    if not polls or polls[0]['userID'] != decoded.get('userID'):
        abort(403)
    if not all(poll['status'] == 'live' for poll in polls):
        abort(400, description='Start all four votes before closing the club election.')
    if not all(item['complete'] for item in club_vote_progress(polls).values()):
        abort(400, description='All admitted members must vote in all four roles before results are revealed.')
    for poll in polls:
        for member in poll['participants']:
            if member.get('status') == 'pending':
                socketio.emit('member_status', {
                    'userID': member.get('userID'), 'status': 'cancelled',
                    'message': 'Voting has ended before your request was admitted.'
                }, to=f"pending_{poll['pollID']}_{member.get('userID')}")
    db.closeClub(club_id)
    for poll in polls:
        socketio.emit('poll_ended', {
            'tally': Flask_Database_Connector.getTally(poll['options'])
        }, to=f"poll_{poll['pollID']}")
    return redirect(url_for('view_club', club_id=club_id))


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


@app.route('/poll/<poll_id>/request-join', methods=['POST'])
def request_to_join(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        return redirect(url_for('view_poll', poll_id=poll_id))
    poll = db.getPoll(poll_id)
    if poll is None:
        abort(404)
    if poll.get('userID') == decoded.get('userID'):
        return redirect(url_for('view_poll', poll_id=poll_id))
    if poll.get('status') == 'closed':
        abort(400, description='Voting has ended.')
    member_name = request.form.get('member_name', '').strip()
    if not member_name or len(member_name) > 60:
        return render_template('poll_view.html', poll=poll, isOwner=False,
                               notStarted=poll.get('status') != 'live',
                               participantApproved=False, participantJoined=False,
                               tallyJson='[]', username=decoded.get('username'),
                               joinedName=member_name,
                               error='Enter a name up to 60 characters.')
    if poll.get('password') and not verify_password(request.form.get('password', ''), poll['password']):
        return render_template('poll_view.html', poll=poll, isOwner=False,
                               notStarted=poll.get('status') != 'live',
                               participantApproved=False, participantJoined=False,
                               tallyJson='[]',
                               username=decoded.get('username'), joinedName=member_name,
                               error='Incorrect password.')
    db.add_participant(poll_id, decoded['userID'], member_name,
                                  require_approval=bool(poll.get('isPrivate')))
    if poll.get('isPrivate'):
        pending = [p for p in db.getPoll(poll_id)['participants'] if p.get('status') == 'pending']
        socketio.emit('join_requests', {'members': pending}, to=f'poll_{poll_id}')
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
    participant = next((p for p in poll.get('participants', []) if p.get('userID') == user_id), None)
    participant_joined = bool(is_owner or participant or not poll.get('isPrivate'))
    participant_approved = bool(is_owner or not poll.get('isPrivate') or
                                (participant and participant.get('status') == 'approved'))
    if not is_owner:
        if not poll.get('isPrivate'):
            participant_approved = db.add_participant(poll_id, user_id, username)

    tally = Flask_Database_Connector.getTally(poll['options']) if participant_approved else []
    club_results = (club_officers_by_vote_count(poll)
                    if poll.get('isClubMode') and poll.get('status') == 'closed' else [])
    return render_template(
        'poll_view.html',
        poll=poll,
        isOwner=is_owner,
        notStarted=poll.get('status') == 'draft',
        votingClosed=poll.get('status') == 'closed',
        tallyJson=json.dumps(tally),
        clubResults=club_results,
        username=username,
        participantApproved=participant_approved,
        participantJoined=participant_joined,
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


@app.route('/poll/<poll_id>/end', methods=['POST'])
def end_poll(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    poll = db.getPoll(poll_id)
    if decoded.get('error') or poll is None or poll.get('userID') != decoded.get('userID'):
        abort(403)
    if poll.get('status') != 'live':
        abort(400)
    if poll.get('isClubMode'):
        if not any(option.get('votes', 0) for option in poll['options'].values()):
            abort(400, description='At least one member must vote before the election can end.')
    for participant in poll['participants']:
        if participant.get('status') == 'pending':
            socketio.emit('member_status', {
                'userID': participant.get('userID'), 'status': 'cancelled',
                'message': 'Voting has ended before your request was admitted.'
            }, to=f"pending_{poll_id}_{participant.get('userID')}")
            db.set_participant_status(poll_id, participant.get('userID'), 'cancelled')
    db.setPollStatus(poll_id, 'closed')
    poll = db.getPoll(poll_id)
    ended_payload = {
        'tally': Flask_Database_Connector.getTally(poll['options']),
    }
    if poll.get('isClubMode'):
        ended_payload['officers'] = club_officers_by_vote_count(poll)
    socketio.emit('poll_ended', ended_payload, to=f'poll_{poll_id}')
    return redirect(url_for('view_poll', poll_id=poll_id))


@app.route('/poll/<poll_id>/export.csv', methods=['GET'])
def export_poll_results(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    poll = db.getPoll(poll_id)
    if decoded.get('error') or poll is None or poll.get('userID') != decoded.get('userID'):
        abort(403)
    if poll.get('status') != 'closed':
        abort(400, description='Results can be exported after voting is closed.')

    output = io.StringIO(newline='')
    writer = csv.writer(output)
    question = str(poll.get('question', ''))
    if question.startswith(('=', '+', '-', '@', '\t', '\r')):
        question = "'" + question
    writer.writerow(['Poll', question])
    writer.writerow(['Status', 'Closed'])
    writer.writerow(['Option', 'Weighted votes', 'Ballots'])
    for option in poll['options'].values():
        name = str(option.get('name', ''))
        if name.startswith(('=', '+', '-', '@', '\t', '\r')):
            name = "'" + name
        writer.writerow([
            name,
            option.get('votes', 0),
            len(option.get('votesBy', [])),
        ])

    response = make_response('\ufeff' + output.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename="poll-results-{poll_id}.csv"'
    return response


@app.route('/poll/<poll_id>/password', methods=['POST'])
def set_poll_password(poll_id):
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    poll = db.getPoll(poll_id)
    if decoded.get('error') or poll is None or poll.get('userID') != decoded.get('userID'):
        abort(403)
    password = request.form.get('password', '')
    if len(password) < 1:
        abort(400, description='A password is required.')
    db.setPollPassword(poll_id, gen_pass(password))
    return redirect(url_for('view_poll', poll_id=poll_id))


# ---------------------- WebSocket events ----------------------

@socketio.on('set_member_power')
def handle_set_member_power(data):
    pollID = str(data.get('pollID', ''))
    userID = str(data.get('userID', ''))
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    poll = db.getPoll(pollID)
    if decoded.get('error') or poll is None or poll.get('userID') != decoded.get('userID'):
        emit('voting_power_result', {
            'ok': False, 'userID': userID, 'message': 'Only the poll host can change voting power.'
        })
        return
    if not poll.get('isPrivate') or poll.get('isClubMode'):
        emit('voting_power_result', {
            'ok': False, 'userID': userID, 'message': 'Voting power is only available for private polls.'
        })
        return

    has_voted = any(
        any(vote.get('userID') == userID for vote in option.get('votesBy', []))
        for option in poll.get('options', {}).values()
    )
    if has_voted:
        emit('voting_power_result', {
            'ok': False, 'userID': userID,
            'message': 'Voting power cannot be changed after this member has voted.'
        })
        return

    mode = data.get('votingPowerMode', 'votes')
    try:
        power = float(data.get('votingPower', '1'))
        if (not math.isfinite(power) or mode not in ('votes', 'percent') or
                not 1 <= power <= (100 if mode == 'percent' else 1000) or
                (mode == 'votes' and not power.is_integer())):
            raise ValueError()
    except (TypeError, ValueError):
        emit('voting_power_result', {
            'ok': False, 'userID': userID,
            'message': 'Use 1–1000 whole votes or a percentage from 1–100.'
        })
        return

    if not db.set_participant_power(pollID, userID, power, mode):
        emit('voting_power_result', {
            'ok': False, 'userID': userID, 'message': 'Approved member not found.'
        })
        return

    socketio.emit('voting_power_updated', {
        'userID': userID, 'votingPower': power, 'votingPowerMode': mode,
    }, to=f'poll_{pollID}')
    emit('voting_power_result', {
        'ok': True, 'userID': userID, 'message': 'Voting power saved.'
    })

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
    userID = decoded['userID']
    username = decoded['username']

    approved = (poll.get('userID') == userID or not poll.get('isPrivate') or
                any(p.get('userID') == userID and p.get('status', 'approved') == 'approved'
                    for p in poll.get('participants', [])))
    if not approved:
        participant = next((p for p in poll.get('participants', []) if p.get('userID') == userID), None)
        if participant:
            join_room(f'pending_{pollID}_{userID}')
            emit('join_pending', {'message': 'Waiting for the host to approve your request.'})
        else:
            emit('join_required', {'message': 'Enter this private poll\'s password to request access.'})
        return

    join_room(room)

    room_members.setdefault(pollID, {})
    already_present = userID in room_members[pollID]
    participant = next((p for p in poll.get('participants', []) if p.get('userID') == userID), None)
    display_name = participant.get('username', username) if participant else username
    room_members[pollID][userID] = display_name

    # Tell this client its own state, everyone else who joined
    if not already_present:
        socketio.emit('member_joined', {'username': display_name}, to=room)
    socketio.emit('members_update', members_payload(pollID), to=room)
    if poll.get('userID') == userID:
        pending = [p for p in poll.get('participants', []) if p.get('status') == 'pending']
        emit('join_requests', {'members': pending})
        if poll.get('isPrivate') and not poll.get('isClubMode'):
            emit('voting_power_members', voting_power_members_payload(poll))


@socketio.on('member_decision')
def handle_member_decision(data):
    pollID = str(data.get('pollID', ''))
    userID = str(data.get('userID', ''))
    decision = data.get('decision')
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    poll = db.getPoll(pollID)
    if decoded.get('error') or poll is None or poll.get('userID') != decoded.get('userID'):
        emit('socket_error', {'message': 'Only the poll host can admit or deny members.'})
        return
    if poll.get('status') == 'closed':
        emit('socket_error', {'message': 'This poll is already closed.'})
        return
    if decision not in ('admit', 'deny'):
        emit('socket_error', {'message': 'Invalid member decision.'})
        return
    pending = next((p for p in poll.get('participants', [])
                    if p.get('userID') == userID and p.get('status') == 'pending'), None)
    if not pending:
        emit('socket_error', {'message': 'That join request is no longer pending.'})
        return
    status = 'approved' if decision == 'admit' else 'cancelled'
    db.set_participant_status(pollID, userID, status)
    socketio.emit('member_status', {
        'userID': userID, 'status': status,
        'message': 'The host denied your join request.' if status == 'cancelled' else ''
    },
                  to=f'pending_{pollID}_{userID}')
    updated = db.getPoll(pollID)
    socketio.emit('join_requests', {
        'members': [p for p in updated['participants'] if p.get('status') == 'pending']
    }, to=f'poll_{pollID}')
    socketio.emit('members_update', members_payload(pollID), to=f'poll_{pollID}')
    if poll.get('isPrivate') and not poll.get('isClubMode'):
        socketio.emit('voting_power_members', voting_power_members_payload(updated), to=f'poll_{pollID}')


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
    decoded = Flask_JWT_Manager.validate_JWT(request.cookies.get('token'))
    if decoded.get('error'):
        emit('vote_result', {'ok': False, 'message': 'Invalid session. Reload the page.'})
        return

    poll = db.getPoll(pollID)
    if poll is None:
        emit('vote_result', {'ok': False, 'message': 'Poll not found.'})
        return
    if poll.get('status') != 'live':
        message = 'Voting is closed.' if poll.get('status') == 'closed' else 'Vote has not started yet.'
        emit('vote_result', {'ok': False, 'message': message})
        return
    if poll.get('userID') == decoded.get('userID'):
        emit('vote_result', {'ok': False, 'message': "You can't vote on your own poll."})
        return
    if poll.get('isPrivate') and not any(p.get('userID') == decoded.get('userID') and p.get('status', 'approved') == 'approved' for p in poll.get('participants', [])):
        emit('vote_result', {'ok': False, 'message': 'Wait for the host to approve your request to join.'})
        return
    participant = next((p for p in poll.get('participants', []) if p.get('userID') == decoded.get('userID')), None)
    voter_name = participant.get('username', decoded['username']) if participant else decoded['username']
    weight = float(participant.get('votingPower', 1)) if poll.get('isPrivate') and participant else 1
    if poll.get('isPrivate') and participant and participant.get('votingPowerMode') == 'percent':
        weight /= 100
    error, tally = db.castVote(pollID, decoded['userID'], voter_name, voteID, weight)
    if error:
        emit('vote_result', {'ok': False, 'message': error})
        return

    emit('vote_result', {'ok': True, 'message': 'Vote counted!'})
    socketio.emit('tally_update', {'tally': tally}, to=f'poll_{pollID}')


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')
