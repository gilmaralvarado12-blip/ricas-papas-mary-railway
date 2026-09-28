from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.templatetags.static import static
from django.contrib.staticfiles import finders
from decimal import Decimal
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from .forms import ComprobanteForm
from .models import Comprobante
from .utils import send_comprobante_validado_email, send_comprobante_rechazado_email
from gestion_web.models import ConfiguracionSitio, Pedido

@login_required
def subir_comprobante(request, pedido_id):
    pedido = get_object_or_404(Pedido, id=pedido_id, cliente=request.user)
    # Cada pedido solo puede tener un comprobante. Si ya existe uno, lo reutilizamos para evitar violar la restricción única.
    comprobante_existente = getattr(pedido, 'comprobante', None)
    if request.method == 'POST':
        if not request.FILES.get('imagen') and not comprobante_existente:
            messages.error(request, 'Selecciona una imagen de tu comprobante para continuar.')
            form = ComprobanteForm(request.POST, request.FILES, instance=comprobante_existente)
            return render(request, 'pagos/subir_comprobante.html', {'form': form, 'pedido': pedido})
        form = ComprobanteForm(request.POST, request.FILES, instance=comprobante_existente)
        if form.is_valid():
            comprobante = form.save(commit=False)
            comprobante.pedido = pedido
            # Al subir una nueva imagen, dejamos el comprobante en revisión para que el empleado lo vuelva a aprobar.
            comprobante.estado = 'PENDIENTE'
            comprobante.validado_por = None
            comprobante.save()
            pedido.estado = 'PENDIENTE_PAGO'
            pedido.save()
            messages.success(request, 'Tu comprobante se recibió correctamente. Ahora está pendiente de revisión.')
            return redirect('ver_menu')
        messages.error(request, 'No pudimos guardar el comprobante. Revisa que la imagen sea válida e inténtalo de nuevo.')
    else:
        form = ComprobanteForm(instance=comprobante_existente)
    return render(request, 'pagos/subir_comprobante.html', {'form': form, 'pedido': pedido})

# Helper: check if user is empleado or admin
def is_empleado_or_admin(user):
    try:
        return user.rol in ('EMPLEADO', 'ADMIN')
    except AttributeError:
        return False

@login_required
@user_passes_test(is_empleado_or_admin)
def validar_comprobante(request, comprobante_id):
    comprobante = get_object_or_404(Comprobante, id=comprobante_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'validar':
            comprobante.estado = 'VALIDADO'
            comprobante.validado_por = request.user
            comprobante.save()
            # marcar pedido como pagado
            pedido = comprobante.pedido
            Pedido.objects.filter(pk=pedido.pk).update(estado='PAGADO')
            pedido.refresh_from_db(fields=['estado'])
            from gestion_web.models import Pago
            pago_obj, _ = Pago.objects.get_or_create(
                pedido=pedido,
                defaults={'monto': pedido.total, 'metodo_pago': 'TRANSFERENCIA'},
            )
            pago_obj.estado = Pago.EstadoPago.CONFIRMADO
            pago_obj.confirmado_por = request.user
            pago_obj.confirmado_en = timezone.now()
            pago_obj.save(update_fields=['estado', 'confirmado_por', 'confirmado_en'])

            send_comprobante_validado_email(comprobante)
            messages.success(request, f'El comprobante del pedido #{pedido.id} fue aprobado. El pago quedó registrado y se avisó al cliente.')
        elif action == 'rechazar':
            comprobante.estado = 'RECHAZADO'
            comprobante.validado_por = request.user
            comprobante.save()
            send_comprobante_rechazado_email(comprobante)
            messages.error(request, f'El comprobante del pedido #{comprobante.pedido.id} fue rechazado. El cliente ya fue notificado.')
        return redirect('lista_comprobantes')

    return render(request, 'pagos/validar_comprobante.html', {'comprobante': comprobante})

@login_required
@user_passes_test(is_empleado_or_admin)
def lista_comprobantes(request):
    comprobantes = Comprobante.objects.select_related(
        'pedido',
        'pedido__cliente',
    ).order_by('-fecha_subida')
    for comprobante in comprobantes:
        comprobante.estado_display = comprobante.get_estado_display()
        comprobante.pedido_estado_display = comprobante.pedido.get_estado_display()
    return render(request, 'pagos/lista_comprobantes.html', {'comprobantes': comprobantes})


def _puede_ver_recibo(request, pedido):
    return (
        request.user == pedido.cliente
        or (
            request.user.is_staff
            and getattr(request.user, 'rol', None) in ('ADMIN', 'EMPLEADO')
        )
    )


@login_required
def generar_recibo_pdf(request, pedido_id):
    pedido = get_object_or_404(
        Pedido.objects.select_related(
            'cliente', 'pago', 'entrega',
        ).prefetch_related('detalles__producto'),
        id=pedido_id,
    )
    if not _puede_ver_recibo(request, pedido):
        raise PermissionDenied

    configuracion = ConfiguracionSitio.objects.filter(activo=True).order_by('-id').first()
    direccion = getattr(getattr(pedido, 'entrega', None), 'direccion', '') or (
        getattr(configuracion, 'direccion', '') if configuracion else ''
    )
    delivery_fee = getattr(getattr(pedido, 'entrega', None), 'cargo_adicional', Decimal('0.00')) or Decimal('0.00')
    subtotal = pedido.total - delivery_fee
    pago_confirmado = getattr(getattr(pedido, 'pago', None), 'estado', '') == 'CONFIRMADO'
    context = {
        'pedido': pedido,
        'configuracion': configuracion,
        'direccion_entrega': direccion,
        'delivery_fee': delivery_fee,
        'subtotal': subtotal,
        'pago_confirmado': pago_confirmado or pedido.estado == Pedido.EstadoPedido.PAGADO,
        'logo_url': static('images/logo.png'),
    }
    for detalle in pedido.detalles.all():
        detalle.subtotal_recibo = detalle.cantidad * detalle.precio_unitario
    if request.GET.get('formato') == 'html':
        return render(request, 'pagos/recibo.html', context)

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f'Recibo Pedido #{pedido.id}',
        author='Ricas Papas Mary',
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='ReceiptSmall', parent=styles['Normal'], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name='ReceiptTitle', parent=styles['Title'], fontSize=18, textColor=colors.HexColor('#8f1520')))
    story = []
    logo_path = finders.find('images/logo.png')
    if logo_path:
        story.append(Image(logo_path, width=30 * mm, height=18 * mm))
    story.append(Paragraph('Ricas Papas Mary', styles['ReceiptTitle']))
    story.append(Paragraph(
        f'{getattr(configuracion, "direccion", "") or "Archidona"} · '
        f'{timezone.localtime(pedido.fecha_creacion).strftime("%d/%m/%Y %H:%M")}',
        styles['ReceiptSmall'],
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f'<b>Cliente:</b> {pedido.cliente.get_full_name() or pedido.cliente.username}<br/>'
        f'<b>Teléfono:</b> {pedido.cliente.telefono or "-"}<br/>'
        f'<b>Dirección:</b> {direccion or "-"}',
        styles['Normal'],
    ))
    story.append(Spacer(1, 8))
    rows = [['Cant.', 'Producto', 'P. unitario', 'Subtotal']]
    for detalle in pedido.detalles.all():
        rows.append([
            str(detalle.cantidad),
            detalle.producto.nombre,
            f'${detalle.precio_unitario:.2f}',
            f'${detalle.cantidad * detalle.precio_unitario:.2f}',
        ])
    table = Table(rows, colWidths=[18 * mm, 86 * mm, 32 * mm, 32 * mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8f1520')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#dddddd')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#faf6f1')]),
    ]))
    story.append(table)
    story.append(Spacer(1, 8))
    financial = [
        ['Subtotal', f'${subtotal:.2f}'],
        ['Tarifa de delivery', f'${delivery_fee:.2f}'],
        ['TOTAL GENERAL', f'${pedido.total:.2f}'],
        ['Estado del pago', 'PAGADO / CONFIRMADO' if context['pago_confirmado'] else 'PENDIENTE'],
    ]
    financial_table = Table(financial, colWidths=[110 * mm, 58 * mm], hAlign='RIGHT')
    financial_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 3), (-1, 3), colors.HexColor('#d1fae5') if context['pago_confirmado'] else colors.HexColor('#fef3c7')),
        ('TEXTCOLOR', (0, 3), (-1, 3), colors.HexColor('#166534') if context['pago_confirmado'] else colors.HexColor('#92400e')),
    ]))
    story.append(financial_table)
    document.build(story)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="recibo_pedido_{pedido.id}.pdf"'
    return response
