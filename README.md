# 📚 WebVault

A minimalist self-hosted digital library
for HTML and EPUB collections.

> **Three files. One database. Thousands of books.**
>
> **Minimalism is a feature.**

WebVault es una biblioteca digital autohospedada para colecciones **HTML** y **EPUB**, diseñada con una 
filosofía muy simple:

> **Hacer mucho con muy poco.**

En lugar de construir una arquitectura compleja basada en numerosos servicios y dependencias, WebVault 
utiliza una arquitectura extremadamente ligera capaz de gestionar bibliotecas de decenas de miles de 
libros con un consumo mínimo de recursos.

---

# Filosofía

WebVault persigue cuatro principios fundamentales.

## ⚡ Eficiencia

Cada componente realiza únicamente la tarea para la que fue diseñado.

No existen procesos innecesarios.

No existen servicios que permanezcan consumiendo recursos sin aportar valor.

---

## 🪶 Minimalismo

La simplicidad no es una limitación.

Es una decisión de diseño.

La arquitectura principal está formada únicamente por:

```text
crea_base.py
      │
      ▼
 webvault.db (SQLite + FTS5)
      │
      ▼
 server.py
      │
      ▼
 reader.html
```

Tres archivos principales.

Una única base de datos.

Sin complejidad innecesaria.

---

## 🔒 Control del usuario

Toda la biblioteca permanece bajo el control del propietario.

No existe dependencia de servicios externos.

Toda la experiencia de lectura pertenece al usuario.

---

## 📚 Escalabilidad

WebVault está pensado para gestionar bibliotecas de decenas de miles de libros manteniendo la misma 
arquitectura.

No es necesario cambiar de tecnología al crecer la colección.

---

# Arquitectura

WebVault divide claramente las responsabilidades.

## Servidor

El servidor únicamente:

* distribuye libros
* responde búsquedas
* sirve metadatos
* entrega portadas

No mantiene sesiones de lectura.

No procesa EPUB.

No renderiza páginas.

---

## Navegador

Toda la experiencia de lectura ocurre en el cliente.

El navegador:

* abre EPUB
* descomprime ZIP
* interpreta OPF
* genera el índice
* renderiza páginas
* mantiene FlipBook
* guarda progreso
* almacena marcadores
* guarda resaltados
* administra notas
* mantiene biblioteca personal

Todo ello mediante almacenamiento local.

---

# Tecnologías

* Python
* SQLite
* SQLite FTS5
* EPUB.js
* JSZip
* BeautifulSoup
* EbookLib
* Pillow
* Caddy

---

# Características

## Biblioteca

* Biblioteca HTML
* Biblioteca EPUB
* Portadas automáticas
* Metadatos EPUB
* Actualización incremental

---

## Búsqueda global

SQLite FTS5 indexa el contenido completo de todos los EPUB.

Permite buscar:

* palabras
* frases completas
* fragmentos
* citas
* notas del traductor
* apéndices

La búsqueda devuelve:

* libro
* contexto
* capítulo
* acceso directo al lector

Todo ello prácticamente de forma instantánea.

---

## Reader

El lector incorpora:

* EPUB.js
* FlipBook
* Índice
* Pantalla completa
* Temas
* Tamaño de fuente
* Progreso

---

## Estudio

El lector incorpora herramientas de estudio completas.

### ❤️ Marcadores

Guardar cualquier posición del libro.

---

### 🖍️ Resaltados

Resaltados por colores.

Persistentes.

---

### 📝 Notas

Notas asociadas a texto seleccionado.

Cada nota:

* conserva su color
* resalta el texto correspondiente
* aparece integrada en Marcadores

---

### 🎯 Localización temporal

Al abrir:

* un marcador
* un resaltado
* una nota

el texto recibe un subrayado negro temporal para localizar inmediatamente el punto exacto.

El subrayado desaparece automáticamente al cambiar de página.

---

### 🔎 Buscar dentro del libro

Búsqueda completa dentro del EPUB abierto.

Resultados con contexto.

Acceso directo al punto encontrado.

---

# Biblioteca personal

Cada navegador mantiene:

* progreso
* libros leídos
* libros en lectura
* marcadores
* resaltados
* notas
* preferencias
* lector seleccionado

Toda esta información permanece exclusivamente en el navegador.

---

# Exportación de perfiles

El lector permite exportar:

* progreso
* biblioteca
* marcadores
* resaltados
* notas
* preferencias

en un único archivo JSON.

Posteriormente puede importarse en cualquier otro navegador.

---

# Rendimiento

La mayor parte del trabajo ocurre en el navegador.

El servidor únicamente sirve archivos.

Esto permite mantener una carga muy baja incluso con numerosos lectores simultáneos.

La arquitectura resulta especialmente adecuada para servidores con recursos limitados.

---

WebVault no nació intentando ser el software con más funciones.

Nació intentando ser el software con la mejor relación entre simplicidad y capacidad.

# Instalación

## Requisitos

* Linux (Ubuntu recomendado)
* Python 3.11+
* SQLite con FTS5
* Caddy (recomendado)
* Navegador moderno

---

## Dependencias Python

```bash
pip install beautifulsoup4 lxml ebooklib pillow
```

---

## Dependencias del sistema

```bash
sudo apt update

sudo apt install \
    python3 \
    python3-pip \
    sqlite3 \
    caddy
```

---

## Librerías JavaScript

Copiar al directorio `static/`:

```
epub.min.js
jszip.min.js
```

---

# Puesta en marcha

Construir la biblioteca:

```bash
python3 crea_base.py
```

Iniciar el servidor:

```bash
python3 server.py
```

---

# Producción

Configuración recomendada:

Servidor HTTPS mediante Caddy.

Procesos persistentes mediante:

```bash
screen -S crea_base
python3 crea_base.py
```

```bash
screen -S webvault
python3 server.py
```

---

# Dependencias que NO necesita

WebVault evita deliberadamente utilizar:

* Docker
* Kubernetes
* Redis
* PostgreSQL
* Elasticsearch
* RabbitMQ
* Prometheus
* Grafana
* Node.js

La simplicidad forma parte del diseño del proyecto.

---

# Roadmap

## Completado

* ✅ Biblioteca HTML
* ✅ Biblioteca EPUB
* ✅ SQLite FTS5
* ✅ FlipBook
* ✅ Marcadores
* ✅ Resaltados
* ✅ Notas
* ✅ Búsqueda global
* ✅ Búsqueda dentro del libro
* ✅ Biblioteca personal
* ✅ Exportación e importación de perfiles

## Futuro

* 🤖 Asistente IA opcional
* 📱 Mejoras móviles
* 🔌 Sistema de plugins

---
## 🌐 Live Demo

Puedes probar WebVault directamente desde el navegador:

**Demo:** https://demo.tudominio.com

Usuario:pedro
Contraseña:trascala

Seguridad

WebVault, por simplicidad, se distribuye con la autenticación deshabilitada (NOPASSWD) para facilitar las pruebas locales.

Para instalaciones accesibles desde Internet se recomienda habilitar autenticación. El código necesario para ello ya está incluido en el proyecto y puede activarse configurando usuario y contraseña según la documentación.

La demostración utiliza una pequeña colección de libros libres de derechos para mostrar todas las 
funcionalidades del lector:

- 📚 Biblioteca
- 🔍 Búsqueda global
- 📖 Reader EPUB
- 📖 FlipBook
- ❤️ Marcadores
- 🖍️ Resaltados
- 📝 Notas
- 🔎 Buscar dentro del libro
- 💾 Exportación e importación del perfil

---
# Licencia

MIT License.

Consulta el archivo **LICENSE** para obtener el texto completo.

---

# Agradecimientos

WebVault utiliza excelentes proyectos Open Source:

* EPUB.js
* JSZip
* SQLite
* BeautifulSoup
* EbookLib
* Pillow
* Caddy

Gracias a todos sus desarrolladores.

---

# Filosofía de diseño

WebVault ha evolucionado durante meses manteniendo siempre una misma idea:

> **No añadir complejidad innecesaria.**

Cada nueva función debía integrarse sin romper la arquitectura existente.

El resultado es una biblioteca digital moderna, potente y sorprendentemente sencilla.

---

# Lema

> **Three files. One database. Thousands of books.**

Porque la mejor ingeniería no siempre consiste en añadir más piezas.

Muchas veces consiste en conseguir que unas pocas piezas hagan extraordinariamente bien su trabajo.

