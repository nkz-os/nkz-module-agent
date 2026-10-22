#!/usr/bin/env node

/**
 * Docs-as-Code Validator
 * Enforces strict Frontmatter presence in all documentation markdown files.
 * Requisites: title (required), description (required), sidebar.order (warns if missing).
 */

import fs from 'fs';
import path from 'path';

const DOCS_DIR = path.join(process.cwd(), 'docs');

// Regex para capturar estritamente el primer bloque YAML
const FRONTMATTER_REGEX = /^\s*---\n([\s\S]*?)\n---/;

function logError(msg) {
  console.error(`\x1b[31m[ERROR]\x1b[0m ${msg}`);
}

function logWarn(msg) {
  console.warn(`\x1b[33m[WARN]\x1b[0m ${msg}`);
}

function logSuccess(msg) {
  console.log(`\x1b[32m[OK]\x1b[0m ${msg}`);
}

// 1. Verificar existencia de la carpeta docs/
if (!fs.existsSync(DOCS_DIR)) {
  logError(`El directorio '/docs' no existe en este repositorio.`);
  logError(`Todos los módulos deben contener una carpeta '/docs' con la documentación técnica.`);
  process.exit(1);
}

// 2. Recorrer archivos recursivamente
function getAllMarkdownFiles(dirPath, arrayOfFiles = []) {
  const files = fs.readdirSync(dirPath);

  files.forEach(function (file) {
    const fullPath = path.join(dirPath, file);
    if (fs.statSync(fullPath).isDirectory()) {
      arrayOfFiles = getAllMarkdownFiles(fullPath, arrayOfFiles);
    } else {
      if (file.endsWith('.md') || file.endsWith('.mdx')) {
        arrayOfFiles.push(fullPath);
      }
    }
  });

  return arrayOfFiles;
}

const mdFiles = getAllMarkdownFiles(DOCS_DIR);

if (mdFiles.length === 0) {
  logError(`El directorio '/docs' está vacío. Debe contener al menos un archivo .md o .mdx.`);
  process.exit(1);
}

let hasErrors = false;

// 3. Validar cada archivo
mdFiles.forEach((filePath) => {
  const content = fs.readFileSync(filePath, 'utf-8');
  const relativePath = path.relative(process.cwd(), filePath);
  
  const match = content.match(FRONTMATTER_REGEX);
  
  if (!match) {
    logError(`[${relativePath}] No contiene un bloque Frontmatter (YAML) válido al inicio del archivo.`);
    hasErrors = true;
    return;
  }

  const frontmatterRaw = match[1];
  
  // Parsea de forma sencilla (búsqueda de claves)
  const hasTitle = /^title\s*:/m.test(frontmatterRaw);
  const hasDescription = /^description\s*:/m.test(frontmatterRaw);
  const hasSidebarOrder = /^sidebar\.order\s*:/m.test(frontmatterRaw) || /^  order\s*:/m.test(frontmatterRaw); // Soporta formato plano o anidado

  if (!hasTitle) {
    logError(`[${relativePath}] Falta la propiedad requerida: 'title'`);
    hasErrors = true;
  }

  if (!hasDescription) {
    logError(`[${relativePath}] Falta la propiedad requerida: 'description'`);
    hasErrors = true;
  }

  if (hasTitle && hasDescription) {
    if (!hasSidebarOrder) {
      logWarn(`[${relativePath}] Falta la propiedad 'sidebar.order'. Es opcional pero recomendada para la jerarquía visual.`);
    }
    logSuccess(`Validado: ${relativePath}`);
  }
});

if (hasErrors) {
  logError(`Falló la validación de la documentación. Corrige los errores en el Frontmatter antes de integrar (merge).`);
  process.exit(1);
} else {
  logSuccess(`Toda la documentación (${mdFiles.length} archivos) cumple el estándar SOTA de NKZ.`);
  process.exit(0);
}
