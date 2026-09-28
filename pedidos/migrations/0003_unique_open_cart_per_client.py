from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pedidos', '0002_cart_cartitem'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='cart',
            constraint=models.UniqueConstraint(
                condition=models.Q(('cliente__isnull', False), ('estado', 'OPEN')),
                fields=('cliente',),
                name='unique_open_cart_per_client',
            ),
        ),
    ]
