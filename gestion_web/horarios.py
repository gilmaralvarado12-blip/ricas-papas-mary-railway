from datetime import time
import re

from django.utils import timezone


HORA_APERTURA = time(8, 0)
HORA_CIERRE = time(21, 0)


def horario_configurado(configuracion=None):
    """Obtiene la franja horaria configurada, con respaldo seguro."""
    horario = getattr(configuracion, 'horario', '') or ''
    horas = re.findall(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', horario)
    if len(horas) >= 2:
        return time(int(horas[0][0]), int(horas[0][1])), time(
            int(horas[1][0]), int(horas[1][1])
        )
    return HORA_APERTURA, HORA_CIERRE


def dentro_del_horario(hora, configuracion=None):
    """Indica si una hora está dentro del horario de atención configurado."""
    hora_apertura, hora_cierre = horario_configurado(configuracion)
    if hora_apertura <= hora_cierre:
        return hora_apertura <= hora < hora_cierre
    return hora >= hora_apertura or hora < hora_cierre


def restaurante_atendiendo(configuracion=None):
    """Indica si actualmente se pueden confirmar pedidos."""
    return dentro_del_horario(
        timezone.localtime().time().replace(second=0, microsecond=0),
        configuracion,
    )


def estado_restaurante(configuracion=None):
    """Devuelve los datos listos para mostrar el estado del restaurante."""
    abierto = restaurante_atendiendo(configuracion)
    hora_apertura, hora_cierre = horario_configurado(configuracion)
    horario = f'{hora_apertura:%H:%M} - {hora_cierre:%H:%M}'
    return {
        'abierto': abierto,
        'texto': 'Abierto - Haz tu pedido' if abierto else 'Cerrado por ahora',
        'clase': 'bg-success' if abierto else 'bg-danger',
        'horario': horario,
    }


def horario_atencion_texto(configuracion=None):
    hora_apertura, hora_cierre = horario_configurado(configuracion)
    return f'El restaurante atiende de {hora_apertura:%H:%M} a {hora_cierre:%H:%M}.'
