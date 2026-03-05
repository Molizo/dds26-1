# Command names sent to participant queues
CMD_HOLD = "hold"
CMD_RELEASE = "release"
CMD_COMMIT = "commit"

# Participant service names (used in ParticipantReply.service)
SVC_STOCK = "stock"
SVC_PAYMENT = "payment"

# Coordinator transaction status values
STATUS_INIT = "init"
STATUS_HOLDING = "holding"
STATUS_HELD = "held"
STATUS_COMMITTING = "committing"
STATUS_COMPENSATING = "compensating"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_FAILED_NEEDS_RECOVERY = "failed_needs_recovery"

# TERMINAL_STATUSES: the active-tx guard is cleared ONLY when reaching these.
# FAILED_NEEDS_RECOVERY is intentionally excluded — clearing the guard while
# recovery is pending was the root cause of the duplicate-checkout bug in attempt 2.
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_ABORTED})

# Protocol modes
PROTOCOL_SAGA = "saga"
PROTOCOL_2PC = "2pc"
VALID_PROTOCOLS = frozenset({PROTOCOL_SAGA, PROTOCOL_2PC})

# Timing constants
PARTICIPANT_REPLY_TIMEOUT = 10   # seconds to wait for a participant reply
ACTIVE_TX_GUARD_TTL = 60         # seconds before an abandoned guard expires
PARTICIPANT_TX_TTL = 3600        # seconds before participant tx idempotency records expire (1 hour)
RECOVERY_SCAN_INTERVAL = 30      # seconds between recovery worker scans
RECOVERY_STALE_AGE = 15          # seconds before a non-terminal tx is eligible for recovery

# RabbitMQ queue names
STOCK_COMMANDS_QUEUE = "stock.commands"
PAYMENT_COMMANDS_QUEUE = "payment.commands"
STOCK_COMMANDS_DLQ = "stock.commands.dlq"
PAYMENT_COMMANDS_DLQ = "payment.commands.dlq"
