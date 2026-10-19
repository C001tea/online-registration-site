import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException


def normalize_phone(phone):
    """Номер в формате E.164 (+37369123456) или None, если номер неверный."""
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException:
        return None
