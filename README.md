# Amazon Price Tracker

Revisa precios de Amazon con Playwright (Edge/Chrome headless), guarda el histórico y avisa con una
notificación de Windows cuando un producto llega a su precio meta.

## Instalación

```powershell
pip install -r requirements.txt
```

No hace falta `playwright install`: usa el Edge (o Chrome) que ya tienes instalado.

## Productos

Edita `data/products.json` o usa la CLI:

```powershell
python tracker.py add "https://www.amazon.com/dp/B075TF61VH" --target 25 --normal 32.99 --name "Billetera"
python tracker.py list
python tracker.py remove B075TF61VH
```

### Avisar cuando un color esté disponible

En vez de `target_price`, agrega `watch` al producto en `data/products.json`:

```json
{
  "id": "B0F83V4NW6",
  "name": "OLIGHT i3E EOS — negro",
  "url": "https://www.amazon.com/dp/B0F83V4NW6",
  "watch": {
    "variant": "Black",
    "search": "OLIGHT i3E EOS black",
    "keywords": ["olight", "i3e", "eos", "black"]
  }
}
```

Cada revisión busca el color `variant` entre las variantes de la publicación. Si no está y se definió `search`,
busca en Amazon una publicación aparte cuyo título tenga todas las `keywords`. Avisa cuando se puede comprar.

`settings` en `data/products.json`:

| clave | valores |
|---|---|
| `notification` | `toast` (centro de notificaciones), `msgbox` (ventana) o `both` |
| `browser_channel` | `msedge` o `chrome` |
| `headless` | `true` / `false` (ponlo en `false` si Amazon empieza a bloquear) |

## Ejecutar

```powershell
python tracker.py               # revisa todos los productos
python tracker.py test-notify   # prueba la notificación
```

### Avisos por tramos

Progreso = `(precio normal − precio actual) / (precio normal − meta)`. Avisa cada vez que el producto alcanza un tramo nuevo:

| Progreso | Color | Aviso |
|---|---|---|
| < 25 % | 🔴 rojo | — |
| ≥ 25 % | 🟠 naranja | "25 % del camino" |
| ≥ 50 % | 🔵 azul | "50 % del camino" |
| ≥ 75 % | 🟢 verde | "75 % del camino" |
| 100 % (≤ meta) | 🟢 verde | "¡Precio objetivo alcanzado!" |

Si el precio sube y baja de tramo, vuelve a avisar cuando lo alcance otra vez. En la meta avisa de nuevo solo si el precio cambia.

## Programador de tareas

```powershell
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -At "11:00"
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1
```

## Dashboard

```powershell
python serve.py      # abre http://localhost:8765
```

Logs en `logs/tracker.log`.
