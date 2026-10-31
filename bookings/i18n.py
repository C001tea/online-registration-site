"""Язык клиента: русский (основной) и румынский — сайт записи и сообщения бота клиенту.

Сайт присылает заголовок X-Lang: ro. Тексты в коде — по-русски; румынский перевод берётся
из словаря RO по русскому тексту (с {подстановками}). Встроенные сообщения Django и DRF
(«Обязательное поле» и т.п.) переводятся их собственными переводами (translation.override).
Панель салона заголовок не присылает и остаётся на русском.
"""
import json

from django.utils import translation

LANGS = ('ru', 'ro')

RO = {
    # --- Запись и отмена ---
    'Это время уже заняли. Выберите другое.': 'Această oră tocmai a fost ocupată. Alegeți alta.',
    'Это время недоступно. Выберите другое.': 'Această oră nu este disponibilă. Alegeți alta.',
    'На это время нет свободных мастеров.': 'La această oră nu sunt maeștri liberi.',
    'Услуга не найдена.': 'Serviciul nu a fost găsit.',
    'Мастер не найден.': 'Maestrul nu a fost găsit.',
    'Мастер не оказывает эту услугу.': 'Maestrul nu prestează acest serviciu.',
    'Нужно согласие на обработку персональных данных':
        'Este necesar acordul pentru prelucrarea datelor cu caracter personal',
    'Запись не найдена': 'Programarea nu a fost găsită',
    'Эту запись нельзя отменить': 'Această programare nu poate fi anulată',
    'Прошедшую запись нельзя отменить': 'O programare trecută nu poate fi anulată',
    'Запись отменена': 'Programarea a fost anulată',
    'Отменить запись можно не позже чем за {hours} ч до визита.':
        'Programarea poate fi anulată cel târziu cu {hours} ore înainte de vizită.',
    ' Позвоните в салон: {phone}': ' Sunați la salon: {phone}',
    # --- Отзывы ---
    'Необходима авторизация': 'Este necesară autentificarea',
    'Нельзя оставить отзыв самому себе': 'Nu vă puteți lăsa o recenzie singur',
    'Вы уже оставили отзыв на этого мастера': 'Ați lăsat deja o recenzie acestui maestru',
    'Отзыв можно оставить после визита к мастеру': 'Puteți lăsa o recenzie după vizita la maestru',
    'Оценка должна быть от 1 до 5': 'Nota trebuie să fie de la 1 la 5',
    # --- Аккаунт ---
    'Укажите телефон, имя и пароль': 'Indicați telefonul, numele și parola',
    'Укажите телефон или пароль': 'Indicați telefonul și parola',
    'Неверный формат номера телефона': 'Numărul de telefon are un format greșit',
    'Этот номер уже занят': 'Acest număr este deja înregistrat',
    'Неверный телефон или пароль': 'Telefon sau parolă greșită',
    'Профиль клиента не найден': 'Profilul clientului nu a fost găsit',
    'Слишком много попыток входа. Попробуйте через 15 минут':
        'Prea multe încercări de autentificare. Încercați peste 15 minute',
    # --- Вход через Telegram (сайт) ---
    'Вход через Telegram сейчас недоступен': 'Autentificarea prin Telegram nu este disponibilă acum',
    'Слишком много попыток. Попробуйте через час': 'Prea multe încercări. Încercați peste o oră',
    'Ссылка для входа устарела. Начните вход заново': 'Linkul de autentificare a expirat. Începeți din nou',
    'Сначала откройте бота и поделитесь номером': 'Mai întâi deschideți botul și trimiteți numărul',
    'Слишком много попыток. Начните вход заново': 'Prea multe încercări. Începeți autentificarea din nou',
    'Неверный код': 'Cod greșit',
    # --- Вход через Telegram (бот) ---
    '📱 Поделиться номером': '📱 Trimite numărul',
    'Вход на сайт записи.\n\nНажмите кнопку «📱 Поделиться номером» внизу — так мы убедимся, что номер ваш.':
        'Autentificare pe site-ul de programări.\n\nApăsați butonul «📱 Trimite numărul» de mai jos — '
        'așa ne asigurăm că numărul este al dvs.',
    'Ссылка для входа устарела. Нажмите «Войти через Telegram» на сайте ещё раз.':
        'Linkul de autentificare a expirat. Apăsați din nou «Intră prin Telegram» pe site.',
    'Отправьте, пожалуйста, свой номер кнопкой «📱 Поделиться номером».':
        'Vă rugăm să trimiteți numărul dvs. cu butonul «📱 Trimite numărul».',
    'Не получилось распознать номер. Попробуйте войти на сайте по номеру и паролю.':
        'Nu am putut recunoaște numărul. Încercați să intrați pe site cu numărul și parola.',
    'Этот номер занят служебным аккаунтом. Войдите на сайте по номеру и паролю.':
        'Acest număr aparține unui cont de serviciu. Intrați pe site cu numărul și parola.',
    'Код для входа: <b>{code}</b>\n\nВведите его на сайте. Никому не сообщайте этот код.':
        'Codul de autentificare: <b>{code}</b>\n\nIntroduceți-l pe site. Nu comunicați nimănui acest cod.',
    # --- Бот: запись клиента ---
    '⏳ Ждём вашего подтверждения': '⏳ Așteptăm confirmarea dvs.',
    '✅ Запись подтверждена': '✅ Programarea este confirmată',
    '❌ Запись отменена': '❌ Programarea a fost anulată',
    'Визит состоялся': 'Vizita a avut loc',
    'Визит пропущен': 'Vizita a fost ratată',
    'Услуга: {name}': 'Serviciu: {name}',
    'Мастер: {name}': 'Maestru: {name}',
    '🔔 <b>Напоминаем о записи завтра</b>': '🔔 <b>Vă reamintim de programarea de mâine</b>',
    '⏰ <b>Через час ждём вас</b>': '⏰ <b>Vă așteptăm peste o oră</b>',
    '❌ <b>Салон отменил вашу запись</b>': '❌ <b>Salonul a anulat programarea dvs.</b>',
    '🔄 <b>Запись перенесена</b>': '🔄 <b>Programarea a fost mutată</b>',
    'Эта запись уже подключена к другому Telegram.': 'Această programare este deja conectată la alt cont Telegram.',
    'Ссылка устарела или не найдена. Откройте её заново с сайта.':
        'Linkul a expirat sau nu a fost găsit. Deschideți-l din nou de pe site.',
    '\n\n<b>Точно отменить запись?</b>': '\n\n<b>Sigur anulați programarea?</b>',
    'Эту запись уже нельзя изменить': 'Această programare nu mai poate fi modificată',
    'Подтверждаю ✅': 'Confirm ✅',
    'Отменить': 'Anulează',
    'Отменить запись': 'Anulează programarea',
    'Да, отменить': 'Da, anulează',
    'Нет': 'Nu',
    'Спасибо, ждём вас!': 'Mulțumim, vă așteptăm!',
    # --- Чёрный список ---
    '⚠️ <b>Неоплаченный штраф: {amount}</b>': '⚠️ <b>Amendă neachitată: {amount}</b>',
    'вы не пришли на {service}': 'nu v-ați prezentat la {service}',
    'вы отменили {service} меньше чем за {hours} ч до визита':
        'ați anulat {service} cu mai puțin de {hours} ore înainte de vizită',
    'запись': 'programare',
    'за неявку или отмену меньше чем за {hours} ч до визита':
        'pentru neprezentare sau anulare cu mai puțin de {hours} ore înainte de vizită',
    'за неявку': 'pentru neprezentare',
    'Согласно правилам салона, с которыми вы ознакомились при записи, {rule} взимается штраф '
    '{percent}% стоимости услуги.':
        'Conform regulilor salonului, cu care ați luat cunoștință la programare, {rule} se aplică o amendă '
        'de {percent}% din prețul serviciului.',
    '💳 Пожалуйста, оплатите его в салоне при этом визите.': '💳 Vă rugăm să o achitați la salon la această vizită.',
    '⚠️ До визита меньше {hours} ч: при отмене будет начислен штраф {amount} '
    '(оплата в салоне при следующем визите).':
        '⚠️ Până la vizită au rămas mai puțin de {hours} ore: la anulare se va aplica o amendă de {amount} '
        '(se achită la salon la următoarea vizită).',
    '❗ <b>Вы не пришли на запись</b>': '❗ <b>Nu v-ați prezentat la programare</b>',
    '❗ <b>Запись отменена позже чем за {hours} ч до визита</b>':
        '❗ <b>Programarea a fost anulată cu mai puțin de {hours} ore înainte de vizită</b>',
    'Начислен штраф {amount} ({percent}% стоимости услуги). Его нужно будет оплатить '
    'в салоне при следующем визите.':
        'A fost aplicată o amendă de {amount} ({percent}% din prețul serviciului). Ea se achită '
        'la salon la următoarea vizită.',
}

WEEKDAYS = {
    'ru': ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'],
    'ro': ['lun', 'mar', 'mie', 'joi', 'vin', 'sâm', 'dum'],
}
MONTHS = {
    'ru': ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
           'сентября', 'октября', 'ноября', 'декабря'],
    'ro': ['ianuarie', 'februarie', 'martie', 'aprilie', 'mai', 'iunie', 'iulie', 'august',
           'septembrie', 'octombrie', 'noiembrie', 'decembrie'],
}


def norm_lang(value):
    return 'ro' if str(value or '').strip().lower().startswith('ro') else 'ru'


def tr(text, lang, **params):
    """Русский текст (шаблон с {подстановками}) → на языке клиента."""
    if lang == 'ro':
        text = RO.get(text, text)
    return text.format(**params) if params else text


def client_lang(request):
    return getattr(request, 'client_lang', 'ru')


def translate_data(data, lang):
    """Перевод всех строк ответа API, которые есть в словаре (ошибки, «Запись отменена»)."""
    if isinstance(data, str):
        return RO.get(data, data) if lang == 'ro' else data
    if isinstance(data, list):
        return [translate_data(item, lang) for item in data]
    if isinstance(data, dict):
        return {key: translate_data(value, lang) for key, value in data.items()}
    return data


class ClientLanguageMiddleware:
    """X-Lang: ro → request.client_lang, переводы Django/DRF и перевод текстов ответа."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = norm_lang(request.headers.get('X-Lang'))
        request.client_lang = lang
        if lang == 'ru':
            return self.get_response(request)
        with translation.override(lang):
            response = self.get_response(request)
        if 'application/json' in response.get('Content-Type', '') and not getattr(response, 'streaming', False):
            try:
                data = json.loads(response.content)
            except ValueError:
                return response
            if response.status_code >= 400:
                translated = translate_data(data, lang)
            elif isinstance(data, dict):
                # В успешных ответах — только служебное сообщение, не данные (имена, услуги)
                translated = {**data, **{k: translate_data(data[k], lang) for k in ('message', 'error') if k in data}}
            else:
                translated = data
            if translated != data:
                response.content = json.dumps(translated, ensure_ascii=False).encode()
                response['Content-Length'] = str(len(response.content))
        response['Content-Language'] = lang
        return response
