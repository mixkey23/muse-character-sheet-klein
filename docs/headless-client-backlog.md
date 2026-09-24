# Headless Client Backlog — Man4TechCharacterSheetKlein sin UI interactiva

> Backlog de seguimiento a la investigación de orquestación headless hecha en sesión
> (auditoría de `character_sheet_klein.py::run()` línea por línea + código fuente real de
> `comfyanonymous/ComfyUI` — `execution.py`/`server.py` — para confirmar, no asumir, cómo
> un backend externo sin navegador puede llevar el nodo de "nada generado" a "character_sheet
> final" usando solo `POST /prompt` + `GET /history` + WS estándar).
>
> Numeración `HC-xxx` (Headless Client) para no pisar `FS-xxx` (backlog del fork,
> figure-agnostic) ni `FW-xxx` (backlog de integración a Framesmith).

## Por qué existe este backlog

La investigación de esta sesión determinó, con evidencia de código, que **no hace falta
ningún endpoint nuevo** — el canal estándar de ComfyUI (`GET /history/{prompt_id}`, WS
`executed`, `GET /view`) ya expone todo lo que un cliente headless necesita, incluyendo el
`ui` dict completo (`pose_previews`, `seeds`, `prompts`, `confirmed`, `status`) que hoy
consume el panel JS del nodo por ese mismo camino (`js/character_sheet_klein.js:659`,
`onExecuted`). Esto **contradice la premisa de `FW-008`** en el backlog de integración a
Framesmith ("el fork necesita un endpoint... para que Framesmith pueda leer los paths de
preview") — ver `HC-009` para la corrección formal de ese ticket.

Lo que SÍ quedó pendiente — y es el contenido de este backlog — es: (a) validar todo esto
contra una instancia ComfyUI real corriendo (la sesión de investigación solo pudo leer
código estático, no ejecutar el nodo), (b) decidir qué hacer con los gaps concretos que la
lectura de código encontró, y (c) dejar un contrato/cliente de referencia utilizable en vez
de conocimiento disperso en una conversación.

## Hallazgos ya confirmados — no re-investigar desde cero

Evidencia trazada en `character_sheet_klein.py` (números de línea de la versión en
`claude/determined-gates-he5p2d` al momento de escribir esto):

1. **`_SESSIONS` es un dict module-level in-memory**, keyeado por `unique_id` (línea 693).
   No sobrevive a un restart de ComfyUI. `char_latent`/`pose_latents` se recomputan solos
   si faltan (708-731) — el cliente nunca necesita reconstruirlos.
2. **Una pose CONFIRMADA es recuperable solo desde `state_json` + disco**, sin depender de
   `_SESSIONS` — `_restore_confirmed_preview`/`_load_preview_pixels` (523-558) relee el PNG
   usando `filename`/`subfolder`/`type` que el propio cliente ya tiene. Corre en cada
   `run()` (702-704), sin importar si la sesión está "fría".
3. **Gap crítico:** si `sig` (hash de inputs+config) no matchea `sess["sig"]` — lo cual pasa
   siempre en una sesión nunca vista con ese `unique_id` — el código descarta CUALQUIER
   `action` que no sea `"finalize"` (líneas 694-700). Un cliente headless no puede mandar
   `edit`/`edit_all`/`reroll`/etc. como primera llamada de una sesión nueva: se ignora sin
   error visible.
4. **`confirmed` es 100% responsabilidad del cliente** — el servidor nunca lo calcula, solo
   hace eco de lo que recibe (confirmado también revisando `js/character_sheet_klein.js:663`,
   donde el JS ni siquiera usa el `confirmed` que vuelve del servidor salvo en el caso
   `reset`).
5. **El dict `"ui"` que retorna `run()` es el canal estándar de ComfyUI**, confirmado contra
   `execution.py`/`server.py` de ComfyUI: `send_sync("executed", {..., "output":
   enriched_output_ui, "prompt_id": ...})` y `GET /history/{prompt_id}` devuelven ambos,
   sin transformación, las mismas claves. Sin endpoint nuevo.
6. **Edición activa (`edit_instruction`/`edit_source_image`) no sobrevive a un restart** —
   la recuperación best-effort de `apply_edit` (793-795) solo repone `image`, no esas dos
   claves, así que un `reroll_edit` posterior después de un restart cae silenciosamente a un
   reroll normal (pierde la edición, sin avisar).
7. **`unique_id` debe mantenerse estable dentro de UN flujo de personaje** (para que
   `edit`/`edit_all` sobrevivan entre llamadas — ver hallazgo 3) y **debe variar entre
   flujos concurrentes de personajes distintos** — usar `class_type` como clave en vez de
   `unique_id` colapsaría todas las sesiones concurrentes en una sola (evaluado y
   descartado explícitamente).
8. Secuencia mínima feliz: **3 llamadas** (generar → editar opcional → confirmar+finalize),
   no 2 — por el hallazgo 3. Sin edición, son 2.

## Sprint 1 — Validación empírica + documentación base (bloqueante para todo lo demás)

### HC-001 — Validar la secuencia de 3 llamadas contra una instancia ComfyUI real
Todo lo de arriba salió de lectura estática de código (esta sesión no tenía ComfyUI/GPU
disponible). Antes de tocar una línea de código o de prometerle nada a Framesmith, correr
esto contra una instancia real:
- Levantar ComfyUI con este pack instalado (`Man4TechCharacterSheetKlein` +
  `Man4TechSheetAlignFigure` + dependencias del README).
- Script Python puro (`requests` + `websockets`, sin navegador) que ejecute exactamente la
  secuencia de 3 llamadas documentada en la sesión de investigación (ver transcript o
  `HC-002` una vez escrito) contra una foto de referencia real.
- Confirmar en vivo: (a) la llamada 1 genera y devuelve `pose_previews` tipo `"temp"`; (b)
  la llamada 2 (`edit_all`) efectivamente edita y el `status` de la respuesta es
  `"ready"`, no `"generating"`; (c) la llamada 3 con `confirmed:[true]*5` +
  `action:{"type":"finalize"}` devuelve el `character_sheet` final sin regenerar nada.
- Confirmar el hallazgo 3 en vivo: mandar `edit_all` como PRIMERA llamada de una sesión
  nunca vista y verificar que efectivamente se ignora (no rompe, pero tampoco edita).
- Confirmar el modo de falla documentado: borrar a mano el PNG de una pose confirmada entre
  llamadas y verificar que `finalize` tira el `ValueError` esperado, visible en
  `GET /history/{prompt_id}` como error de ejecución.

**Criterio de aceptación:** documento (o test automatizado) con el request/response real de
cada una de las 3 llamadas contra una instancia viva, no simulado.

### HC-002 — Documentar el contrato de orquestación headless en el repo
Este conocimiento hoy solo existe en una conversación. Agregar al `README.md` (o un
`docs/headless-api.md` nuevo, referenciado desde el README) una sección "Driving this node
without the interactive UI" con: la secuencia de 3 llamadas y sus `state_json` completos
(tal cual quedaron documentados en esta sesión), la lista de `action.type` válidos y su
forma exacta (`reroll`, `edit`, `edit_all`, `reroll_edit`, `reset_edit`, `reset_prompt`,
`finalize`), y los 8 hallazgos de la sección anterior como "gotchas" explícitos.

**Depende de:** HC-001 (no documentar como confirmado algo que no se corrió en vivo).

## Sprint 2 — Endurecer los gaps encontrados (decisiones de diseño, no solo documentarlos)

Cada ticket de este sprint es una decisión: ¿se resuelve en código, o se deja como
contrato documentado que el cliente debe respetar? Definir caso por caso, no asumir "hay
que arreglarlo todo".

### HC-003 — Sesión fría descarta `action` silenciosamente (hallazgo 3)
Opciones a evaluar:
- (a) Dejarlo como está, documentado (HC-002) — el cliente siempre hace un "warm-up" call
  de generación simple antes de cualquier `edit`/`edit_all`. Costo cero en código.
- (b) Hacer que una sesión fría con `action` distinto de `finalize` genere primero
  (comportamiento normal de sesión nueva) Y aplique la acción en la MISMA llamada, en vez
  de descartarla — colapsaría el flujo a 2 llamadas también para el caso con edición.
  Requiere tocar el orden de líneas 694-902 de `run()` con cuidado de no romper el flujo
  interactivo actual del panel JS (que nunca manda `edit_all` en una sesión fría porque el
  usuario siempre ve el resultado de la generación primero).
- Recomendación por defecto si no hay tiempo para (b): ir con (a), es cero-riesgo.

### HC-004 — Continuidad de edición no sobrevive a un restart (hallazgo 6)
Evaluar si vale la pena persistir `edit_instruction` (string) como metadata junto al PNG de
preview (ej. un `.json` sidecar en el mismo `subfolder`, o EXIF/PNG text chunk) para que
`apply_edit`/`reroll_edit` puedan recuperarlo igual que ya recuperan los píxeles. Sopesar
contra: ¿un cliente headless real necesita `reroll_edit` sobreviviendo a un restart, o le
alcanza con volver a mandar el mismo `edit`/`edit_all` desde cero si el proceso murió a
mitad de flujo? Probablemente NO vale la pena para el caso de uso de Framesmith (flujo
corto, secuencial, sin necesidad de sobrevivir un restart de ComfyUI a mitad de una edición)
— documentar la limitación (HC-002) puede ser suficiente en vez de tocar código.

### HC-005 — Ciclo de vida de los archivos de preview (temp vs output)
El propio mensaje de error del código ("temp previews can get cleaned up over time",
`character_sheet_klein.py:554-555`) admite que no hay garantía de retención para las poses
NO confirmadas (guardadas en `folder_paths.get_temp_directory()` vía `PreviewImage`). Para
un flujo headless con una ventana de tiempo larga entre llamadas (ej. esperando confirmación
humana del lado de Framesmith, no de ComfyUI), esto es un riesgo real de que la llamada de
`finalize` falle por archivo faltante.
- Investigar (no asumir) la política real de limpieza del directorio temp de la instancia
  ComfyUI objetivo — depende de configuración/versión, no es universal.
- Evaluar cambiar `_preview(image, persist=confirmed[i])` para que TAMBIÉN persista a
  `output` (no solo `temp`) durante el flujo, no únicamente al confirmar — trade-off directo
  contra uso de disco (5 imágenes por pose intermedia, por cada llamada de reroll/edit, si
  no se poda nada).

## Sprint 3 — Cliente de referencia + manejo de errores

### HC-006 — Cliente Python de referencia reusable
Un módulo (no solo un script de test) que implemente la secuencia de HC-001/HC-002 como
funciones reusables: `submit_generate()`, `submit_edit()`/`submit_edit_all()`,
`submit_finalize()`, más un helper de espera (`await_result(prompt_id)` sobre WS o polling
de `/history`). Este es el artefacto concreto que Framesmith (`FW-004` en su backlog) puede
adaptar directamente en vez de reimplementar la orquestación desde la documentación en
prosa.

**Depende de:** HC-001, HC-002.

### HC-007 — Catálogo de modos de falla y su forma en WS/history
Enumerar cada excepción que `run()` puede tirar (`_node()` — nodo no registrado,
`_restore_confirmed_preview` — preview faltante, `MuseSheetAlignFigure`/
`Man4TechSheetAlignFigure.align` — sujeto no detectado o panel muy angosto para la figura)
y documentar/verificar en vivo (junto con HC-001) cómo se ve cada una en
`GET /history/{prompt_id}` y en el evento WS de error, para que el cliente de HC-006 pueda
capturarlas explícitamente en vez de asumir happy path.

## Sprint 4 — Concurrencia y feedback a Framesmith

### HC-008 — Contrato de `unique_id` por flujo de personaje
Formalizar (documentar en HC-002 y, si aplica, en el cliente de HC-006) la regla del
hallazgo 7 y 8 de la investigación previa: un `unique_id` estable por flujo de personaje,
distinto entre flujos concurrentes. Si Framesmith va a lanzar generaciones en paralelo para
distintos personajes, este contrato es lo que les da aislamiento — no hace falta tocar este
repo, es responsabilidad exclusiva de quien arma el JSON del `/prompt`.

### HC-009 — Actualizar `FW-008` del backlog de Framesmith
`FW-008` (backlog de integración a Framesmith, sección FASE 3) da por sentado que "el fork
necesita un endpoint (o nodo que exponga el `state_json` actual)". Esta investigación lo
contradice con evidencia (ver "Por qué existe este backlog" arriba). Acción: en la próxima
sesión de Framesmith, reemplazar `FW-008` por una versión corregida que documente el uso del
canal estándar de ComfyUI (WS `executed` + `GET /history` + `GET /view`) en vez de un
endpoint custom — probablemente reduce el alcance de esa fase entera, ya no es "trabajo en
el fork" sino solo consumo del cliente de HC-006 desde el lado de Framesmith.

**Depende de:** HC-001 (confirmar en vivo antes de comunicarle a Framesmith que no hace
falta el endpoint que su propio backlog asume).
