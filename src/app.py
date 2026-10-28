# Dependencies | pip install -r requirements.txt
import os
import time
import json
import uuid
import sqlite3
import secrets
from haikunator import Haikunator
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)
haikunator = Haikunator()

# Webserver

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html'), 200

# Application Programming Interface (API)

if __name__ == '__main__':
    app.run(
        debug=True
    )