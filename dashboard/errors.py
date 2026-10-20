from rest_framework import status
from rest_framework.exceptions import APIException


class StaffError(APIException):
    """Ошибка панели салона в формате {"error": "текст", ...доп. поля}.

    В отличие от ValidationError текст не заворачивается в список, а доп. поля
    (например, "count") остаются числами.
    """
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message, status_code=status.HTTP_400_BAD_REQUEST, **extra):
        self.status_code = status_code
        self.detail = {'error': message, **extra}
