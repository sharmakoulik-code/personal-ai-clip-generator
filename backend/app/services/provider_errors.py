"""Provider failures carry an allowlisted category, never raw remote details."""


class ProviderError(ValueError):
    def __init__(self, category: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.category = category
        # Only a valid numeric HTTP status is safe to show; never provider text.
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None


SAFE_DIAGNOSES = {
    'network': 'connection or timeout error',
    'auth': 'key rejected or model access denied',
    'request': 'request rejected (HTTP 400; could be a key, access, or request issue)',
    'google_invalid_key': 'API key is invalid (Google API_KEY_INVALID)',
    'google_restricted_key': 'API key restrictions or permissions denied the request',
    'google_service_disabled': 'Google API is disabled for this project',
    'unavailable': 'model or endpoint unavailable (HTTP 404)',
    'quota': 'rate or quota limit (HTTP 429)',
    'http': 'provider returned an error',
    'invalid_response': 'invalid, truncated or blocked response',
    'groq_json_validation': 'provider could not produce valid JSON (HTTP 400)',
}


_GOOGLE_REASONS = {
    'API_KEY_INVALID': 'google_invalid_key',
    'API_KEY_SERVICE_BLOCKED': 'google_restricted_key',
    'API_KEY_HTTP_REFERRER_BLOCKED': 'google_restricted_key',
    'API_KEY_IP_ADDRESS_BLOCKED': 'google_restricted_key',
    'API_KEY_ANDROID_APP_BLOCKED': 'google_restricted_key',
    'API_KEY_IOS_APP_BLOCKED': 'google_restricted_key',
    'SERVICE_DISABLED': 'google_service_disabled',
}


def google_error_category(response):
    """Interpret only known structured reason codes, never raw message/metadata."""
    fallback = 'auth' if response.status_code in (401, 403) else 'request'
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        details = error.get('details', []) if isinstance(error, dict) else []
        if not isinstance(details, list): return fallback
        for item in details[:20]:
            if not isinstance(item, dict): continue
            reason = item.get('reason')
            if isinstance(reason, str) and reason in _GOOGLE_REASONS:
                return _GOOGLE_REASONS[reason]
    except (ValueError, TypeError):
        pass
    return fallback
