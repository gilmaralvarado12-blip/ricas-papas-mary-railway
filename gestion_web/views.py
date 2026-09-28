import json
import csv
import unicodedata
import re

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil, isfinite
from django.db import transaction
from django.db.models import Count, Sum, Q, Prefetch
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_http_methods
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError
from django.templatetags.static import static
from django.urls import reverse
from django.http import HttpResponse
from urllib.parse import quote_plus
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .middleware import (
    CLIENT_ACTIVITY_SYNC_INTERVAL_SECONDS,
    CLIENT_ACTIVITY_SYNC_KEY,
    get_client_session_expiry,
    is_public_client,
    renew_client_session,
)

# Importamos los modelos que alimentan la interfaz principal del cliente.
# - Pedido/Producto: pestaña de pedidos.
# - Reserva/Mesa: pestaña de reservas y mapa de selección de mesa.
from .models import ConfiguracionSitio, Pedido, Pago, Reserva, Producto, Mesa, DetallePedido, Entrega, Insumo
from .forms import RegistroForm
from reservas.utils import send_reserva_confirmada_email
from .horarios import dentro_del_horario, horario_atencion_texto


@never_cache
@require_POST
@csrf_protect
def renovar_sesion_cliente(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'authentication_required'}, status=401)
    if not is_public_client(request.user):
        return JsonResponse({'error': 'client_only'}, status=403)

    now = timezone.now()
    now_timestamp = now.timestamp()
    last_sync = request.session.get(CLIENT_ACTIVITY_SYNC_KEY)

    if (
        isinstance(last_sync, (int, float))
        and not isinstance(last_sync, bool)
        and isfinite(last_sync)
        and now_timestamp >= last_sync
        and now_timestamp - last_sync < CLIENT_ACTIVITY_SYNC_INTERVAL_SECONDS
    ):
        expires_at = get_client_session_expiry(request.session)
        if expires_at is not None:
            return JsonResponse({
                'renewed': False,
                'expires_at': int(expires_at * 1000),
            })

    expires_at = renew_client_session(request.session, now)
    request.session[CLIENT_ACTIVITY_SYNC_KEY] = now_timestamp
    return JsonResponse({
        'renewed': True,
        'expires_at': int(expires_at.timestamp() * 1000),
    })


def _normalize_chat_message(message):
    if not message:
        return ''
    normalized = unicodedata.normalize('NFKD', str(message).lower())
    normalized = ''.join(character for character in normalized if not unicodedata.combining(character))
    normalized = normalized.replace('¿', ' ').replace('?', ' ').replace('¡', ' ').replace('!', ' ')
    normalized = re.sub(r'[^a-z0-9\sáéíóúñü]', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def _quick_buttons_html(buttons):
    if not buttons:
        return ''
    html = ['<div class="rpm-chat-quick-actions">']
    for label, value in buttons:
        html.append(f'<button type="button" class="rpm-chat-quick-action" data-message="{escape(value)}">{label}</button>')
    html.append('</div>')
    return ''.join(html)


def _product_cards_html(request, queryset, empty_text='No hay productos disponibles en este momento.'):
    if not queryset:
        return '<div class="rpm-chat-empty-state">{}</div>'.format(escape(empty_text))

    cards = []
    for producto in queryset[:6]:
        product_url = request.build_absolute_uri(f"{reverse('ver_menu')}?q={quote_plus(producto.nombre)}")
        image_url = request.build_absolute_uri(producto.imagen.url) if getattr(producto, 'imagen', None) and producto.imagen else request.build_absolute_uri(static('images/logo.png'))
        cards.append(
            '<div class="rpm-bot-card">'
            f'<div class="rpm-bot-card-img"><img src="{escape(image_url)}" alt="{escape(producto.nombre)}"></div>'
            '<div class="rpm-bot-card-body">'
            f'<div class="rpm-bot-card-title">{escape(producto.nombre)}</div>'
            f'<div class="rpm-bot-card-desc">{escape((producto.descripcion or "Sin descripción disponible.")[:120])}</div>'
            f'<div class="rpm-bot-card-price">${format(producto.precio, ".2f")} · {"Disponible" if producto.disponible else "No disponible"}</div>'
            '</div>'
            f'<div class="rpm-bot-card-actions"><a href="{escape(product_url)}" target="_blank" rel="noopener noreferrer">Ver menú</a></div>'
            '</div>'
        )
    return '<div class="rpm-bot-cards">' + ''.join(cards) + '</div>'


def _product_payload(request, queryset):
    products = []
    for producto in queryset[:6]:
        menu_url = request.build_absolute_uri(
            f"{reverse('ver_menu')}?q={quote_plus(producto.nombre)}"
        )
        image_url = (
            request.build_absolute_uri(producto.imagen.url)
            if getattr(producto, 'imagen', None) and producto.imagen
            else request.build_absolute_uri(static('images/logo.png'))
        )
        products.append({
            'id': producto.id,
            'nombre': producto.nombre,
            'descripcion': (producto.descripcion or '')[:120],
            'precio': str(producto.precio),
            'disponible': producto.disponible,
            'imagen_url': image_url,
            'enlace': menu_url,
        })
    return products


def _shopping_payment_info():
    plain = (
        'Actualmente aceptamos los siguientes métodos de pago: Efectivo y Transferencia bancaria. '
        'Si pagas por transferencia, realiza la transferencia y luego sube el comprobante para que el personal lo valide.'
    )
    html = (
        '<div class="rpm-chat-info-block">'
        '<strong>Formas de pago:</strong><ul>'
        '<li>Efectivo</li>'
        '<li>Transferencia bancaria</li>'
        '</ul>'
        '<p>Si pagas por transferencia, realiza la transferencia y luego sube el comprobante para que sea validado por el personal autorizado.</p>'
        '</div>'
    )
    return plain, html


@require_http_methods(['POST'])
def chatbot_response(request):
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'error': 'El mensaje debe enviarse como JSON válido.'}, status=400)

    if not isinstance(payload, dict) or not isinstance(payload.get('message'), str):
        return JsonResponse({'error': 'El campo message es obligatorio.'}, status=400)

    message = payload['message'].strip()
    if not message:
        return JsonResponse({'error': 'Escribe un mensaje para continuar.'}, status=400)
    if len(message) > 150:
        return JsonResponse(
            {'error': 'Tu mensaje es demasiado largo. Escríbelo en 150 caracteres o menos.'},
            status=400,
        )

    normalized_message = _normalize_chat_message(message)
    if not normalized_message:
        return JsonResponse({'error': 'Escribe un mensaje válido para consultar.'}, status=400)

    site_config = ConfiguracionSitio.objects.filter(activo=True).order_by('-id').first()

    def _text_response(text, extra_html=None):
        html = extra_html if extra_html is not None else f'<p>{escape(text)}</p>'
        return JsonResponse({'response': text, 'response_html': html})

    def _navigation_response(text, label, url):
        html = (
            f'<p>{escape(text)}</p>'
            f'<p><a class="rpm-chat-navigation-link" href="{escape(url)}">{
                escape(label)
            }</a></p>'
        )
        return JsonResponse({
            'response': text,
            'response_html': html,
            'navigation': {'label': label, 'url': url},
        })

    def _lookup_products(query, include_unavailable=False):
        clean_query = (query or '').strip()
        qs = Producto.objects.filter(disponible=True) if not include_unavailable else Producto.objects.all()
        if not clean_query:
           return qs.order_by('nombre')[:6]

        terms = [term for term in clean_query.split() if len(term) > 2]
        if not terms:
           terms = clean_query.split()
        filters = Q()
        for term in terms:
           variants = {term}
           if term.endswith('es') and len(term) > 4:
               variants.add(term[:-2])
           elif term.endswith('s') and len(term) > 3:
               variants.add(term[:-1])
           for variant in variants:
               filters |= Q(nombre__icontains=variant) | Q(descripcion__icontains=variant)
        return qs.filter(filters).order_by('nombre')[:6]

    def _extract_product_query(context_name=''):
        query = normalized_message
        for token in [
           'precio', 'cuanto cuesta', 'cuanto vale', 'valor', 'costo', 'cuesta', 'precio de',
           'cuanto', 'vale', 'donde esta', 'donde está', 'disponible', 'ver menu', 'menú',
           'menu', 'platos', 'productos', 'que venden', 'que venden', 'que platos tienen',
           'comida', 'cantidad', 'mi', 'mis', 'quiero', 'necesito', 'for', 'del', 'de la',
           'de', 'y', 'esta', 'estan', 'está', 'consultar', 'consulta', 'precios'
        ]:
           query = query.replace(token, ' ')
        query = re.sub(r'\s+', ' ', query).strip()
        if not query:
           query = context_name
        return query.strip()

    def _respuesta_no_entiendo():
        text = 'No tengo información suficiente para responder esa consulta. Puedes elegir una de las opciones disponibles o comunicarte directamente con Ricas Papas Mary.'
        quick = _quick_buttons_html([
           ('🍔 Menú', 'Ver menú'),
           ('📦 Pedidos', 'Mi pedido'),
           ('🪑 Reservas', 'Reservar mesa'),
           ('📍 Ubicación', 'Ubicación'),
           ('🕐 Horarios', 'Horarios'),
           ('📞 Atención al cliente', 'Atención al cliente'),
        ])
        return JsonResponse({
           'response': text,
           'response_html': f'<p>{escape(text)}</p>{quick}',
           'quick_actions': [
               {'label': '🍔 Menú', 'message': 'Ver menú'},
               {'label': '📦 Pedidos', 'message': 'Mi pedido'},
               {'label': '🪑 Reservas', 'message': 'Reservar mesa'},
               {'label': '📍 Ubicación', 'message': 'Ubicación'},
               {'label': '🕐 Horarios', 'message': 'Horarios'},
               {'label': '📞 Atención al cliente', 'message': 'Atención al cliente'},
           ],
        })

    greeting_keywords = {'hola', 'buenas', 'buenos dias', 'buenas tardes', 'buenas noches', 'saludos'}
    if any(keyword in normalized_message for keyword in greeting_keywords):
        intro = '¡Hola! 👋 Soy el asistente virtual de Ricas Papas Mary. Puedo ayudarte con el menú, precios, pedidos, reservas, pagos, entregas, horarios y ubicación. ¿Qué deseas consultar?'
        quick = _quick_buttons_html([
           ('🍔 Ver menú', 'Ver menú'),
           ('💰 Consultar precios', 'Consultar precios'),
           ('📦 Mi pedido', 'Mi pedido'),
           ('🪑 Reservar mesa', 'Reservar mesa'),
           ('🛵 Entregas', 'Entregas'),
           ('💳 Formas de pago', 'Formas de pago'),
           ('📍 Ubicación', 'Ubicación'),
           ('🕐 Horarios', 'Horarios'),
           ('❓ Ayuda', 'Ayuda'),
        ])
        return JsonResponse({'response': intro, 'response_html': f'<p>{escape(intro)}</p>{quick}'})

    menu_triggers = {'menu', 'ver menu', 'menú', 'platos', 'productos', 'que venden', 'que platos tienen', 'comida disponible', 'catalogo', 'platos disponibles', 'menu disponible'}
    if any(trigger in normalized_message for trigger in menu_triggers):
        products = Producto.objects.filter(disponible=True).order_by('nombre')[:6]
        if products.exists():
           response = 'Estos son algunos productos disponibles:'
           html = _product_cards_html(request, products)
           request.session['chat_last_product'] = products[0].nombre
           return JsonResponse({
               'response': response,
               'response_html': html,
               'products': _product_payload(request, products),
           })
        response = 'Actualmente no hay productos disponibles en este momento.'
        return _text_response(response)

    if any(trigger in normalized_message for trigger in ['precio', 'cuanto cuesta', 'cuanto vale', 'valor', 'costo', 'cuesta', 'precio de', 'cuanto']):
        search = _extract_product_query(request.session.get('chat_last_product', ''))
        products = _lookup_products(search)
        if not search:
           products = Producto.objects.filter(disponible=True).order_by('nombre')[:6]
        if not products.exists():
           if 'mixto' in normalized_message:
               products = _lookup_products('mixto')
        if products.exists():
           product = products[0]
           request.session['chat_last_product'] = product.nombre
           if products.count() == 1:
               response = f'El {product.nombre} cuesta ${format(product.precio, ".2f")}. '
               html = _product_cards_html(request, products[:1])
               return JsonResponse({
                   'response': response,
                   'response_html': html,
                   'products': _product_payload(request, products[:1]),
               })
           response = 'Encontré varias opciones relacionadas con tu consulta:'
           html = _product_cards_html(request, products)
           return JsonResponse({
               'response': response,
               'response_html': html,
               'products': _product_payload(request, products),
           })
        response = 'No encontré un producto con ese nombre en el menú. Puedes revisar la sección Menú o consultar otro producto.'
        return _text_response(response)

    if any(trigger in normalized_message for trigger in ['disponible', 'esta disponible', 'hay disponibilidad', 'hay stock']):
        product_name = request.session.get('chat_last_product', '').strip()
        if product_name:
           product = Producto.objects.filter(disponible=True, nombre__icontains=product_name).first() or Producto.objects.filter(nombre__icontains=product_name).first()
           if product:
               status = 'sí, está disponible' if product.disponible else 'no está disponible'
               response = f'El {product.nombre} {status} en este momento.'
               return _text_response(response)

    pedido_status_triggers = {'mi pedido', 'estado de mi pedido', 'como va mi pedido', 'donde esta mi pedido', 'mi orden', 'seguimiento', 'mis pedidos', 'historial de pedidos', 'pedidos anteriores'}
    if any(trigger in normalized_message for trigger in pedido_status_triggers):
        if not request.user.is_authenticated:
           login_url = reverse('login')
           text = 'Para consultar el estado de tus pedidos debes iniciar sesión.'
           html = f'<p>{escape(text)}</p><p><a href="{escape(login_url)}" target="_blank" rel="noopener noreferrer">Iniciar sesión</a></p>'
           return JsonResponse({'response': text, 'response_html': html})

        if 'mis pedidos' in normalized_message or 'historial de pedidos' in normalized_message or 'pedidos anteriores' in normalized_message:
           pedidos = Pedido.objects.filter(cliente=request.user).order_by('-fecha_creacion')[:5]
           if not pedidos.exists():
               return _text_response('Todavía no tienes pedidos registrados.')
           lines = []
           for pedido in pedidos:
               lines.append(f'Pedido #{pedido.id} · {pedido.get_estado_display()} · ${pedido.total:.2f} · {pedido.fecha_creacion.strftime("%d/%m/%Y")}')
           response = 'Estos son tus pedidos recientes:\n- ' + '\n- '.join(lines)
           return JsonResponse({'response': response, 'response_html': '<p>' + escape(response).replace('\n', '<br>') + '</p>'})

        pedido = Pedido.objects.filter(cliente=request.user).order_by('-fecha_creacion').first()
        if not pedido:
           return _text_response('Todavía no tienes pedidos registrados.')

        entrega = getattr(pedido, 'entrega', None)
        response = (
           f'Pedido #{pedido.id}\n'
           f'Estado: {pedido.get_estado_display()}\n'
           f'Total: ${pedido.total:.2f}\n'
           f'Fecha: {pedido.fecha_creacion.strftime("%d/%m/%Y")}'
        )
        if entrega:
           estado_entrega = entrega.get_estado_envio_display() if hasattr(entrega, 'get_estado_envio_display') else entrega.estado_envio
           tiempo_estimado = getattr(entrega, 'tiempo_estimado_minutos', 'No disponible')
           response += f'\nEstado de entrega: {estado_entrega}\nTiempo estimado: {tiempo_estimado} minutos'
        return JsonResponse({'response': response, 'response_html': '<p>' + escape(response).replace('\n', '<br>') + '</p>'})

    reserva_triggers = {'reserva', 'mesa', 'reservar', 'reservación', 'reservacion', 'separar mesa', 'quiero reservar', 'hacer reserva', 'mis reservas', 'estado de mi reserva'}
    if any(trigger in normalized_message for trigger in reserva_triggers):
        if 'mis reservas' in normalized_message or 'estado de mi reserva' in normalized_message:
           if not request.user.is_authenticated:
               login_url = reverse('login')
               text = 'Para consultar tus reservas debes iniciar sesión.'
               html = f'<p>{escape(text)}</p><p><a href="{escape(login_url)}" target="_blank" rel="noopener noreferrer">Iniciar sesión</a></p>'
               return JsonResponse({'response': text, 'response_html': html})
           reservas = (
               Reserva.objects
               .filter(cliente=request.user)
               .select_related('mesa')
               .order_by('-fecha', '-hora')[:5]
           )
           if not reservas.exists():
               return _text_response('No tienes reservas recientes.')
           lines = []
           for reserva in reservas:
               mesa_text = f'Mesa {reserva.mesa.numero}' if getattr(reserva, 'mesa', None) else 'Mesa por confirmar'
               lines.append(f'{reserva.fecha.strftime("%d/%m/%Y")} · {reserva.hora.strftime("%H:%M")} · {reserva.numero_personas} personas · {mesa_text} · {reserva.get_estado_display()}')
           response = 'Tus reservas recientes:\n- ' + '\n- '.join(lines)
           return JsonResponse({'response': response, 'response_html': '<p>' + escape(response).replace('\n', '<br>') + '</p>'})

        text = 'Puedes reservar una mesa desde la sección de reservas del restaurante. Haz clic en el botón para continuar.'
        return _navigation_response(text, 'Reservar mesa', reverse('reservas:crear_reserva'))

    payment_triggers = {'como puedo pagar', 'formas de pago', 'metodos de pago', 'métodos de pago', 'aceptan transferencia', 'pago', 'pagar', 'transferencia', 'comprobante'}
    if any(trigger in normalized_message for trigger in payment_triggers):
        if 'comprobante' in normalized_message or 'subir comprobante' in normalized_message or 'pago por transferencia' in normalized_message:
           response = (
               '1. Realiza el pedido.\n'
               '2. Selecciona transferencia bancaria.\n'
               '3. Revisa los datos bancarios.\n'
               '4. Realiza la transferencia.\n'
               '5. Sube la imagen o archivo del comprobante.\n'
               '6. Espera la validación del personal autorizado.'
           )
           return JsonResponse({'response': response, 'response_html': '<p>' + escape(response).replace('\n', '<br>') + '</p>'})

        plain, html = _shopping_payment_info()
        return JsonResponse({'response': plain, 'response_html': html})

    navigation_triggers = {
        'como pedir': ('Puedes revisar el menú, agregar productos y continuar al carrito para confirmar tu pedido.', 'Ver menú', reverse('ver_menu')),
        'como hago un pedido': ('Puedes revisar el menú, agregar productos y continuar al carrito para confirmar tu pedido.', 'Ver menú', reverse('ver_menu')),
        'como hacer un pedido': ('Puedes revisar el menú, agregar productos y continuar al carrito para confirmar tu pedido.', 'Ver menú', reverse('ver_menu')),
        'hacer un pedido': ('Puedes revisar el menú, agregar productos y continuar al carrito para confirmar tu pedido.', 'Ver menú', reverse('ver_menu')),
        'reservar mesa': ('Puedes reservar una mesa completando el formulario de reservas.', 'Reservar mesa', reverse('reservas:crear_reserva')),
        'como reservar': ('Puedes reservar una mesa completando el formulario de reservas.', 'Reservar mesa', reverse('reservas:crear_reserva')),
        'editar pedido': ('Desde el carrito puedes modificar cantidades o eliminar productos antes de confirmar.', 'Ver mi carrito', reverse('pedidos:view_cart')),
        'ver mi carrito': ('Desde el carrito puedes revisar y editar los productos antes de confirmar.', 'Ver mi carrito', reverse('pedidos:view_cart')),
    }
    for trigger, (text, label, url) in navigation_triggers.items():
        if trigger in normalized_message:
            return _navigation_response(text, label, url)

    delivery_triggers = {'hacen entregas', 'delivery', 'entrega a domicilio', 'envios', 'envío', 'entregas', 'domicilio'}
    if any(trigger in normalized_message for trigger in delivery_triggers):
        if site_config:
           info = [
               'Sí, contamos con servicio de entrega a domicilio.',
               f'Radio de entrega: {site_config.delivery_standard_radius_km} km.',
               f'Tiempo estimado: {site_config.delivery_base_prep_minutes} minutos de preparación más la distancia.',
           ]
           text = ' '.join(info)
        else:
           text = 'Sí, contamos con servicio de entrega a domicilio. El tiempo exacto depende de la distancia y la zona de entrega.'
        return JsonResponse({'response': text, 'response_html': f'<p>{escape(text)}</p>'})

    location_triggers = {
        'donde estan', 'donde están', 'ubicacion', 'ubicación', 'direccion',
        'dirección', 'como llegar', 'donde se encuentran', 'mapa', 'local',
    }
    if any(trigger in normalized_message for trigger in location_triggers):
        direccion = (site_config.direccion if site_config and site_config.direccion else 'Archidona, Ecuador')
        response = f'Nuestra dirección es: {direccion}.'
        embed_url = (
            site_config.mapa_iframe_url
            if site_config and site_config.mapa_iframe_url
            else 'https://www.google.com/maps?q=Rukullacta%2C%20Archidona&output=embed'
        )
        maps_url = (
            'https://www.google.com/maps/search/?api=1&query='
            + quote_plus(direccion)
        )
        html = (
            f'<p>{escape(response)}</p>'
            f'<p><a class="rpm-chat-navigation-link" href="{escape(maps_url)}" '
            'target="_blank" rel="noopener noreferrer">Abrir en Google Maps</a></p>'
        )
        return JsonResponse({
            'response': response,
            'response_html': html,
            'location': {
                'address': direccion,
                'maps_url': maps_url,
                'embed_url': embed_url,
            },
        })

    schedule_triggers = {'horario', 'horarios', 'a que hora abren', 'a que hora cierran', 'a qué hora abren', 'abren', 'cierran', 'atencion', 'atención', 'estan atendiendo', 'están atendiendo'}
    if any(trigger in normalized_message for trigger in schedule_triggers):
        horario_text = (site_config.horario if site_config and site_config.horario else 'Lun - Dom: 11:00 - 22:00')
        response = f'Horario de atención: {horario_text}.'
        return JsonResponse({'response': response, 'response_html': f'<p>{escape(response)}</p>'})

    if 'atencion al cliente' in normalized_message or 'atención al cliente' in normalized_message:
        if site_config and site_config.whatsapp:
            contact_url = (
                'https://wa.me/'
                + re.sub(r'\D', '', site_config.whatsapp)
            )
            return _navigation_response(
                'Puedes comunicarte directamente con nuestro equipo por WhatsApp.',
                'Contactar por WhatsApp',
                contact_url,
            )
        if site_config and site_config.telefono:
            return _navigation_response(
                'Puedes comunicarte directamente con nuestro equipo por teléfono.',
                'Llamar al restaurante',
                f'tel:{site_config.telefono}',
            )
        return _text_response(
            'Puedes comunicarte con el restaurante desde los datos de contacto mostrados en el pie de página.'
        )

    if any(trigger in normalized_message for trigger in ['ayuda', 'help', 'consulta']) or 'que puedes hacer' in normalized_message:
        intro = '¡Hola! 👋 Puedo ayudarte con menú, precios, pedidos, reservas, pagos, entregas, horarios y ubicación.'
        quick = _quick_buttons_html([
           ('🍔 Ver menú', 'Ver menú'),
           ('💰 Consultar precios', 'Consultar precios'),
           ('📦 Mi pedido', 'Mi pedido'),
           ('🪑 Reservar mesa', 'Reservar mesa'),
           ('📍 Ubicación', 'Ubicación'),
           ('🕐 Horarios', 'Horarios'),
        ])
        return JsonResponse({'response': intro, 'response_html': f'<p>{escape(intro)}</p>{quick}'})

    if 'como te llamas' in normalized_message or 'quien eres' in normalized_message:
        response = 'Soy el asistente virtual de Ricas Papas Mary y puedo ayudarte con menú, precios, pedidos, reservas, pagos, entregas, horarios y ubicación.'
        return JsonResponse({'response': response, 'response_html': f'<p>{escape(response)}</p>'})

    if 'gracias' in normalized_message or 'muchas gracias' in normalized_message:
        response = '¡Con gusto! Estoy aquí para ayudarte con tu consulta.'
        return JsonResponse({'response': response, 'response_html': f'<p>{escape(response)}</p>'})

    product_search = _extract_product_query(request.session.get('chat_last_product', ''))
    if product_search:
        matching_products = _lookup_products(product_search)
        if matching_products.exists():
            request.session['chat_last_product'] = matching_products[0].nombre
            response = f'Encontré estos productos relacionados con "{product_search}":'
            return JsonResponse({
                'response': response,
                'response_html': _product_cards_html(request, matching_products),
                'products': _product_payload(request, matching_products),
            })

    return _respuesta_no_entiendo()

def splash(request):
    return render(request, 'gestion_web/splash.html')

def home(request):
    search_query = request.GET.get('q', '').strip()
    active_category = request.GET.get('categoria', '').strip().lower()
    configuracion_sitio = ConfiguracionSitio.objects.filter(activo=True).order_by('-id').first()
    platos_destacados = []
    mostrar_seccion_destacados = True
    featured_section_kicker = 'Menu completo'
    featured_section_title = 'Todos nuestros platos en movimiento'
    featured_section_subtitle = 'Recorre todo el menu disponible y agrega tus favoritos al carrito.'
    hero_primary_button_text = 'Explorar pedidos'
    hero_primary_button_url = reverse('ver_menu')
    hero_secondary_button_text = 'Crear cuenta'
    hero_secondary_button_url = reverse('registro')
    destacados_curados = []

    if configuracion_sitio:
        mostrar_seccion_destacados = configuracion_sitio.mostrar_seccion_destacados
        featured_section_kicker = configuracion_sitio.etiqueta_seccion_destacados or featured_section_kicker
        featured_section_title = configuracion_sitio.titulo_seccion_destacados or featured_section_title
        featured_section_subtitle = configuracion_sitio.subtitulo_seccion_destacados or featured_section_subtitle
        hero_primary_button_text = configuracion_sitio.texto_boton_principal_hero or hero_primary_button_text
        hero_primary_button_url = configuracion_sitio.enlace_boton_principal_hero or hero_primary_button_url
        hero_secondary_button_text = configuracion_sitio.texto_boton_secundario_hero or hero_secondary_button_text
        hero_secondary_button_url = configuracion_sitio.enlace_boton_secundario_hero or hero_secondary_button_url

        destacados_curados = [
            {
                'producto': destacado.producto,
                'descripcion_corta': destacado.descripcion_corta,
            }
            for destacado in configuracion_sitio.destacados_portada.select_related('producto').all()
            if destacado.producto.disponible
        ]

        if not destacados_curados:
            destacados_curados = list(
                {
                    'producto': producto,
                    'descripcion_corta': '',
                }
                for producto in configuracion_sitio.platos_destacados.filter(disponible=True).order_by('-id')[:5]
            )

    # El carrusel principal debe mostrar TODO el catalogo disponible.
    # Si hay destacados curados, los mostramos primero y luego anexamos el resto.
    destacados_ids = []
    destacados_lookup = {}
    for destacado in destacados_curados:
        producto = destacado['producto']
        if producto.id in destacados_lookup:
            continue
        destacados_ids.append(producto.id)
        destacados_lookup[producto.id] = destacado.get('descripcion_corta', '')

    if destacados_ids:
        for destacado in destacados_curados:
            producto = destacado['producto']
            if not producto.disponible:
                continue
            platos_destacados.append({
                'producto': producto,
                'descripcion_corta': destacado.get('descripcion_corta', ''),
            })

    productos_restantes = Producto.objects.filter(disponible=True)
    if destacados_ids:
        productos_restantes = productos_restantes.exclude(id__in=destacados_ids)

    for producto in productos_restantes.order_by('-id'):
        platos_destacados.append({
            'producto': producto,
            'descripcion_corta': destacados_lookup.get(producto.id, ''),
        })

    filter_terms = {
        'combos': ('combo',),
        'papas': ('papa', 'papas', 'frita', 'fritas'),
        'bebidas': ('bebida', 'jugo', 'agua', 'gaseosa', 'cola'),
    }
    category_labels = {
        'combos': 'Combos',
        'papas': 'Papas Fritas',
        'bebidas': 'Bebidas',
    }
    if active_category in filter_terms:
        search_terms = filter_terms[active_category]
        platos_destacados = [
            plato for plato in platos_destacados
            if any(
                term in f"{plato['producto'].nombre} {plato['producto'].descripcion or ''}".lower()
                for term in search_terms
            )
        ]

    if search_query:
        normalized_query = search_query.lower()
        platos_destacados = [
            plato for plato in platos_destacados
            if normalized_query in plato['producto'].nombre.lower()
            or normalized_query in (plato['producto'].descripcion or '').lower(            )
        ]

    if active_category in category_labels:
        featured_section_kicker = f'Categoría seleccionada: {category_labels[active_category]}'
        featured_section_title = category_labels[active_category]
        featured_section_subtitle = 'Selecciona un producto para conocer sus detalles y agregarlo a tu pedido.'

    return render(request, 'gestion_web/home.html', {
        'configuracion_sitio': configuracion_sitio,
        'platos_destacados': platos_destacados,
        'mostrar_seccion_destacados': mostrar_seccion_destacados,
        'featured_section_kicker': featured_section_kicker,
        'featured_section_title': featured_section_title,
        'featured_section_subtitle': featured_section_subtitle,
        'hero_primary_button_text': hero_primary_button_text,
        'hero_primary_button_url': hero_primary_button_url,
        'hero_secondary_button_text': hero_secondary_button_text,
        'hero_secondary_button_url': hero_secondary_button_url,
        'search_query': search_query,
        'active_category': active_category,
        'category_label': category_labels.get(active_category, ''),
    })

# Vista principal "Realizar Pedido"
@login_required
def ver_menu(request):
    # Mensaje de confirmación de pedidos (placeholder actual del flujo de pedidos).
    mensaje_confirmacion = None
    modulo = request.GET.get('modulo', '').strip().lower()
    modulo_reservas = modulo == 'reservas'
    modulo_pedidos = modulo != 'reservas'

    # Mensajes flash de reservas (se leen desde sesión y se consumen una sola vez).
    # Esto permite redirigir tras POST y mostrar feedback en el GET siguiente.
    reserva_confirmacion = request.session.pop('reserva_confirmacion', None)
    reserva_error = request.session.pop('reserva_error', None)
    reserva_sugerencias = request.session.pop('reserva_sugerencias', [])
    reserva_sugerida_mesa_id = request.session.pop('reserva_sugerida_mesa_id', None)

    # Filtrado obligatorio solicitado:
    # solamente exponemos al cliente mesas cuyo estado operativo sea DISPONIBLE.
    mesas = list(Mesa.objects.order_by('numero'))

    # Catálogo de productos visible en la pestaña de pedidos.
    productos = Producto.objects.filter(disponible=True)
    
    mis_reservas = []
    mis_pedidos = []

    # Cargamos historial del usuario para las pestañas "Mis Reservas" y "Mis Entregas".
    if request.user.is_authenticated:
        mis_reservas = list(
            Reserva.objects
            .filter(cliente=request.user)
            .select_related('mesa', 'pedido')
            .order_by('-fecha', '-hora')
        )
        detalles_prefetch = Prefetch(
            'detalles',
            queryset=DetallePedido.objects.select_related('producto'),
            to_attr='detalles_para_cliente',
        )
        mis_pedidos = list(
            Pedido.objects
            .filter(cliente=request.user)
            .select_related('pago', 'comprobante', 'entrega')
            .prefetch_related(detalles_prefetch)
            .order_by('-fecha_creacion')
        )

        # Añadir atributos legibles para estado para evitar problemas de renderizado literal
        for r in mis_reservas:
            r.estado_display = r.get_estado_display()
        for p in mis_pedidos:
            p.estado_display = p.get_estado_display()
            for detalle in p.detalles_para_cliente:
                detalle.subtotal_cliente = detalle.cantidad * detalle.precio_unitario
            # Detectamos si el pago fue confirmado por cualquiera de las vías del sistema.
            comprobante_validado = False
            try:
                comprobante_validado = p.comprobante.estado == 'VALIDADO'
            except AttributeError:
                comprobante_validado = False
            pago_reportado = False
            try:
                pago_reportado = p.pago.estado == 'CONFIRMADO'
            except AttributeError:
                pago_reportado = False
            p.tiene_pago_aprobado = (
                p.estado == 'PAGADO'
                or pago_reportado
                or comprobante_validado
            )

            # Mantenemos el estado real del pedido para evitar incoherencias en el historial.
            # Solo usamos "Pago confirmado" cuando realmente está en etapa de pago.
            if p.estado == 'PENDIENTE_PAGO':
                p.estado_display_cliente = 'Pago confirmado' if p.tiene_pago_aprobado else 'Pendiente de pago'
            elif p.estado == 'PAGADO':
                p.estado_display_cliente = 'Pago confirmado'
            else:
                p.estado_display_cliente = p.estado_display

            # Si el pedido es a domicilio, compartimos la ubicación para cliente,
            # admin y repartidor desde la misma fuente de datos.
            p.entrega_direccion = ''
            p.entrega_latitud = None
            p.entrega_longitud = None
            p.entrega_mapa_url = ''
            p.entrega_ruta_url = ''
            entrega = getattr(p, 'entrega', None)
            if p.estado == 'ENTREGADO':
                p.estado_modal_display = 'Entregado'
            elif p.estado == 'CANCELADO':
                p.estado_modal_display = 'Cancelado'
            elif p.estado == 'PREPARANDO':
                p.estado_modal_display = 'En preparación'
            elif entrega and p.tipo == 'DOMICILIO' and getattr(entrega, 'estado_envio', ''):
                p.estado_modal_display = entrega.estado_envio
            else:
                p.estado_modal_display = 'Pendiente'
            if entrega:
                p.entrega_direccion = entrega.direccion or ''
                p.entrega_latitud = entrega.latitud
                p.entrega_longitud = entrega.longitud

                if entrega.latitud is not None and entrega.longitud is not None:
                    destination = f'{entrega.latitud},{entrega.longitud}'
                else:
                    destination = quote_plus(entrega.direccion or '')

                if destination:
                    p.entrega_mapa_url = f'https://www.google.com/maps?q={destination}'
                    p.entrega_ruta_url = f'https://www.google.com/maps/dir/?api=1&destination={destination}'

            # ETA visible para el cliente en pedidos a domicilio.
            p.tiempo_estimado_entrega = ''
            if p.tipo == 'DOMICILIO':
                if p.estado == 'ENTREGADO':
                    p.tiempo_estimado_entrega = 'Entregado'
                elif p.estado == 'CANCELADO':
                    p.tiempo_estimado_entrega = 'Pedido cancelado'
                elif p.estado == 'PENDIENTE_PAGO' and not p.tiene_pago_aprobado:
                    p.tiempo_estimado_entrega = 'Pendiente de confirmación de pago'
                else:
                    entrega_estimada = getattr(entrega, 'tiempo_estimado_minutos', None) if entrega else None
                    if entrega_estimada is None:
                        entrega_estimada = 30

                    referencia = timezone.localtime(p.fecha_creacion)
                    eta_obj = referencia + timedelta(minutes=int(entrega_estimada))
                    ahora = timezone.localtime(timezone.now())
                    minutos_restantes = ceil((eta_obj - ahora).total_seconds() / 60)

                    if minutos_restantes <= 0:
                        p.tiempo_estimado_entrega = 'Llegando en pocos minutos'
                    elif minutos_restantes < 60:
                        p.tiempo_estimado_entrega = f'{minutos_restantes} min aprox.'
                    else:
                        horas = minutos_restantes // 60
                        mins = minutos_restantes % 60
                        if mins == 0:
                            p.tiempo_estimado_entrega = f'{horas} h aprox.'
                        else:
                            p.tiempo_estimado_entrega = f'{horas} h {mins} min aprox.'

    return render(request, 'gestion_web/menu.html', {
        'productos': productos,
        'mesas': mesas,
        'mensaje_confirmacion': mensaje_confirmacion,
        'reserva_confirmacion': reserva_confirmacion,
        'reserva_error': reserva_error,
        'reserva_sugerencias': reserva_sugerencias,
        # Si hubo rechazo por mesa ocupada/no válida, preseleccionamos una sugerida.
        'reserva_sugerida_mesa_id': reserva_sugerida_mesa_id,
        'reservas': mis_reservas,
        'pedidos': mis_pedidos,
        'test_valor': 'FUNCIONA_CORRECTAMENTE',
        'modulo_reservas': modulo_reservas,
        'modulo_pedidos': modulo_pedidos,
    })

@login_required
@require_http_methods(["GET"])
def disponibilidad_mesas(request):
    """Devuelve la disponibilidad exacta de cada mesa para fecha y hora."""
    fecha = request.GET.get('fecha')
    hora = request.GET.get('hora')
    personas = request.GET.get('personas')

    try:
        reserva_fecha = datetime.strptime(fecha or '', '%Y-%m-%d').date()
        reserva_hora = datetime.strptime(hora or '', '%H:%M').time()
    except (TypeError, ValueError):
        return JsonResponse({'mesas': [], 'error': 'Fecha y hora inválidas.'}, status=400)

    if reserva_fecha < timezone.localdate():
        return JsonResponse(
            {'mesas': [], 'error': 'No puedes consultar disponibilidad para una fecha pasada.'},
            status=400,
        )

    try:
        numero_personas = int(personas) if personas else None
    except (TypeError, ValueError):
        numero_personas = None

    mesas = list(Mesa.objects.order_by('numero'))
    ocupadas = set(
        Reserva.objects.filter(
            fecha=reserva_fecha,
            hora=reserva_hora,
            estado=Reserva.EstadoReserva.CONFIRMADA,
            mesa__isnull=False,
        ).values_list('mesa_id', flat=True)
    )
    resultado = []
    for mesa in mesas:
        operativa = mesa.estado == Mesa.EstadoMesa.DISPONIBLE
        capacidad_valida = numero_personas is None or mesa.capacidad >= numero_personas
        ocupada = mesa.id in ocupadas
        if not operativa:
            estado = 'deshabilitada'
        elif ocupada or not capacidad_valida:
            estado = 'ocupada'
        else:
            estado = 'disponible'
        resultado.append({
            'id': mesa.id,
            'nombre': f'Mesa {mesa.numero}',
            'numero': mesa.numero,
            'capacidad': mesa.capacidad,
            'estado': estado,
            'seleccionable': estado == 'disponible',
        })
    return JsonResponse({'mesas': resultado})

@login_required
@transaction.atomic
def crear_reserva(request):
    # Este endpoint procesa el formulario de reserva del cliente.
    # Regla de negocio principal:
    # 1) Solo se puede reservar una mesa en estado DISPONIBLE.
    # 2) Si la mesa elegida está ocupada/no disponible, se rechaza y se sugiere una libre.
    # 3) Si no se eligió mesa, el sistema asigna la primera disponible que cumpla capacidad/horario.
    if request.method == 'POST':
        # Capturamos campos básicos del formulario.
        fecha = request.POST.get('fecha')
        hora = request.POST.get('hora')
        numero_personas = request.POST.get('numero_personas') or request.POST.get('personas')
        notas = request.POST.get('notas') or request.POST.get('notes') or ''
        mesa_id = request.POST.get('mesa_id')
        prepedido_ids = request.POST.getlist('prepedido_productos')
        # También consideramos como seleccionado cualquier producto cuya cantidad
        # enviada sea mayor a 1 (el usuario pudo aumentar cantidad pero olvidar
        # marcar el checkbox). Evita perder selecciones por errores de UX.
        try:
            # normalizamos a strings (getlist devuelve strings)
            prepedido_set = set(str(x) for x in prepedido_ids)
        except (TypeError, ValueError):
            prepedido_set = set()

        for p in Producto.objects.filter(disponible=True):
            try:
                cant = int(request.POST.get(f'prepedido_cantidad_{p.id}', '1'))
            except (TypeError, ValueError):
                cant = 1
            if cant > 1:
                prepedido_set.add(str(p.id))

        prepedido_ids = list(prepedido_set)

        # El pre-pedido es opcional: solo se convierte en Pedido si el cliente marcó al menos un plato.
        prepedido_items = []
        prepedido_total = Decimal('0.00')

        # `mesa_asignada` será la mesa final aprobada para la reserva.
        mesa_asignada = None

        # Convertimos personas a entero para poder filtrar por capacidad.
        try:
            miembros = int(numero_personas)
        except (TypeError, ValueError):
            miembros = 1

        # Parseamos fecha y hora para validación de solapes por intervalo.
        try:
            reserva_fecha = datetime.strptime(fecha, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            reserva_fecha = None
        try:
            reserva_hora = datetime.strptime(hora, '%H:%M').time()
        except (TypeError, ValueError):
            reserva_hora = None

        if not reserva_fecha or not reserva_hora:
            request.session['reserva_error'] = 'Ingresa una fecha y hora válidas.'
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        hoy_local = timezone.localdate()
        if reserva_fecha < hoy_local:
            request.session['reserva_error'] = (
                'No puedes reservar una fecha pasada. Selecciona hoy o una fecha futura.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        if not dentro_del_horario(reserva_hora):
            request.session['reserva_error'] = (
                f'{horario_atencion_texto()} No puedes reservar fuera de ese horario.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        ahora_local = timezone.localtime()
        if reserva_fecha == hoy_local and reserva_hora < ahora_local.time().replace(second=0, microsecond=0):
            request.session['reserva_error'] = (
                'La hora elegida ya pasó. Selecciona una hora futura dentro del horario de atención.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        mesas_candidatas = list(Mesa.objects.select_for_update().filter(
            estado=Mesa.EstadoMesa.DISPONIBLE,
            capacidad__gte=miembros,
        ).order_by('numero'))

        def mesa_disponible(mesa):
            return not Reserva.objects.filter(
                mesa=mesa,
                fecha=reserva_fecha,
                hora=reserva_hora,
                estado=Reserva.EstadoReserva.CONFIRMADA,
            ).exists()

        mesa_sugerida = next((mesa for mesa in mesas_candidatas if mesa_disponible(mesa)), None)

        # Si el cliente intentó forzar una mesa específica (mesa_id), la validamos.
        if mesa_id:
            mesa_seleccionada = Mesa.objects.select_for_update().filter(id=mesa_id).first()

            # Caso 1: id inexistente o alterado.
            if not mesa_seleccionada:
                request.session['reserva_error'] = 'La mesa seleccionada no existe.'
                if mesa_sugerida:
                    request.session['reserva_error'] = (
                        f'La mesa seleccionada no existe. Te sugerimos seleccionar la Mesa {mesa_sugerida.numero}.'
                    )
                    request.session['reserva_sugerida_mesa_id'] = mesa_sugerida.id
                return redirect(f"{reverse('ver_menu')}?modulo=reservas")

            # Caso 2: mesa no disponible por estado operativo (ocupada o mantenimiento).
            if mesa_seleccionada.estado != Mesa.EstadoMesa.DISPONIBLE:
                request.session['reserva_error'] = (
                    f'La Mesa {mesa_seleccionada.numero} esta ocupada o no disponible.'
                )
                if mesa_sugerida:
                    request.session['reserva_error'] = (
                        f'La Mesa {mesa_seleccionada.numero} esta ocupada. '
                        f'Te sugerimos seleccionar la Mesa {mesa_sugerida.numero}.'
                    )
                    request.session['reserva_sugerida_mesa_id'] = mesa_sugerida.id
                return redirect(f"{reverse('ver_menu')}?modulo=reservas")

            if mesa_seleccionada.capacidad < miembros:
                request.session['reserva_error'] = (
                    f'La Mesa {mesa_seleccionada.numero} no tiene capacidad para {miembros} personas.'
                )
                if mesa_sugerida:
                    request.session['reserva_sugerida_mesa_id'] = mesa_sugerida.id
                return redirect(f"{reverse('ver_menu')}?modulo=reservas")

            # Caso 3: mesa disponible por estado, pero ocupada por cruce horario.
            if not mesa_disponible(mesa_seleccionada):
                request.session['reserva_error'] = (
                    f'La Mesa {mesa_seleccionada.numero} ya se encuentra reservada para la fecha y hora seleccionadas. '
                    'Por favor, elige otra mesa u otro horario.'
                )
                return redirect(f"{reverse('ver_menu')}?modulo=reservas")

            # Caso válido: el cliente eligió una mesa realmente disponible.
            mesa_asignada = mesa_seleccionada
        else:
            # Si no eligió mesa manualmente, asignamos la primera disponible automática.
            mesa_asignada = mesa_sugerida

        # Si no hay mesa disponible en horario exacto, proponemos horarios cercanos.
        if not mesa_asignada:
            request.session['reserva_error'] = (
                'No hay mesas disponibles para esa fecha y hora. Intenta con otro horario.'
            )
            request.session['reserva_sugerencias'] = []
            request.session.pop('reserva_sugerida_mesa_id', None)
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        # Si la reserva ya es válida, convertimos los platos opcionales en un Pedido vinculado.
        for producto_id in prepedido_ids:
            try:
                producto = Producto.objects.get(id=producto_id, disponible=True)
            except Producto.DoesNotExist:
                continue

            try:
                cantidad = int(request.POST.get(f'prepedido_cantidad_{producto_id}', 1))
            except (TypeError, ValueError):
                cantidad = 1

            if cantidad < 1:
                cantidad = 1

            subtotal = producto.precio * cantidad
            prepedido_items.append({
                'producto': producto,
                'cantidad': cantidad,
                'precio_unitario': producto.precio,
                'subtotal': subtotal,
            })
            prepedido_total += subtotal

        pedido_previo = None

        # Si llegamos aquí, la mesa ya fue validada y la reserva se puede crear.
        estado = Reserva.EstadoReserva.CONFIRMADA

        try:
            with transaction.atomic():
                reserva = Reserva.objects.create(
                    cliente=request.user,
                    mesa=mesa_asignada,
                    pedido=pedido_previo,
                    fecha=reserva_fecha,
                    hora=reserva_hora,
                    numero_personas=miembros,
                    estado=estado,
                    notes=notas,
                )

                if prepedido_items:
                    pedido_previo = Pedido.objects.create(
                        cliente=request.user,
                        tipo=Pedido.TipoPedido.LOCAL,
                        estado=Pedido.EstadoPedido.PENDIENTE_PAGO,
                        total=prepedido_total,
                    )
                    for item in prepedido_items:
                        DetallePedido.objects.create(
                            pedido=pedido_previo,
                            producto=item['producto'],
                            cantidad=item['cantidad'],
                            precio_unitario=item['precio_unitario'],
                        )
                    reserva.pedido = pedido_previo
                    reserva.save(update_fields=['pedido'])
        except IntegrityError:
            request.session['reserva_error'] = (
                f'La Mesa {mesa_asignada.numero} ya se encuentra reservada para la fecha y hora '
                'seleccionadas. Por favor, elige otra mesa u otro horario.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")
        except ValidationError:
            request.session['reserva_error'] = (
                'No se pudo completar la reserva porque la mesa o el inventario '
                'cambiaron mientras se procesaba. Por favor, intenta nuevamente.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")
        except DatabaseError:
            request.session['reserva_error'] = (
                'No se pudo guardar la reserva por un problema temporal. '
                'Verifica los datos e inténtalo nuevamente.'
            )
            return redirect(f"{reverse('ver_menu')}?modulo=reservas")

        # Programamos el correo después del commit para no deshacer la reserva si el envío falla.
        if reserva.estado == 'CONFIRMADA':
            transaction.on_commit(lambda: send_reserva_confirmada_email(reserva))

        # Mensaje de éxito que se mostrará en la pestaña de reservas.
        request.session['reserva_confirmacion'] = (
            f"✅ ¡Reserva agendada con éxito! Te hemos asignado la Mesa #{mesa_asignada.numero}"
        )
        # Limpiamos sugerencias viejas para evitar que aparezcan tras un éxito.
        request.session.pop('reserva_sugerencias', None)
        request.session.pop('reserva_sugerida_mesa_id', None)

        return redirect(f"{reverse('ver_menu')}?modulo=reservas")

    return redirect(f"{reverse('ver_menu')}?modulo=reservas")

@require_http_methods(["GET", "POST"])
def registrar_usuario(request):
    if request.method == 'POST':
        form = RegistroForm(request.POST)
        if form.is_valid():
            try:
                # Guardamos la aceptación de política de datos al momento de crear la cuenta.
                usuario = form.save(commit=False)
                usuario.acepta_politica_datos = True
                usuario.fecha_aceptacion_politica = timezone.now()
                usuario.version_politica_datos = 'v1.0'
                usuario.set_password(form.cleaned_data['password1'])
                usuario.save()
            except IntegrityError:
                form.add_error('username', 'Este nombre de usuario ya está en uso. Prueba con otro.')
                messages.error(request, 'Ese nombre de usuario ya está ocupado. Prueba con otro.')
            else:
                messages.success(request, '¡Tu cuenta quedó lista! Ahora puedes iniciar sesión.')
                return redirect('login')
    else:
        form = RegistroForm()

    return render(request, 'gestion_web/register.html', {'form': form})


def politica_privacidad(request):
    return render(request, 'gestion_web/politica_privacidad.html')


def _es_personal_administrativo(user):
    return (
        user.is_authenticated
        and user.is_staff
        and getattr(user, 'rol', None) == 'ADMIN'
    )


@login_required
@user_passes_test(_es_personal_administrativo)
def reportes_admin(request):
    """Presenta indicadores operativos calculados desde los registros del sistema."""
    hoy = timezone.localdate()
    fecha_desde = request.GET.get('desde', '').strip()
    fecha_hasta = request.GET.get('hasta', '').strip()

    try:
        desde = datetime.strptime(fecha_desde, '%Y-%m-%d').date() if fecha_desde else hoy.replace(day=1)
        hasta = datetime.strptime(fecha_hasta, '%Y-%m-%d').date() if fecha_hasta else hoy
    except ValueError:
        desde = hoy.replace(day=1)
        hasta = hoy

    if desde > hasta:
        desde, hasta = hasta, desde

    pedidos = Pedido.objects.filter(
        fecha_creacion__date__range=(desde, hasta),
    ).select_related('cliente')
    reservas = Reserva.objects.filter(
        fecha__range=(desde, hasta),
    ).select_related('cliente', 'mesa', 'pedido')

    estados_pedido = list(
        pedidos.values('estado').annotate(total=Count('id')).order_by('estado')
    )
    tipos_pedido = list(
        pedidos.values('tipo').annotate(total=Count('id')).order_by('tipo')
    )
    pedidos_no_cancelados = pedidos.exclude(estado=Pedido.EstadoPedido.CANCELADO)
    ventas_registradas = pedidos_no_cancelados.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
    ventas_pagadas = Pago.objects.filter(
        estado=Pago.EstadoPago.CONFIRMADO,
        pedido__in=pedidos,
    ).aggregate(total=Sum('monto'))['total'] or Decimal('0.00')

    productos_mas_pedidos = list(
        DetallePedido.objects.filter(
            pedido__in=pedidos_no_cancelados,
        ).values(
            'producto__nombre',
        ).annotate(
            cantidad=Sum('cantidad'),
        ).order_by('-cantidad', 'producto__nombre')[:10]
    )
    estados_reserva = list(
        reservas.values('estado').annotate(total=Count('id')).order_by('estado')
    )
    entregas = Entrega.objects.filter(
        pedido__in=pedidos,
    ).select_related('pedido', 'pedido__cliente')
    entregas_por_estado = entregas.values('estado_envio').annotate(total=Count('id')).order_by('estado_envio')

    # Entregas por dia (basado en fecha de creación del pedido asociado)
    entregas_por_dia = list(
        entregas.annotate(fecha=TruncDate('pedido__fecha_creacion')).values('fecha').annotate(total=Count('id')).order_by('fecha')
    )

    # Reservas por dia
    reservas_por_dia = list(
        reservas.annotate(fecha_dia=TruncDate('fecha')).values('fecha_dia').annotate(total=Count('id')).order_by('fecha_dia')
    )

    # Entregas por repartidor: usamos Pago.confirmado_por como proxy cuando esté disponible
    entregas_por_repartidor = list(
        # pagos ligados a pedidos que tengan entrega
        # contamos pagos confirmados por usuario (repartidor) en el rango
        # esto solo actúa como proxy si en tu flujo el repartidor reporta el pago o confirma la entrega
        # (no existe campo repartidor en Entrega en el modelo gestion_web)
        
        # Joins: Pago -> Pedido -> Entrega
        
        # Usar el ORM para agrupar
        
        # Import Pago localmente para evitar circular imports
        
    )

    insumos_sin_stock = list(
        Insumo.objects.filter(stock__lte=0).select_related('proveedor').order_by('nombre')
    )

    # Calculamos entregas por repartidor como conteo de pagos con confirmado_por que tienen entrega asociada
    entregas_por_repartidor_qs = Pago.objects.filter(
        confirmado_por__isnull=False,
        confirmado_en__date__range=(desde, hasta),
        pedido__entrega__isnull=False,
    ).values('confirmado_por__username').annotate(total=Count('id')).order_by('-total')
    entregas_por_repartidor = list(entregas_por_repartidor_qs)

    # CSV export: soportamos detalle específico para entregas o reservas
    if request.GET.get('formato') == 'pdf':
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
        from io import BytesIO

        buffer = BytesIO()
        document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm)
        styles = getSampleStyleSheet()
        story = [
            Paragraph('Ricas Papas Mary - Reporte financiero ejecutivo', styles['Title']),
            Paragraph(f'Periodo: {desde} a {hasta}', styles['Normal']),
            Spacer(1, 10),
        ]
        report_rows = [
            ['Indicador', 'Valor'],
            ['Pedidos registrados', str(pedidos.count())],
            ['Ventas no canceladas', f'${ventas_registradas:.2f}'],
            ['Ventas pagadas confirmadas', f'${ventas_pagadas:.2f}'],
            ['Reservas registradas', str(reservas.count())],
        ]
        report_table = Table(report_rows, colWidths=[110 * mm, 55 * mm])
        report_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8f1520')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#dddddd')),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
        ]))
        story.append(report_table)
        story.append(Spacer(1, 12))
        story.append(Paragraph('Productos más solicitados', styles['Heading2']))
        product_rows = [['Producto', 'Cantidad']]
        product_rows.extend([
            [row['producto__nombre'], str(row['cantidad'])]
            for row in productos_mas_pedidos
        ])
        products_table = Table(product_rows, colWidths=[110 * mm, 55 * mm])
        products_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f59e0b')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#dddddd')),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
        ]))
        story.append(products_table)
        document.build(story)
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="reporte_financiero_{desde}_{hasta}.pdf"'
        )
        return response
    if request.GET.get('formato') == 'csv':
        detalle = request.GET.get('detalle')
        if detalle == 'entregas':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response.write('\ufeff')
            writer = csv.writer(response)
            response['Content-Disposition'] = (
                f'attachment; filename="entregas_{desde.isoformat()}_{hasta.isoformat()}.csv"'
            )
            writer.writerow([
                'EntregaID', 'PedidoID', 'Cliente', 'Direccion', 'EstadoEnvio',
                'FechaPedido', 'Latitud', 'Longitud', 'TiempoEstimadoMinutos',
                'CargoAdicional',
            ])
            for e in entregas.select_related('pedido__cliente').order_by('-pedido__fecha_creacion'):
                fecha_ped = e.pedido.fecha_creacion.strftime('%Y-%m-%d %H:%M:%S') if e.pedido and e.pedido.fecha_creacion else ''
                writer.writerow([
                    e.id,
                    e.pedido.id,
                    e.pedido.cliente.username if e.pedido.cliente else '',
                    e.direccion or '',
                    e.estado_envio,
                    fecha_ped,
                    e.latitud or '',
                    e.longitud or '',
                    e.tiempo_estimado_minutos,
                    e.cargo_adicional,
                ])
            return response
        elif detalle == 'reservas':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response.write('\ufeff')
            writer = csv.writer(response)
            response['Content-Disposition'] = (
                f'attachment; filename="reservas_{desde.isoformat()}_{hasta.isoformat()}.csv"'
            )
            writer.writerow([
                'ReservaID', 'Cliente', 'Fecha', 'Hora', 'Personas', 'Mesa',
                'Estado', 'Notas',
            ])
            for r in reservas.select_related('cliente','mesa').order_by('-fecha'):
                writer.writerow([
                    r.id,
                    r.cliente.username if r.cliente else '',
                    r.fecha,
                    r.hora,
                    r.numero_personas,
                    r.mesa.numero if r.mesa else '',
                    r.estado,
                    r.notes or '',
                ])
            return response
        else:
            # Mantener el comportamiento previo para exportación resumida
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response.write('\ufeff')
            writer = csv.writer(response)
            response['Content-Disposition'] = (
                f'attachment; filename="reporte_{desde.isoformat()}_{hasta.isoformat()}.csv"'
            )
            writer.writerow(['Indicador', 'Valor'])
            writer.writerow(['Periodo', f'{desde} a {hasta}'])
            writer.writerow(['Pedidos', pedidos.count()])
            writer.writerow(['Ventas registradas', ventas_registradas])
            writer.writerow(['Ventas pagadas', ventas_pagadas])
            writer.writerow(['Reservas', reservas.count()])
            writer.writerow([])
            writer.writerow(['Estado de pedidos', 'Total'])
            for row in estados_pedido:
                writer.writerow([row['estado'], row['total']])
            writer.writerow([])
            writer.writerow(['Productos mas pedidos', 'Cantidad'])
            for row in productos_mas_pedidos:
                writer.writerow([row['producto__nombre'], row['cantidad']])
            writer.writerow([])
            writer.writerow(['Insumos sin stock', 'Unidad de medida'])
            for insumo in insumos_sin_stock:
                writer.writerow([insumo.nombre, insumo.unidad_medida])
            return response

    # Serializar series para uso en JS (Chart.js)
    entregas_por_dia_json = json.dumps([{'fecha': str(row['fecha']), 'total': row['total']} for row in entregas_por_dia])
    reservas_por_dia_json = json.dumps([{'fecha_dia': str(row['fecha_dia']), 'total': row['total']} for row in reservas_por_dia])

    context = {
        'desde': desde.isoformat(),
        'hasta': hasta.isoformat(),
        'pedidos_total': pedidos.count(),
        'ventas_registradas': ventas_registradas,
        'ventas_pagadas': ventas_pagadas,
        'reservas_total': reservas.count(),
        'estados_pedido': estados_pedido,
        'tipos_pedido': tipos_pedido,
        'productos_mas_pedidos': productos_mas_pedidos,
        'estados_reserva': estados_reserva,
        'entregas': entregas_por_estado,
        'insumos_sin_stock': insumos_sin_stock,
        'entregas_por_dia': entregas_por_dia_json,
        'reservas_por_dia': reservas_por_dia_json,
        'entregas_por_repartidor': entregas_por_repartidor,
    }
    return render(request, 'admin/reportes.html', context)


def _requiere_rol(*roles):
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise PermissionDenied
            if request.user.rol not in roles:
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
