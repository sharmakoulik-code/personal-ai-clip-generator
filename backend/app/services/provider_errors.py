"""Provider failures carry an allowlisted category, never raw remote details."""


class ProviderError(ValueError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


SAFE_DIAGNOSES = {
    'network': 'connection or timeout error',
    'auth': 'key rejected or model access denied',
    'request': 'request rejected (HTTP 400)',
    'unavailable': 'model or endpoint unavailable (HTTP 404)',
    'quota': 'rate or quota limit (HTTP 429)',
    'http': 'provider returned an error',
    'invalid_response': 'invalid, truncated or blocked response',
}
