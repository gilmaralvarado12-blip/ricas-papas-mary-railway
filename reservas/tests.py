from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from gestion_web.models import Mesa, Reserva


class DisponibilidadReservasTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username='cliente_reservas',
            password='Password123!',
            rol='CLIENTE',
        )
        self.mesa_1 = Mesa.objects.create(numero=1, capacidad=4, estado=Mesa.EstadoMesa.DISPONIBLE)
        self.mesa_2 = Mesa.objects.create(numero=2, capacidad=4, estado=Mesa.EstadoMesa.DISPONIBLE)
        self.mesa_3 = Mesa.objects.create(numero=3, capacidad=4, estado=Mesa.EstadoMesa.MANTENIMIENTO)
        self.fecha = date.today() + timedelta(days=7)
        self.datos = {
            'fecha': self.fecha.isoformat(),
            'hora': '12:00',
            'personas': '2',
            'mesa_id': str(self.mesa_2.id),
            'notas': '',
        }
        self.client.force_login(self.usuario)

    @patch('gestion_web.views.send_reserva_confirmada_email')
    def test_rechaza_segunda_reserva_de_la_misma_mesa_fecha_y_hora(self, _send_email):
        self.client.post(reverse('reservas:crear_reserva'), self.datos)

        response = self.client.post(reverse('reservas:crear_reserva'), self.datos)

        self.assertRedirects(response, '/menu/?modulo=reservas')
        self.assertEqual(
            Reserva.objects.filter(
                mesa=self.mesa_2,
                fecha=self.fecha,
                hora=time(12),
                estado=Reserva.EstadoReserva.CONFIRMADA,
            ).count(),
            1,
        )
        self.assertIn('ya se encuentra reservada', response.wsgi_request.session['reserva_error'])

    def test_endpoint_marca_ocupada_y_deshabilitada(self):
        Reserva.objects.create(
            cliente=self.usuario,
            mesa=self.mesa_2,
            fecha=self.fecha,
            hora=time(12),
            numero_personas=2,
        )

        response = self.client.get(reverse('disponibilidad_mesas'), {
            'fecha': self.fecha.isoformat(),
            'hora': '12:00',
            'personas': '2',
        })

        self.assertEqual(response.status_code, 200)
        estados = {mesa['numero']: mesa for mesa in response.json()['mesas']}
        self.assertEqual(estados[1]['estado'], 'disponible')
        self.assertTrue(estados[1]['seleccionable'])
        self.assertEqual(estados[2]['estado'], 'ocupada')
        self.assertFalse(estados[2]['seleccionable'])
        self.assertEqual(estados[3]['estado'], 'deshabilitada')
        self.assertFalse(estados[3]['seleccionable'])

    @patch('gestion_web.views.send_reserva_confirmada_email')
    def test_reserva_cancelada_no_bloquea_mesa(self, _send_email):
        Reserva.objects.create(
            cliente=self.usuario,
            mesa=self.mesa_2,
            fecha=self.fecha,
            hora=time(12),
            numero_personas=2,
            estado=Reserva.EstadoReserva.CANCELADA,
        )

        response = self.client.post(reverse('reservas:crear_reserva'), self.datos)

        self.assertRedirects(response, '/menu/?modulo=reservas')
        self.assertTrue(
            Reserva.objects.filter(
                mesa=self.mesa_2,
                fecha=self.fecha,
                hora=time(12),
                estado=Reserva.EstadoReserva.CONFIRMADA,
            ).exists()
        )

    @patch('gestion_web.views.send_reserva_confirmada_email')
    def test_no_permite_reservar_mesa_deshabilitada(self, _send_email):
        datos = {**self.datos, 'mesa_id': str(self.mesa_3.id)}

        response = self.client.post(reverse('reservas:crear_reserva'), datos)

        self.assertRedirects(response, '/menu/?modulo=reservas')
        self.assertFalse(Reserva.objects.filter(mesa=self.mesa_3).exists())
