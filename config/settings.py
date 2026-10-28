from pathlib import Path
from dotenv import load_dotenv
import os
from datetime import timedelta
import cloudinary
import dj_database_url

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent


def env_list(name, default=''):
    return [item.strip() for item in os.getenv(name, default).split(',') if item.strip()]


SECRET_KEY = os.getenv('SECRET_KEY')

# os.getenv возвращает строку, а строка 'False' в Python — истина,
# поэтому DEBUG раньше был включён всегда
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 'yes')

# Через запятую: ALLOWED_HOSTS=api.example.md,172.20.10.2
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')
if DEBUG:
    ALLOWED_HOSTS.append('*')

# HTTPS-домен API (Caddy, docs/HTTPS.md) — разрешаем его автоматически
API_DOMAIN = os.getenv('API_DOMAIN', '').strip()
if API_DOMAIN:
    ALLOWED_HOSTS.append(API_DOMAIN)

# Caddy передаёт исходную схему в X-Forwarded-Proto; без этого Django
# считает HTTPS-запросы обычными HTTP. Подделать заголовок снаружи нельзя:
# web открыт только для caddy, а caddy перезаписывает этот заголовок
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Для входа в Django admin по HTTPS (проверка Origin в CSRF)
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', '')
if API_DOMAIN:
    CSRF_TRUSTED_ORIGINS.append(f'https://{API_DOMAIN}')

# Куки Django admin — только по HTTPS (включать после перехода на домен)
HTTPS_ONLY = os.getenv('HTTPS_ONLY', 'False').lower() in ('true', '1', 'yes')
SESSION_COOKIE_SECURE = HTTPS_ONLY
CSRF_COOKIE_SECURE = HTTPS_ONLY


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'cloudinary_storage',
    'cloudinary',
    'rest_framework',
    'corsheaders',
    'bookings',
    'dashboard',
    'notifications',
    'finance',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # CorsMiddleware должен стоять до CommonMiddleware
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'business_db',
        'USER': 'artacsercs',
        'PASSWORD': os.getenv('DATA_BASE_PASSWORD'),
        'HOST': '127.0.0.1',
        'PORT': '5432',
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

# Стандартные сообщения Django/DRF об ошибках — на русском
LANGUAGE_CODE = 'ru'

# Время записей и графиков мастеров — местное
TIME_ZONE = 'Europe/Chisinau'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'

# Адреса фронтенда через запятую: CORS_ALLOWED_ORIGINS=https://jcnails.md,http://localhost:5173
# '*' — разрешить всех (django-cors-headers не принимает '*' в списке и падает при старте)
CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173')
CORS_ALLOW_ALL_ORIGINS = DEBUG or '*' in CORS_ALLOWED_ORIGINS
if '*' in CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = []

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
}

CLOUDINARY_STORAGE = {
    "CLOUD_NAME": "dznjgkiel",
    "API_KEY": os.getenv('API_KEY_CLOUDINARY'),
    "API_SECRET": os.getenv('API_SECRET_CLOUDINARY')
}

cloudinary.config(
    cloud_name = CLOUDINARY_STORAGE['CLOUD_NAME'],
    api_key = CLOUDINARY_STORAGE['API_KEY'],
    api_secret = CLOUDINARY_STORAGE['API_SECRET'],
    secure = True
)

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    DATABASES['default'] = dj_database_url.parse(DATABASE_URL)

STATIC_ROOT = BASE_DIR / 'staticfiles'
# Telegram-бот: подтверждения и напоминания (notifications/). Без токена бот выключен
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_BOT_USERNAME = os.getenv('TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
TELEGRAM_MAX_ATTEMPTS = 8
