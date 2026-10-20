import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException


def normalize_phone(phone, region=None):
    """Номер в формате E.164 (+37369123456) или None, если номер неверный.

    region — код страны для номеров без «+» (панель салона передаёт 'MD',
    чтобы можно было ввести 069123456 или 69123456).
    """
    if not isinstance(phone, str):
        return None
    try:
        parsed = phonenumbers.parse(phone, region)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException:
        return None
