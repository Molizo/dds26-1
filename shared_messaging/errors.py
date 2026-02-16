class MessageContractError(Exception):
    """Base error type for message contract handling."""


class MessageDecodeError(MessageContractError):
    """Raised when a message cannot be decoded from the wire format."""


class MessageValidationError(MessageContractError):
    """Raised when message metadata/payload does not match the contract."""


class UnsupportedMessageTypeError(MessageValidationError):
    """Raised when message_type is not part of the supported contract."""
