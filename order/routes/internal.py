from flask import Blueprint, jsonify

from recovery import recover_tx_by_id, run_recovery_pass

internal_bp = Blueprint("order_internal", __name__)


@internal_bp.post("/internal/recovery/scan")
def trigger_recovery_scan():
    recovered = run_recovery_pass()
    return jsonify({"recovered": recovered})


@internal_bp.post("/internal/tx/recover/<tx_id>")
def recover_single_tx(tx_id: str):
    status = recover_tx_by_id(tx_id)
    return jsonify({"tx_id": tx_id, "status": status})
