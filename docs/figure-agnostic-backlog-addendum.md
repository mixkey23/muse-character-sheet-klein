# Figure-Agnostic Character Sheets — Addendum al backlog original (FS-029+)

> El backlog original ("Figure-Agnostic Character Sheets", `FS-001`–`FS-028`) es un documento
> externo (adjuntado como contexto al arrancar el trabajo en este fork) y no vive como archivo
> en este repo. Este addendum junta los tickets `FS-0XX` nuevos que salieron de trabajo real
> sobre el código — hallazgos que no estaban en el backlog original porque se descubrieron
> recién al implementar/auditar Phase 1 — para no perderlos en el historial de una conversación.
> Numeración continúa desde `FS-028`.

## FS-029 — `GUIDE_CROPS`/`TARGET_SIZE` tienen proporciones calibradas para humano, no agnósticas

**Descubierto:** auditando `character_sheet_klein.py` para responder una pregunta sobre cómo
generar pose guide images por `figure_type` (sesión post-Phase-1). No es un bug introducido
por el trabajo de Phase 1 — es geometría preexistente del fork original que Phase 1 (FS-002,
"preservar geometría del guide") dejó **intacta a propósito**, y que ahora hay que reconocer
como una limitación conocida antes de que el test de las 3 figuras la exponga sin contexto.

### El problema

`GUIDE_CROPS` (`character_sheet_klein.py:78-84`) y `TARGET_SIZE` (`character_sheet_klein.py:53-59`)
asignan anchos DISTINTOS a cada vista, y esa asimetría es intencional pero específicamente humana.
Cita del propio comentario del código (`character_sheet_klein.py:41-48`, sobre por qué los
perfiles se angostaron de 544 a 416/416px):

> "a true side-on silhouette is only chest-to-back deep, much narrower than a front-on
> shoulder-to-shoulder view"

Es decir: para un humano parado, el panel de PERFIL es el más angosto de los 5
(`left_profile`=267px, `right_profile`=237px en `GUIDE_CROPS`; 416px de ancho de generación en
`TARGET_SIZE`), y el de FRENTE es de los más anchos (307px / 544px) — porque de perfil un
humano ocupa poco ancho (pecho-espalda) y de frente ocupa más (hombro a hombro).

**Para un cuadrúpedo esta relación está invertida.** De perfil (hocico a cola) es donde un
cuadrúpedo ocupa MÁS espacio horizontal — es su silueta más ancha, no la más angosta. De
frente (mirando de cara) es donde ocupa MENOS — solo el ancho del pecho. Meterlo en los
mismos paneles (perfil angosto, frente ancho) le da al modelo el espacio invertido de lo que
necesita.

### Por qué no se tocó en Phase 1

`FS-002` pedía explícitamente preservar la geometría del guide, y el milestone de Phase 1 es
sobre lenguaje de prompts (`FS-004`/`FS-005`) y el guide mannequin en sí (`FS-003`), no sobre
la geometría de los paneles de generación. Tocar `GUIDE_CROPS`/`TARGET_SIZE` ahora habría sido
adelantar trabajo de `FS-006`–`FS-008` (Structure Analysis) sin haber corrido primero el test
de las 3 figuras que el propio backlog pide correr antes — el criterio de secuenciación que
vos mismo estableciste (no reordenar, no comprimir fases).

### Por qué importa igual

Es información que **el test de las 3 figuras va a necesitar para interpretarse bien**: si el
resultado del cuadrúpedo sale con la silueta apretada/deformada en el panel de perfil, la
causa más probable no es "el prompt está mal" ni "el guide mannequin está mal dibujado" — es
esta asimetría geométrica preexistente. Sin este ticket escrito, ese resultado se podría
malinterpretar como una falla de `FS-004`/`FS-005` (ya cerradas) en vez de reconocerse como
exactamente el tipo de ajuste que `FS-006`–`FS-008` existen para resolver.

### Alcance / qué hay que decidir cuando se llegue a esta fase

- No es un ajuste "por figure_type" simple (un `dict` de overrides como
  `FIGURE_TYPE_ALIGN_OVERRIDES` que ya existe para `figure_height`/`bottom_margin` en
  `_assemble_final`) porque acá el ancho de CADA panel depende de qué vista es la silueta
  ancha/angosta para ese tipo de cuerpo — no es un único valor a escalar, es una permutación
  distinta de qué vista es ancha y cuál angosta según el plano corporal.
- Afecta dos lugares independientes: `TARGET_SIZE` (canvas de generación, por pose) y
  `GUIDE_CROPS`/`REFERENCE_GUIDE_SIZE` (crop del pose_reference_image). Ambos comparten hoy la
  misma asimetría por la misma razón (ver comentario citado arriba) — si se corrige uno sin el
  otro quedan desalineados entre sí.
- Depende del resultado real del test de las 3 figuras (`FS-003`+`FS-004`+`FS-008`+`FS-009`
  según el criterio de secuenciación original) para saber si esto realmente rompe el output de
  cuadrúpedo en la práctica, o si el margen de error de `MuseSheetAlignFigure`/
  `Man4TechSheetAlignFigure` (que sí es genuinamente agnóstico, ver auditoría de
  `sheet_align.py`) alcanza a absorberlo sin ajuste.

**Estado:** abierto, sin resolver. Pendiente de confirmar con evidencia visual (no solo de
código) una vez que se corra el milestone de las 3 figuras.
