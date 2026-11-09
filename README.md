# Dávila Tours — reservas y gestión de flota

Prototipo web responsivo desarrollado con Flask y SQLite para las reservas de viajes redondos, el seguimiento de apartados, la asignación de salidas y el control de unidades.

## Requisitos

- Python 3.10 o posterior
- pip

## Puesta en marcha

Desde esta carpeta, abre una terminal y ejecuta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
flask --app app run --debug
```

Abre `http://127.0.0.1:5000`. La base SQLite se crea automáticamente en `instance/davila_tours.sqlite3`, junto con los destinos, cinco unidades, seis choferes y las siguientes doce salidas semanales. El calendario público permite filtrar por destino y seleccionar salidas los jueves, con regreso el domingo; el mapa bloquea los asientos ocupados.

## Perfiles y endpoints

- Pasajero: `/cliente`
- Chofer: `/chofer`
- Administrador: `/administrador`
- Centro de pruebas QA: `/pruebas` (enlace directo, no aparece en el menú; requiere iniciar sesión y conserva los permisos del perfil)

El registro público crea cuentas de pasajero. Las cuentas operativas iniciales para desarrollo son:

| Perfil | Correo | Contraseña inicial |
| --- | --- | --- |
| Administrador | `admin@gmail.com` | `Davilatours2026!` |
| Chofer 1–6 | `chofer1@gmail.com` … `chofer6@gmail.com` | `Davilatours2026!` |

Configura `SECRET_KEY`, `DEMO_PASSWORD` y, si aplica, `DEMO_ADMIN_EMAIL` como variables de entorno antes de desplegar. `RESERVATION_PRICE_CENTS` establece la tarifa por pasajero en centavos de MXN; mientras no se defina, los importes aparecen como “Por confirmar”, ya que los documentos no especifican precios. Cambia las credenciales de demostración y no expongas un servidor de desarrollo en producción.

El centro de pruebas es un punto de entrada para el equipo de QA; no concede privilegios adicionales ni aparece en el menú. Comparte su URL directamente con el equipo de pruebas. Para revisar cada perfil, inicia sesión con una cuenta de ese perfil. La ruta anterior `/microbusero` redirige al portal `/chofer` y mantiene la misma autorización.

## Reservas y correo

No se integra una pasarela de pagos. Cada reserva genera un folio y empieza como **Pendiente**. El personal puede actualizarla a **Apartado** o **Pagado**; las cancelaciones se muestran de forma diferenciada y liberan los asientos. Cada apartado guarda los asientos elegidos y evita que se asignen dos veces para la misma salida.

El símbolo `@` y los dominios `gmail.com`, `hotmail.com` y `outlook.com` se cargan de tablas SQLite y se presentan como selector en registro e inicio de sesión. Las consultas usan parámetros SQL y las contraseñas se almacenan con hash seguro.

Las cuentas de pasajero, chofer y administrador pueden subir su foto en `/perfil`. Se aceptan JPG, PNG y WEBP de hasta 5 MB; la aplicación normaliza la imagen y detecta localmente un rostro antes de guardarla bajo `instance/profile_photos`. La detección no verifica identidad ni garantiza que sea una fotografía real; la foto se sirve únicamente a una sesión autenticada. La validación requiere Pillow, NumPy y OpenCV, instalados con `pip install -r requirements.txt`.

El mapa de asientos muestra una silueta en los lugares ocupados y en el puesto del chofer; no muestra las fotos de perfil de los pasajeros.

## Interfaz

Se implementó la propuesta **A — Ruta confiable** del manual de color: azul profundo `#12304A`, turquesa `#087E8B`, superficies claras, tipografía de sistema con pesos más marcados y tamaños legibles, e iconos SVG coherentes junto con colores semánticos de reserva. La portada presenta la misión, visión y equipo de Dávila Tours, además de una animación de bienvenida con el autobús. El diseño adapta listados y formularios a pantallas pequeñas, mantiene el foco visible y combina icono, color y texto en los estados.

## Documentación

Consulta la [documentación técnica y guía de usuario](Docs/Documentacion_TK001_Davila_Tours.docx) para conocer los perfiles, flujos, configuración, modelo de datos, pruebas y limitaciones documentadas del sistema.
