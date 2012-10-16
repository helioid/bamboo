from bamboo.config import settings

BROKER_URL = 'mongodb://localhost:27017/%s' % settings.DATABASE_NAME
CELERY_RESULT_BACKEND = 'mongodb'
CELERY_MONGODB_BACKEND_SETTINGS = {
    'host': 'localhost',
    'port': 27017,
    'database': settings.DATABASE_NAME,
    'taskmeta_collection': 'celery_tasks',
}
