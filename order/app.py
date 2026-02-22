import atexit
import logging

from flask import Flask

from routes.public import public_bp
from store import close_db_connection

app = Flask("order-service")
app.register_blueprint(public_bp)

atexit.register(close_db_connection)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

