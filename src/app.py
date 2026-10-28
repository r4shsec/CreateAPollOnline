# Dependencies | pip install -r requirements.txt
import os
import jwt
import time
import json
import uuid
import sqlite3
import secrets
from haikunator import Haikunator
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, make_response, url_for, jsonify, g
from jwt.exceptions import InvalidSignatureError, ExpiredSignatureError, InvalidTokenError

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)
haikunator = Haikunator()

# Config
secret = os.getenv("SECRET_KEY") or secrets.token_hex(16)

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
        validation_result = Flask_JWT_Manager.validate_JWT(token)
        if validation_result.get('error') == 'Invalid Token':
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

# Application Programming Interface (API)

if __name__ == '__main__':
    app.run(
        debug=True
    )