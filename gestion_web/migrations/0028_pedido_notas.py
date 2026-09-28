from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gestion_web', '0027_configuracionsitio_mapa_iframe_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='pedido',
            name='notas',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Notas o instrucciones especiales indicadas por el cliente.',
            ),
        ),
    ]
