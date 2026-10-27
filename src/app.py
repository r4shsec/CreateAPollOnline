# Dependencies | pip install -r requirements.txt
from flask import Flask, render_template

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html'), 200



if __name__ == '__main__':
    app.run(
        debug=True
    )