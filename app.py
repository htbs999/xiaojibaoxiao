import os
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify(ok=True, msg="flask on 微信云托管 works")

@app.route("/api/ping")
def ping():
    return jsonify(pong=True)

if __name__ == "__main__":
    # 关键：host=0.0.0.0，端口用 PORT（云托管会给）
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)