from django.urls import path
from . import views

urlpatterns = [
    path('subir/<int:pedido_id>/', views.subir_comprobante, name='subir_comprobante'),
    path('validar/<int:comprobante_id>/', views.validar_comprobante, name='validar_comprobante'),
    path('lista/', views.lista_comprobantes, name='lista_comprobantes'),
    path('recibo/<int:pedido_id>/', views.generar_recibo_pdf, name='generar_recibo_pdf'),
]
