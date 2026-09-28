from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from django.test import TestCase
from unittest.mock import patch

from gestion_web.models import (
    DetallePedido,
    Insumo,
    Insumo_Producto,
    Pedido,
    Producto,
    Proveedor,
)
from .models import Cart, CartItem


class EliminarPedidoHistorialTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username='cliente_historial',
            password='secret123',
        )
        self.url = lambda pedido_id: reverse(
            'pedidos:eliminar_pedido_historial',
            args=[pedido_id],
        )
        self.client.login(username='cliente_historial', password='secret123')

    def test_elimina_pedido_cancelado(self):
        pedido = Pedido.objects.create(
            cliente=self.usuario,
            estado=Pedido.EstadoPedido.CANCELADO,
        )

        response = self.client.post(self.url(pedido.id))

        self.assertRedirects(response, reverse('ver_menu') + '?modulo=pedidos')
        self.assertFalse(Pedido.objects.filter(id=pedido.id).exists())
        self.assertEqual(
            list(get_messages(response.wsgi_request))[0].message,
            'El pedido fue eliminado de tu historial.',
        )

    def test_rechaza_pedido_en_proceso(self):
        pedido = Pedido.objects.create(
            cliente=self.usuario,
            estado=Pedido.EstadoPedido.PREPARANDO,
        )

        response = self.client.post(self.url(pedido.id))

        self.assertRedirects(response, reverse('ver_menu') + '?modulo=pedidos')
        self.assertTrue(Pedido.objects.filter(id=pedido.id).exists())
        self.assertEqual(
            list(get_messages(response.wsgi_request))[0].message,
            'No puedes eliminar un pedido que aún está en proceso.',
        )


class CheckoutInventarioTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username='cliente_checkout',
            password='secret123',
        )
        self.client.force_login(self.usuario)
        self.cart = Cart.objects.create(cliente=self.usuario)
        self.proveedor = Proveedor.objects.create(
            nombre_empresa='Proveedor de prueba',
            contacto_nombre='Contacto',
            telefono='0999999999',
            correo_electronico='proveedor@example.com',
        )

    @patch('pedidos.views.restaurante_atendiendo', return_value=True)
    def test_stock_insuficiente_conserva_carrito_y_revierte_checkout(self, _restaurante_abierto):
        papa = Insumo.objects.create(
            proveedor=self.proveedor,
            nombre='Papas de prueba',
            stock='5.00',
            unidad_medida='kg',
        )
        aceite = Insumo.objects.create(
            proveedor=self.proveedor,
            nombre='Aceite de prueba',
            stock='0.00',
            unidad_medida='l',
        )
        productos = [
            Producto.objects.create(nombre='Producto suficiente', precio='12.00'),
            Producto.objects.create(nombre='Producto sin stock', precio='12.00'),
        ]
        Insumo_Producto.objects.create(
            producto=productos[0],
            insumo=papa,
            cantidad_utilizada='1.00',
        )
        Insumo_Producto.objects.create(
            producto=productos[1],
            insumo=aceite,
            cantidad_utilizada='1.00',
        )
        for producto in productos:
            CartItem.objects.create(
                cart=self.cart,
                producto=producto,
                cantidad=1,
                precio_unitario=producto.precio,
            )

        response = self.client.post(
            reverse('pedidos:checkout'),
            {'direccion': 'Calle de prueba 123', 'metodo_pago': 'EFECTIVO'},
        )

        self.assertRedirects(response, reverse('pedidos:view_cart'))
        self.assertEqual(Pedido.objects.count(), 0)
        self.assertEqual(DetallePedido.objects.count(), 0)
        self.assertEqual(self.cart.items.count(), 2)
        papa.refresh_from_db()
        aceite.refresh_from_db()
        self.assertEqual(papa.stock, 5)
        self.assertEqual(aceite.stock, 0)
        mensajes = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any('Papas de prueba' in mensaje or 'Aceite de prueba' in mensaje for mensaje in mensajes))
        self.assertTrue(any('inventario es insuficiente' in mensaje for mensaje in mensajes))
