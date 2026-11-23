class ServiceError(Exception):
    """Базовая ошибка слоя services. Текст можно показывать пользователю."""


class PermissionDenied(ServiceError):
    pass


class NotFound(ServiceError):
    pass


class ValidationError(ServiceError):
    pass
