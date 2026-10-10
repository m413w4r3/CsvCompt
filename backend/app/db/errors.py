"""Exceptions métier de persistance à traduire dans l'enveloppe HTTP API."""


class DatabaseDomainError(RuntimeError):
    """Base des erreurs de persistance exposables par le handler d'API."""

    code = "DATABASE_ERROR"
    status_code = 500
    retryable = False


class DatabaseBusyError(DatabaseDomainError):
    """La base est momentanément verrouillée; le client peut réessayer."""

    code = "DATABASE_BUSY"
    status_code = 503
    retryable = True

    def __init__(self) -> None:
        super().__init__("La base est momentanément occupée; réessayez.")


class VersionConflictError(DatabaseDomainError):
    """La version attendue d'une ligne ne correspond plus à la version stockée."""

    code = "VERSION_CONFLICT"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("La ressource a été modifiée par une autre écriture.")
