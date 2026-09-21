---
title: "[Tu Nombre de Módulo Aquí]"
description: "Escribe aquí una descripción muy breve (1 línea) de lo que hace tu módulo."
sidebar:
  order: 1
---

# Bienvenido a la Documentación de tu Módulo

> **¡Atención Desarrollador!** Esta carpeta `/docs` es mágica. Todo lo que escribas aquí se subirá automáticamente al portal público de documentación `nkz-os.org`. Por favor, **no pongas aquí notas privadas ni logs de errores**.

## ¿Cómo añadir el Logo y un Pantallazo de tu módulo? (Guía Fácil)

Para que tu módulo luzca profesional en el catálogo de la web oficial, necesitas incluir imágenes. Sigue estos **3 sencillos pasos**:

### Paso 1: Guarda tus imágenes aquí
Coge tu archivo del logo (por ejemplo, `logo.png`) y el pantallazo de tu aplicación (por ejemplo, `pantallazo.jpg`) y **arrástralos directamente dentro de esta misma carpeta `docs/`**. 
*(Importante: Ponlos junto a este archivo `index.md`, no crees subcarpetas para las imágenes).*

### Paso 2: Escribe este código
Copia y pega el código que ves justo debajo, pero cambiando el nombre del archivo por el tuyo real:

```markdown
<!-- Así se pone el logo: -->
![Logo de mi módulo](./logo.png)

<!-- Así se pone un pantallazo: -->
![Vista principal del módulo](./pantallazo.jpg)
```

### Paso 3: ¡Listo!
No tienes que hacer nada más. Cuando hagas `git push` a tu rama `main`, nuestro motor central se encargará de descargar tus imágenes automáticamente y las mostrará perfectamente encuadradas en la web oficial.
