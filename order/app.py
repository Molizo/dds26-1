import atexit
import logging

from flask import Flask

from recovery import start_recovery_worker, stop_recovery_worker
from routes.internal import internal_bp
from routes.public import public_bp
from store import close_db_connection

app = Flask("order-service")
app.register_blueprint(public_bp)
app.register_blueprint(internal_bp)
start_recovery_worker()

atexit.register(close_db_connection)
atexit.register(stop_recovery_worker)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
