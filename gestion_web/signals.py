from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.db import transaction
from django.core.exceptions import ValidationError
from .models import DetallePedido, Insumo, Insumo_Producto, Producto


def _delete_file_after_commit(field_file):
    if not field_file or not field_file.name:
        return
    storage = field_file.storage
    name = field_file.name
    transaction.on_commit(lambda: storage.delete(name))


@receiver(pre_save, sender=Producto)
def eliminar_imagen_anterior_producto(sender, instance, **kwargs):
    if not instance.pk:
        return

    previous = sender.objects.filter(pk=instance.pk).only('imagen').first()
    if not previous or not previous.imagen or not instance.imagen:
        return
    if previous.imagen.name != instance.imagen.name:
        instance._previous_imagen = previous.imagen


@receiver(post_save, sender=Producto)
def eliminar_imagen_reemplazada_producto(sender, instance, **kwargs):
    previous_image = getattr(instance, '_previous_imagen', None)
    if previous_image:
        _delete_file_after_commit(previous_image)
        del instance._previous_imagen


@receiver(post_delete, sender=Producto)
def eliminar_imagen_producto(sender, instance, **kwargs):
    _delete_file_after_commit(instance.imagen)

@receiver(post_save, sender=DetallePedido)
def descontar_insumos_bodega(sender, instance, created, **kwargs):
    if created:
        with transaction.atomic():
            insumos_producto = Insumo_Producto.objects.filter(
                producto=instance.producto,
            ).select_related('insumo')

            for item in insumos_producto:
                insumo = Insumo.objects.select_for_update().get(pk=item.insumo_id)
                total_descuento = item.cantidad_utilizada * instance.cantidad

                if insumo.stock < total_descuento:
                    raise ValidationError(
                        f'Stock insuficiente para el insumo "{insumo.nombre}".',
                    )

                insumo.stock -= total_descuento
                insumo.save(update_fields=['stock'])