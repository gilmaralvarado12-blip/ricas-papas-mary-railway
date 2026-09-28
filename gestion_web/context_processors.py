from django.templatetags.static import static
from django.conf import settings
from django.db import DatabaseError
from django.middleware.csrf import get_token
from django.utils import timezone
from decimal import Decimal
import logging

from .middleware import get_client_session_expiry, is_public_client
from .models import Pedido, Producto, Reserva
from .models import ConfiguracionSitio, DetallePedido
from django.db.models import Sum
from pedidos.models import Cart
from .horarios import estado_restaurante

logger = logging.getLogger(__name__)


def get_site_configuration():
    config = ConfiguracionSitio.objects.filter(activo=True).order_by('-id').first()
    if config is None:
        return ConfiguracionSitio(
            nombre='Configuración principal',
            nombre_restaurante='Ricas Papas Mary',
            subtitulo_logo='Rukullacta',
            titulo_inicio='Ricas Papas Mary',
            subtitulo_inicio='Papas, combos y sabores para cada momento.',
            texto_bienvenida='Disfruta lo mejor de la cocina en Ricas Papas Mary.',
            color_principal='#F57C00',
            color_secundario='#B71C1C',
            color_botones='#FF9800',
            color_menu='#F57C00',
            color_fondo='#FFFFFF',
            color_encabezados='#212529',
            color_texto='#212529',
            color_enlaces='#D96600',
        )
    return config


def cart_summary(request):
    # prefer persistent cart for authenticated users
    count = 0
    total = Decimal('0.00')
    if request.user.is_authenticated:
        try:
            cart = Cart.objects.filter(cliente=request.user, estado=Cart.Estado.OPEN).first()
            if cart:
                for ci in cart.items.select_related('producto').all():
                    count += int(ci.cantidad)
                    total += ci.subtotal()
                return {'cart_count': count, 'cart_total': total}
        except DatabaseError:
            logger.exception(
                'No se pudo cargar el carrito persistente del usuario %s.',
                request.user.pk,
            )
    # fallback to session cart
    cart_s = request.session.get('cart', {})
    product_ids = {}
    for pid in cart_s:
        try:
            product_ids[pid] = int(pid)
        except (TypeError, ValueError):
            continue
    products = Producto.objects.in_bulk(product_ids.values())
    for pid, qty in cart_s.items():
        try:
            prod = products.get(product_ids[pid])
            if isinstance(qty, dict):
                qty = qty.get('cantidad', 0)
            if prod is None or not prod.disponible:
                continue
            count += int(qty)
            precio = Decimal(str(prod.precio).replace(',', '.'))
            total += precio * int(qty)
        except (TypeError, ValueError, KeyError):
            continue
    return {'cart_count': count, 'cart_total': total}


def site_config(request):
    configuracion = get_site_configuration()
    return {
        'site_config': configuracion,
        'restaurant_status': estado_restaurante(configuracion),
    }


def site_background(request):
    fondo_sitio_url = static('images/papas_mary.jpg')

    configuracion = get_site_configuration()
    if configuracion and configuracion.fondo and configuracion.fondo.name:
        try:
            if configuracion.fondo.storage.exists(configuracion.fondo.name):
                fondo_sitio_url = configuracion.fondo.url
        except (OSError, ValueError):
            logger.warning(
                'No se pudo resolver la imagen de fondo configurada.',
                exc_info=True,
            )

    return {'fondo_sitio_url': fondo_sitio_url}


def site_branding(request):
    site_logo_url = static('images/logo.png')
    site_logo_subtitle = 'Rukullacta'

    configuracion = get_site_configuration()
    if configuracion:
        if configuracion.logo_principal and configuracion.logo_principal.name:
            try:
                if configuracion.logo_principal.storage.exists(configuracion.logo_principal.name):
                    site_logo_url = configuracion.logo_principal.url
            except (OSError, ValueError):
                logger.warning(
                    'No se pudo resolver el logotipo configurado.',
                    exc_info=True,
                )
        if configuracion.subtitulo_logo:
            site_logo_subtitle = configuracion.subtitulo_logo

    return {
        'site_logo_url': site_logo_url,
        'site_logo_subtitle': site_logo_subtitle,
        'site_whatsapp_url': (
            f"https://wa.me/{''.join(character for character in configuracion.whatsapp if character.isdigit())}"
            if configuracion and configuracion.whatsapp
            else ''
        ),
    }

def session_notice(request):
    if not is_public_client(request.user):
        return {}

    expires_at = get_client_session_expiry(request.session)
    if expires_at is None:
        return {}

    return {
        'client_session_expires_at_ms': int(expires_at * 1000),
        'client_session_csrf_token': get_token(request),
    }


def google_maps_config(request):
    return {
        'google_maps_api_key': getattr(settings, 'GOOGLE_MAPS_API_KEY', ''),
    }


def admin_dashboard_summary(request):
    if not request.path.startswith('/admin/'):
        return {}

    today = timezone.now().date()
    productos_mas_vendidos = list(
        DetallePedido.objects.values(
            'producto__nombre',
            'producto_id',
        ).annotate(
            cantidad=Sum('cantidad'),
        ).order_by('-cantidad', 'producto__nombre')[:5]
    )
    productos = {
        producto.id: producto
        for producto in Producto.objects.filter(
            id__in=[row['producto_id'] for row in productos_mas_vendidos]
        )
    }
    for row in productos_mas_vendidos:
        producto = productos.get(row['producto_id'])
        row['imagen_url'] = producto.imagen.url if producto and producto.imagen else ''
    return {
        'admin_pedidos_hoy': Pedido.objects.filter(
            fecha_creacion__date=today,
        ).count(),
        'admin_reservas_activas_hoy': Reserva.objects.filter(
            fecha=today,
            estado=Reserva.EstadoReserva.CONFIRMADA,
        ).count(),
        'admin_productos_mas_vendidos': productos_mas_vendidos,
    }
