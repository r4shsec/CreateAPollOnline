# Dependencies | pip install -r requirements.txt
import os
import jwt
import time
import json
import uuid
import bcrypt
import sqlite3
import secrets
from haikunator import Haikunator
from flask_socketio import SocketIO, emit
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, make_response, url_for, jsonify, g, abort
from jwt.exceptions import InvalidSignatureError, ExpiredSignatureError, InvalidTokenError

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)
socketio = SocketIO(
    app, 
    cors_allowed_origins="*"
)
haikunator = Haikunator()

DATABASE = 'createapoll.db'

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

# Config
secret = os.getenv("SECRET_KEY") or secrets.token_hex(16)

MAX_OPTIONS = 10
MAX_QUESTION_LEN = 200
MAX_OPTION_LEN = 100

def gen_pass(password:str) -> str:
    password = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password, salt)
    return hashed_password

def verify_password(password:str, hash:str) -> str:
    if bcrypt.checkpw(password, hash):
        return True
    else:
        return False

class Flask_Database_Connector:
    def setup(self):
        conn = sqlite3.connect(DATABASE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pollDB (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                createdByName TEXT NOT NULL,
                createdByID TEXT NOT NULL,
                pollID TEXT NOT NULL UNIQUE,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                isPrivate BOOLEAN DEFAULT FALSE,
                passwordRequired BOOLEAN DEFAULT FALSE,
                password TEXT NULL
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            createdByName, createdByID, pollID, question,
            options, isPrivate, passwordRequired, password,
        ))
        conn.commit()
        conn.close()

    def getPoll(self, pollID):
        conn = get_db()
        row = conn.execute("SELECT * FROM pollDB WHERE pollID = ?", (pollID,)).fetchone()
        conn.close()
        if row is None:
            return None
        poll = dict(row)
        poll['options'] = json.loads(poll['options'])
        return poll

db = Flask_Database_Connector()
db.setup()

class Flask_JWT_Manager:
    @staticmethod
    def generate_jwt() -> str:
        encoded = jwt.encode(
            {
                "userID": str(uuid.uuid4()),
                "expired": str(datetime.now(tz=timezone.utc) + timedelta(days=3)),
                "username": str(haikunator.haikunate(token_length=5, delimiter='-'))
            }, 
            secret, 
            algorithm="HS256"
        )
        return encoded
    
    @staticmethod
    def validate_JWT(token:str) -> dict:
        decoded = jwt.decode(token, secret, algorithms=["HS256"])
        if decoded:
            if decoded.get('expired') < str(datetime.now(tz=timezone.utc)):
                return {
                    "error": "Expired Token"
                }
            return decoded
        else:
            return {
                "error": "Invalid Token"
            }

# Webserver

@app.before_request
def check_request_token():
    token = request.cookies.get('token')
    
    if not token:
        g.generate_new_token = True
    else:
        try:
            jwt.decode(token, secret, algorithms=["HS256"])
        except:
            g.clear_token = True

@app.after_request
def handle_response_cookies(response):
    if getattr(g, 'generate_new_token', False):
        response.set_cookie('token', Flask_JWT_Manager.generate_jwt(), httponly=True, secure=True)
        
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
    if not decoded or decoded.get('error'):
        return redirect(url_for('login'))

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
        return render_template(
            'poll_create.html',
            error=error, question=question, options=answers
        ), 400

    options = {
        f"option{i}": {
            "name": text,
            "votes": 0,
            "votesBy": [],
            "voteID": secrets.token_urlsafe(8),
        }
        for i, text in enumerate(answers, start=1)
    }

    poll_id = str(uuid.uuid4())
    db.createPoll(
        decoded.get('username'),
        decoded.get('userID'),
        poll_id,
        question,
        json.dumps(options),
        request.form.get('isPrivate') == 'on',
        bool(password),
        gen_pass(password) if password else None,
    )   

    return redirect(url_for('view_poll', poll_id=poll_id))

@app.route('/poll/<poll_id>', methods=['GET', 'POST'])
def view_poll(poll_id):
    poll = db.getPoll(poll_id)
    if poll is None:
        abort(404)
    print(type(poll['options']), repr(poll['options'])[:200])
    return render_template('poll_view.html', poll=poll)


if __name__ == '__main__':
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True
    )