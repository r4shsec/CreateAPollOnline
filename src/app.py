# Dependencies | pip install -r requirements.txt
import jwt
import uuid
import sqlite3
import secrets
from haikunator import Haikunator
from flask import Flask, render_template

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)



# Webserver

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html'), 200

# Application Programming Interface (API)

if __name__ == '__main__':
    app.run(
        debug=True
    )