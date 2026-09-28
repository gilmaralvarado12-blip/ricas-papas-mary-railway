from django.urls import path
from . import views

urlpatterns = [
    path('inicio/', views.home, name='home'),
    path('actividad-sesion/', views.renovar_sesion_cliente, name='client_session_activity'),
    path('chatbot/', views.chatbot_response, name='chatbot_response'),
    path('reservas/disponibilidad/', views.disponibilidad_mesas, name='disponibilidad_mesas'),
    path('menu/', views.ver_menu, name='ver_menu'),
    path('reservas/nueva/', views.crear_reserva, name='crear_reserva'),
    path('registro/', views.registrar_usuario, name='registro'),
    path('politica-privacidad/', views.politica_privacidad, name='politica_privacidad'),
]