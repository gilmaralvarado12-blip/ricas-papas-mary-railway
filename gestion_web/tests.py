from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .context_processors import session_notice
from .forms import RegistroForm
from .middleware import (
    CLIENT_ACTIVITY_SYNC_INTERVAL_SECONDS,
    CLIENT_SESSION_EXPIRY_KEY,
    CLIENT_SESSION_TIMEOUT_SECONDS,
    SessionExpiryByRoleMiddleware,
    get_client_session_expiry,
)
from .models import Producto


class SeguridadTests(TestCase):
    def test_registro_rechaza_entrada_html_en_username(self):
        form = RegistroForm(data={
            'username': '<script>alert("x")</script>',
            'password1': 'Password123!',
            'password2': 'Password123!',
            'acepta_politica_datos': True,
        })

        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_cliente_tiene_expiracion_de_dos_horas_sin_renovacion_por_respuesta_normal(self):
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

        Usuario = get_user_model()

        cliente = Usuario.objects.create_user(username='cliente_test', password='Password123!', rol='CLIENTE')
        now = timezone.now()

        middleware = SessionExpiryByRoleMiddleware(lambda request: None)

        request_cliente = RequestFactory().get('/')
        request_cliente.user = cliente
        request_cliente.session = SessionStore()
        with patch('gestion_web.middleware.timezone.now', return_value=now):
            middleware(request_cliente)
        initial_expiry = get_client_session_expiry(request_cliente.session)
        self.assertEqual(
            initial_expiry,
            (now + timedelta(seconds=CLIENT_SESSION_TIMEOUT_SECONDS)).timestamp(),
        )

        next_request = RequestFactory().get('/')
        next_request.user = cliente
        next_request.session = request_cliente.session
        with patch('gestion_web.middleware.timezone.now', return_value=now + timedelta(hours=1)):
            middleware(next_request)
        self.assertEqual(get_client_session_expiry(next_request.session), initial_expiry)

        admin = Usuario.objects.create_user(username='admin_test', password='Password123!', rol='ADMIN')
        empleado = Usuario.objects.create_user(username='empleado_test', password='Password123!', rol='EMPLEADO')
        repartidor = Usuario.objects.create_user(username='repartidor_test', password='Password123!', rol='REPARTIDOR')

        for user in (admin, empleado, repartidor):
            request = RequestFactory().get('/')
            request.user = user
            request.session = SessionStore()
            middleware(request)
            self.assertGreater(request.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
            self.assertNotIn(CLIENT_SESSION_EXPIRY_KEY, request.session)

        privileged_client = Usuario.objects.create_user(
            username='privileged_client',
            password='Password123!',
            rol='CLIENTE',
            is_staff=True,
            is_superuser=True,
        )
        request_privileged = RequestFactory().get('/')
        request_privileged.user = privileged_client
        request_privileged.session = SessionStore()
        middleware(request_privileged)
        self.assertEqual(request_privileged.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertNotIn(CLIENT_SESSION_EXPIRY_KEY, request_privileged.session)

    def test_renovacion_requiere_cliente_y_respeta_intervalo_de_cinco_minutos(self):
        url = reverse('client_session_activity')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 401)

        Usuario = get_user_model()
        cliente = Usuario.objects.create_user(username='cliente_actividad', password='Password123!', rol='CLIENTE')
        self.client.force_login(cliente)
        now = timezone.now()

        with patch('gestion_web.views.timezone.now', return_value=now):
            first = self.client.post(url)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['renewed'])
        initial_expiry = first.json()['expires_at']
        self.assertAlmostEqual(
            initial_expiry,
            int((now + timedelta(seconds=CLIENT_SESSION_TIMEOUT_SECONDS)).timestamp() * 1000),
            delta=1000,
        )

        with patch('gestion_web.views.timezone.now', return_value=now + timedelta(minutes=4)):
            limited = self.client.post(url)
        self.assertEqual(limited.status_code, 200)
        self.assertFalse(limited.json()['renewed'])
        self.assertEqual(limited.json()['expires_at'], initial_expiry)

        with patch(
            'gestion_web.views.timezone.now',
            return_value=now + timedelta(seconds=CLIENT_ACTIVITY_SYNC_INTERVAL_SECONDS),
        ):
            renewed = self.client.post(url)
        self.assertEqual(renewed.status_code, 200)
        self.assertTrue(renewed.json()['renewed'])
        self.assertGreater(renewed.json()['expires_at'], initial_expiry)

        admin = Usuario.objects.create_user(username='admin_actividad', password='Password123!', rol='ADMIN')
        self.client.force_login(admin)
        denied = self.client.post(url)
        self.assertEqual(denied.status_code, 403)

        privileged_client = Usuario.objects.create_user(
            username='privileged_activity',
            password='Password123!',
            rol='CLIENTE',
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(privileged_client)
        privileged_denied = self.client.post(url)
        self.assertEqual(privileged_denied.status_code, 403)

    def test_renovacion_de_sesion_exige_csrf(self):
        Usuario = get_user_model()
        cliente = Usuario.objects.create_user(username='cliente_csrf', password='Password123!', rol='CLIENTE')
        client = Client(enforce_csrf_checks=True)
        client.force_login(cliente)
        url = reverse('client_session_activity')

        rejected = client.post(url)
        self.assertEqual(rejected.status_code, 403)

        request = RequestFactory().get('/')
        token = get_token(request)
        client.cookies['csrftoken'] = request.META['CSRF_COOKIE']
        accepted = client.post(url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200)

    def test_configuracion_del_aviso_se_limita_a_clientes_publicos(self):
        Usuario = get_user_model()
        cliente = Usuario.objects.create_user(username='cliente_aviso', password='Password123!', rol='CLIENTE')
        admin = Usuario.objects.create_user(username='admin_aviso', password='Password123!', rol='ADMIN')
        middleware = SessionExpiryByRoleMiddleware(lambda request: None)

        request_cliente = RequestFactory().get('/')
        request_cliente.user = cliente
        request_cliente.session = SessionStore()
        middleware(request_cliente)
        self.assertIn('client_session_expires_at_ms', session_notice(request_cliente))

        request_admin = RequestFactory().get('/')
        request_admin.user = admin
        request_admin.session = SessionStore()
        middleware(request_admin)
        self.assertEqual(session_notice(request_admin), {})

    def test_vista_publica_muestra_componente_solo_a_cliente(self):
        Usuario = get_user_model()
        cliente = Usuario.objects.create_user(username='cliente_inicio', password='Password123!', rol='CLIENTE')
        self.client.force_login(cliente)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'id="rpmSessionExpiryWarning"')
        self.assertContains(response, 'data-activity-url="/actividad-sesion/"')
        self.assertNotContains(response, 'Tu sesión sigue activa durante')

        admin = Usuario.objects.create_user(username='admin_inicio', password='Password123!', rol='ADMIN')
        self.client.force_login(admin)
        admin_response = self.client.get(reverse('home'))
        self.assertNotContains(admin_response, 'rpmSessionExpiryWarning')

    def test_sesion_vencida_es_rechazada_por_django(self):
        Usuario = get_user_model()
        cliente = Usuario.objects.create_user(username='cliente_vencido', password='Password123!', rol='CLIENTE')
        self.client.force_login(cliente)
        session = self.client.session
        expired_at = timezone.now() - timedelta(seconds=1)
        session[CLIENT_SESSION_EXPIRY_KEY] = expired_at.timestamp()
        session.set_expiry(expired_at)
        session.save()

        response = self.client.post(reverse('client_session_activity'))
        self.assertEqual(response.status_code, 401)

    def test_login_muestra_mensaje_si_la_sesion_finalizo_por_inactividad(self):
        response = self.client.get(f"{reverse('login')}?session_expired=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Tu sesión finalizó por inactividad. Inicia sesión nuevamente para continuar.',
        )


class ChatbotTests(TestCase):
    def test_chatbot_responde_json_a_consulta_valida(self):
        Producto.objects.create(
            nombre='Papa especial',
            descripcion='Papas fritas con ingredientes especiales',
            precio='3.50',
            disponible=True,
        )

        response = self.client.post(
            reverse('chatbot_response'),
            data={'message': 'Consultar precios'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertIn('response', response.json())
