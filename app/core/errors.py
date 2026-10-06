"""One error type for the whole application, plus the Flask handlers.

Any route can `raise ApiError(404, "book not found")` and Flask turns it
into a JSON response. This keeps every route free of jsonify/status-code
plumbing.
"""

from flask import jsonify
from pymongo.errors import DuplicateKeyError, OperationFailure

# MongoDB returns this code when a write fails a $jsonSchema validator.
# Catching it is how the API proves the DATABASE rejected the document,
# rather than the application.
DOCUMENT_VALIDATION_FAILURE = 121


class ApiError(Exception):
    """An error with an HTTP status code attached."""

    def __init__(self, status_code: int, message: str, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        body = {"error": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


def register_error_handlers(app) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return jsonify(exc.to_dict()), exc.status_code

    @app.errorhandler(DuplicateKeyError)
    def handle_duplicate_key(exc: DuplicateKeyError):
        # Raised by the unique index on users.email.
        return (
            jsonify({"error": "that value already exists", "detail": str(exc.details)}),
            409,
        )

    @app.errorhandler(OperationFailure)
    def handle_operation_failure(exc: OperationFailure):
        if exc.code == DOCUMENT_VALIDATION_FAILURE:
            return (
                jsonify(
                    {
                        "error": "rejected by the MongoDB $jsonSchema validator",
                        "detail": exc.details.get("errInfo") if exc.details else None,
                    }
                ),
                422,
            )
        return jsonify({"error": "database error", "detail": str(exc)}), 500

    @app.errorhandler(404)
    def handle_not_found(_exc):
        return jsonify({"error": "no such route"}), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(_exc):
        return jsonify({"error": "method not allowed on this route"}), 405
