# Dávila Tours — reservas y gestión de flota

Prototipo web responsivo desarrollado con Flask y SQLite para las reservas de viajes redondos, el seguimiento de apartados, la asignación de salidas y el control de unidades.

## Requisitos

- Python 3.10 o posterior
- pip

## Despliegue con Netlify

La aplicación de servidor es Flask/Python. Netlify publica el dominio y ejecuta un proxy Edge, mientras que el backend Flask y sus datos persistentes se alojan en Render.

1. Sube este repositorio a GitHub.
2. En Render, crea un Blueprint desde el repositorio y usa `render.yaml`. Define `DEMO_PASSWORD` y `QA_ACCESS_TOKEN` como secretos cuando Render los solicite. El Blueprint genera `SECRET_KEY` y configura un disco persistente para SQLite y fotografías.
3. Espera a que el backend quede disponible y copia su URL HTTPS, por ejemplo `https://davila-tours-backend.onrender.com`.
4. En Netlify, importa el mismo repositorio. Usa el `netlify.toml` incluido y crea la variable de entorno `BACKEND_URL` con la URL HTTPS del backend (solo el origen, sin rutas). Dale el alcance **Functions** para que la Edge Function pueda leerla y vuelve a desplegar.
5. Comparte con los usuarios la URL del sitio de Netlify. Las rutas, formularios, sesiones y archivos estáticos se envían al backend Flask.

El servicio Render incluido requiere un plan con disco persistente. El disco limita el servicio a una instancia; esto permite que SQLite y las fotos sigan disponibles después de reinicios, pero no es una configuración de alta disponibilidad. No guardes secretos en el repositorio. Si necesitas escalar a varias instancias, migra SQLite a PostgreSQL y las fotografías a almacenamiento de objetos antes de cambiar el escalado.

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
- Acceso del equipo de pruebas: `/qa` (requiere el token configurado y permite seleccionar un perfil)
- Centro de pruebas QA: `/pruebas` (no aparece en el menú; conserva los permisos del perfil)

El registro público crea cuentas de pasajero. Las cuentas operativas iniciales para desarrollo son:

| Perfil | Correo | Contraseña inicial |
| --- | --- | --- |
| Administrador | `admin@gmail.com` | `Davilatours2026!` |
| Chofer 1–6 | `chofer1@gmail.com` … `chofer6@gmail.com` | `Davilatours2026!` |

Configura `SECRET_KEY`, `DEMO_PASSWORD` y `QA_ACCESS_TOKEN` como variables de entorno antes de desplegar; configura también `DEMO_ADMIN_EMAIL` si aplica. `QA_ACCESS_TOKEN` no tiene un valor predeterminado: sin configurarlo, `/qa` responde que el acceso está deshabilitado. Genera un token aleatorio largo y compártelo solo con el equipo de QA. Por ejemplo, en PowerShell:

```powershell
1.- py -c "import secrets; print(secrets.token_urlsafe(32))"
2.- $env:QA_ACCESS_TOKEN = "<token-generado>"
3.- flask --app app run --debug
```

Al validar el token se puede elegir pasajero, chofer o administrador. El acceso usa cuentas de prueba de esos perfiles y mantiene las protecciones por rol; las acciones realizadas modifican los datos de la aplicación. La primera selección de pasajero crea una cuenta QA si todavía no existe una cuenta de ese perfil. Usa HTTPS en el despliegue y no expongas un servidor de desarrollo en producción. `RESERVATION_PRICE_CENTS` establece la tarifa por pasajero en centavos de MXN; mientras no se defina, los importes aparecen como “Por confirmar”, ya que los documentos no especifican precios.

El centro de pruebas no aparece en el menú. Comparte `/qa` y el token directamente con el equipo de pruebas. La ruta anterior `/microbusero` redirige al portal `/chofer` y mantiene la misma autorización.

## Reservas y correo

No se integra una pasarela de pagos. Cada reserva genera un folio y empieza como **Pendiente**. El personal puede actualizarla a **Apartado** o **Pagado**; las cancelaciones se muestran de forma diferenciada y liberan los asientos. Cada apartado guarda los asientos elegidos y evita que se asignen dos veces para la misma salida.

El símbolo `@` y los dominios `gmail.com`, `hotmail.com` y `outlook.com` se cargan de tablas SQLite y se presentan como selector en registro e inicio de sesión. Las consultas usan parámetros SQL y las contraseñas se almacenan con hash seguro.

Las cuentas de pasajero, chofer y administrador pueden subir su foto en `/perfil`. Se aceptan JPG, PNG y WEBP de hasta 5 MB; la aplicación normaliza la imagen y detecta localmente un rostro antes de guardarla bajo `instance/profile_photos`. La detección no verifica identidad ni garantiza que sea una fotografía real; la foto se sirve únicamente a una sesión autenticada. La validación requiere Pillow, NumPy y OpenCV, instalados con `pip install -r requirements.txt`.

El mapa de asientos muestra una silueta en los lugares ocupados y en el puesto del chofer; no muestra las fotos de perfil de los pasajeros.

## Interfaz

Se implementó la propuesta **A — Ruta confiable** del manual de color: azul profundo `#12304A`, turquesa `#087E8B`, superficies claras, tipografía de sistema con pesos más marcados y tamaños legibles, e iconos SVG coherentes junto con colores semánticos de reserva. La portada presenta la misión, visión y equipo de Dávila Tours, además de una animación de bienvenida con el autobús. El diseño adapta listados y formularios a pantallas pequeñas, mantiene el foco visible y combina icono, color y texto en los estados.

## Documentación

Consulta la [documentación técnica y guía de usuario](Docs/Documentacion_TK001_Davila_Tours.docx) para conocer los perfiles, flujos, configuración, modelo de datos, pruebas y limitaciones documentadas del sistema.

Consulta también las [maquetas visuales del sistema](Docs/Maquetas_Davila_Tours.pdf), con las pantallas principales de pasajeros, choferes y administración.
