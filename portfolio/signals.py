from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from dashboard.photos import delete_file
from .models import WorkPhoto


@receiver(post_delete, sender=WorkPhoto)
def remove_file(sender, instance, **kwargs):
    if instance.image and instance.image.name:
        storage, name = instance.image.storage, instance.image.name
        transaction.on_commit(lambda: delete_file(storage, name))
